"""Shared core for the history-search skill.

Contains:
- the OpenCode SQLite data layer (read-only, V2 schema: session_v2 +
  session_message), ported from the opencode-history-search skill
- shared utilities: date filter parsing, fuzzy scoring, result formatting

Python 3 stdlib only. Nothing here ever writes to either history store.
"""
import calendar
import difflib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

DEFAULT_LIMIT = 50
DAY_MS = 86_400_000
V2_TABLES = ("session_v2", "session_message")


class HistoryError(Exception):
    """User-facing error: missing DB, old schema, or bad arguments."""


class Match(object):
    __slots__ = ("session_id", "session_title", "project", "timestamp",
                 "match_type", "excerpt", "context", "message_id",
                 "part_id", "source")

    def __init__(self, session, timestamp, match_type, excerpt, context,
                 message_id=None, part_id=None, source=None):
        self.session_id = session["id"]
        self.session_title = session["title"]
        self.project = session["directory"]
        self.timestamp = timestamp
        self.match_type = match_type
        self.excerpt = excerpt
        self.context = context
        self.message_id = message_id
        self.part_id = part_id
        self.source = source


class MultitermMatch(object):
    __slots__ = ("session_id", "session_title", "timestamp", "project",
                 "term_hits", "source")

    def __init__(self, session_id, session_title, timestamp, project,
                 term_hits, source=None):
        self.session_id = session_id
        self.session_title = session_title
        self.timestamp = timestamp
        self.project = project
        self.term_hits = term_hits  # [(term, message_id, excerpt)]
        self.source = source


class TraceResult(object):
    __slots__ = ("session_id", "session_title", "timestamp", "first_touch",
                 "user_prompt", "tool_name", "file_path", "source")

    def __init__(self, session_id, session_title, timestamp, first_touch,
                 user_prompt, tool_name, file_path, source=None):
        self.session_id = session_id
        self.session_title = session_title
        self.timestamp = timestamp
        self.first_touch = first_touch
        self.user_prompt = user_prompt
        self.tool_name = tool_name
        self.file_path = file_path
        self.source = source


def _source_line(result):
    if getattr(result, "source", None):
        return "- Source: %s" % result.source
    return None


def compile_query(query, regex, case_sensitive):
    pattern = query if regex else re.escape(query)
    return re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)


def _ms(dt):
    return int(dt.timestamp() * 1000)


def parse_iso_ms(value):
    """Parse an ISO-8601 timestamp (e.g. pi's 2026-11-02T19:01:27.617Z)
    into epoch milliseconds. Returns None on failure."""
    if isinstance(value, (int, float)):
        if value > 10 ** 12:  # already milliseconds
            return int(value)
        return int(value * 1000)
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _shift_months(dt, months):
    total = dt.month - 1 + months
    y = dt.year + total // 12
    m = total % 12 + 1
    day = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=day)


def parse_date_filter(spec, now=None):
    """Parse a date spec into an inclusive (start_ms, end_ms) range.

    Formats: "today", "yesterday", "last N days/weeks/months",
    "YYYY-MM-DD", "YYYY-MM", "YYYY-MM-DD to YYYY-MM-DD".
    """
    now = now or datetime.now()
    s = (spec or "").strip().lower()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if s == "today":
        return _ms(day0), _ms(day0) + DAY_MS - 1
    if s == "yesterday":
        y = (now - timedelta(days=1)).replace(hour=0, minute=0,
                                              second=0, microsecond=0)
        return _ms(y), _ms(y) + DAY_MS - 1
    m = re.fullmatch(r"last (\d+) (day|week|month)s?", s)
    if m:
        n = int(m.group(1))
        if m.group(2) == "day":
            start = now - timedelta(days=n)
        elif m.group(2) == "week":
            start = now - timedelta(weeks=n)
        else:
            start = _shift_months(now, -n)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        return _ms(start), _ms(now)
    m = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})\s+to\s+(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            a = _ms(datetime(*[int(x) for x in m.groups()[:3]]))
            b = _ms(datetime(*[int(x) for x in m.groups()[3:]]))
        except ValueError:
            raise HistoryError("invalid date range: %r" % spec)
        if a > b:
            raise HistoryError(
                "start date must be before end date: %r" % spec)
        return a, b + DAY_MS - 1
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            a = _ms(datetime(*[int(x) for x in m.groups()]))
        except ValueError:
            raise HistoryError("invalid date: %r" % spec)
        return a, a + DAY_MS - 1
    m = re.fullmatch(r"(\d{4})-(\d{2})", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if not 1 <= mo <= 12:
            raise HistoryError("invalid month: %r" % spec)
        nxt = (y + 1, 1, 1) if mo == 12 else (y, mo + 1, 1)
        return _ms(datetime(y, mo, 1)), _ms(datetime(*nxt)) - 1
    raise HistoryError(
        'unrecognized date filter %r (supported: "today", "yesterday", '
        '"last N days/weeks/months", "YYYY-MM-DD", "YYYY-MM", '
        '"YYYY-MM-DD to YYYY-MM-DD")' % spec)


def filter_by_date(results, start_ms, end_ms):
    return [r for r in results if start_ms <= r.timestamp <= end_ms]


def fuzzy_score(query, text, max_scan=8000):
    """Score 0 (identical) .. 1 (no match); returns (score, best_segment).

    difflib approximation of fuse.js with ignoreLocation: short texts
    are scored with a full-string SequenceMatcher ratio; long texts are
    scored on 2x-query windows centred on occurrences of the query's
    leading 4 characters (capped at 64 windows / 8000 scanned chars).
    """
    q = (query or "").lower().strip()
    t = (text or "").lower()
    if not q or not t:
        return 1.0, ""
    if len(t) <= 64:
        r = difflib.SequenceMatcher(None, q, t, autojunk=False).ratio()
        return 1.0 - r, t
    if len(t) > max_scan:
        t = t[:max_scan]
    key = q[:4] if len(q) >= 4 else q
    window = max(len(q) * 2, 24)
    best, seg = 0.0, ""
    pos, count = t.find(key), 0
    while pos != -1 and count < 64:
        start = max(0, pos - window // 2)
        piece = t[start:start + window]
        r = difflib.SequenceMatcher(None, q, piece, autojunk=False).ratio()
        if r > best:
            best, seg = r, piece
        pos = t.find(key, pos + 1)
        count += 1
    return 1.0 - best, seg


def _fmt_ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")


def format_results(matches):
    if not matches:
        return "No matches found in conversation history."
    lines = ["Found %d matches in conversation history:\n" % len(matches)]
    for m in matches:
        lines.append("## %s" % m.session_title)
        lines.append("- Session ID: %s" % m.session_id)
        src = _source_line(m)
        if src:
            lines.append(src)
        lines += ["- Project: %s" % m.project,
                  "- Date: %s" % _fmt_ts(m.timestamp),
                  "- Match Type: %s" % m.match_type,
                  '- Excerpt: "%s"' % m.excerpt]
        if m.context and m.context != m.excerpt:
            lines.append("- Context: ...%s..." % m.context)
        lines.append("")
    return "\n".join(lines)


def format_trace_results(results):
    if not results:
        return "No file trace matches found in conversation history."
    lines = ["Found %d file trace matches in conversation history:\n"
             % len(results)]
    for r in results:
        lines.append("## %s" % r.session_title)
        lines.append("- Session ID: %s" % r.session_id)
        src = _source_line(r)
        if src:
            lines.append(src)
        lines += ["- Date: %s" % _fmt_ts(r.timestamp),
                  "- Status: %s" % ("First seen" if r.first_touch
                                    else "Later touch"),
                  "- File: %s" % r.file_path]
        if r.tool_name:
            lines.append("- Tool: %s" % r.tool_name)
        if r.user_prompt:
            lines.append('- Preceding User Prompt: "%s"' % r.user_prompt)
        lines.append("")
    return "\n".join(lines)


def format_multiterm_results(results):
    if not results:
        return "No sessions found in conversation history."
    lines = ["Found %d sessions in conversation history:\n" % len(results)]
    for r in results:
        lines.append("## %s" % r.session_title)
        lines.append("- Session ID: %s" % r.session_id)
        src = _source_line(r)
        if src:
            lines.append(src)
        lines += ["- Project: %s" % r.project,
                  "- Date: %s" % _fmt_ts(r.timestamp)]
        if r.term_hits:
            lines.append("- Matched terms: %s"
                         % ", ".join(t for t, _, _ in r.term_hits))
            for term, _mid, excerpt in r.term_hits:
                lines.append("  - %s: %s" % (term, excerpt))
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OpenCode data layer (read-only SQLite, V2 schema)
# ---------------------------------------------------------------------------

def get_data_dir():
    """Platform-specific OpenCode data directory."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/opencode")
    if sys.platform.startswith("win"):
        return os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "opencode")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(
        "~/.local/share")
    return os.path.join(base, "opencode")


def resolve_db_file(data_dir=None):
    """Pick the history DB: $OPENCODE_DB override, stable opencode.db,
    else newest-mtime opencode-*.db. Returns None if nothing is found."""
    data_dir = data_dir or get_data_dir()
    override = os.environ.get("OPENCODE_DB")
    if override:
        return (override if os.path.isabs(override)
                else os.path.join(data_dir, override))
    stable = os.path.join(data_dir, "opencode.db")
    if os.path.exists(stable):
        return stable
    best, best_m = None, 0
    try:
        names = os.listdir(data_dir)
    except OSError:
        return None
    for name in names:
        if not re.fullmatch(r"opencode-[^/\\]+\.db", name):
            continue
        path = os.path.join(data_dir, name)
        m = os.stat(path).st_mtime
        if m > best_m:
            best, best_m = path, m
    return best


def open_db(path):
    """Open the DB read-only and verify the V2 tables exist."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error as e:
        raise HistoryError("cannot open %s: %s" % (path, e))
    for table in V2_TABLES:
        found = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name=?", (table,)).fetchone()
        if not found:
            conn.close()
            raise HistoryError(
                "%s is missing the %s table; this looks like an old or "
                "incomplete OpenCode database. Run OpenCode once so it "
                "can migrate, or check --db points at the right file."
                % (path, table))
    return conn


def find_git_root(path):
    cur = os.path.abspath(path)
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def git_first_commit(root):
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-list", "--max-parents=0",
             "--reverse", "HEAD"], capture_output=True, text=True,
            timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    lines = out.stdout.strip().splitlines()
    return lines[0].strip() if lines else None


def project_clause(project_ids, alias="s"):
    if project_ids is None:
        return "", []
    ph = ",".join("?" * len(project_ids))
    return "AND %s.project_id IN (%s)" % (alias, ph), list(project_ids)


def resolve_project(conn, cwd, all_projects):
    """Project ids for the current location, or None for all projects.

    Git repos are keyed by their first-commit hash (the id OpenCode V2
    uses); non-git directories match sessions recorded in that exact
    directory plus the legacy "global" bucket.
    """
    if all_projects:
        return None
    root = find_git_root(cwd)
    if root:
        first = git_first_commit(root)
        if first:
            return [first]
    ids = {r[0] for r in conn.execute(
        "SELECT DISTINCT project_id FROM session_v2 WHERE directory = ?",
        (cwd,))}
    ids.add("global")
    return sorted(ids)


def list_sessions(conn, project_ids):
    clause, binds = project_clause(project_ids)
    rows = conn.execute(
        "SELECT id, project_id, title, directory, time_created, "
        "time_updated FROM session_v2 s WHERE 1=1 %s "
        "ORDER BY s.time_updated DESC" % clause, binds).fetchall()
    for r in rows:
        yield {"id": r[0], "project_id": r[1], "title": r[2] or "",
               "directory": r[3] or "", "created": r[4] or 0,
               "updated": r[5] or 0}


def _parse_data(raw):
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def iter_messages(conn, session_id, role=None):
    """Yield (id, type, data, time_created) for one session, oldest first."""
    where, binds = "WHERE session_id = ?", [session_id]
    if role:
        where += " AND type = ?"
        binds.append(role)
    rows = conn.execute(
        "SELECT id, type, data, time_created FROM session_message %s "
        "ORDER BY seq ASC, time_created ASC" % where, binds).fetchall()
    for r in rows:
        yield r[0], r[1], _parse_data(r[2]), r[3] or 0


def iter_parts(data, msg_type):
    """Yield searchable part dicts from one OpenCode message payload.

    Kinds: 'message' (plain text), 'tool' (tool name), 'toolio'
    (tool input/output), 'filepath' (patch files). User messages carry
    data.text; assistant messages carry data.content[] parts.
    """
    text = data.get("text")
    if msg_type == "user" and isinstance(text, str) and text:
        yield {"kind": "message", "text": text}
    content = data.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                t = part.get("text")
                if isinstance(t, str) and t:
                    yield {"kind": "message", "text": t,
                           "id": part.get("id")}
            elif ptype == "tool":
                name = part.get("name")
                if not isinstance(name, str) or not name:
                    continue
                state = part.get("state") or {}
                title = state.get("title")
                if not isinstance(title, str) or not title:
                    title = name
                yield {"kind": "tool", "text": name, "id": part.get("id"),
                       "title": title}
                inp = state.get("input")
                outp = state.get("output")
                io = ((json.dumps(inp) if inp else "") + " " +
                      (outp if isinstance(outp, str) else "")).strip()
                if io:
                    yield {"kind": "toolio", "text": io,
                           "id": part.get("id"), "title": title}
            elif ptype == "patch":
                files = part.get("files")
                if isinstance(files, list):
                    paths = [f for f in files if isinstance(f, str)]
                    if paths:
                        yield {"kind": "filepath",
                               "text": "\n".join(paths),
                               "id": part.get("id")}
    if msg_type != "user" and not isinstance(content, list) \
            and isinstance(text, str) and text:
        yield {"kind": "message", "text": text}
