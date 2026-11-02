"""Read-only access to pi's JSONL session history.

pi stores sessions at ~/.pi/agent/sessions/<cwd-encoded>/<ts>_<uuid>.jsonl
(override with $PI_SESSIONS_DIR or load_sessions_dir()). Each line is one
record: {"type": "session"|"message"|...}. Message records carry
message.role in {user, assistant, system, toolResult} and
message.content as a string or a list of blocks:
  user:       [{"type": "text", "text": ...}]
  assistant:  [{"type": "thinking", "thinking": ...},
               {"type": "text", "text": ...},
               {"type": "toolCall", "id", "name", "arguments"}]
  toolResult: [{"type": "text", "text": ...}]
"""
import json
import os
import re

from history_core import (DEFAULT_LIMIT, HistoryError, Match,
                          MultitermMatch, TraceResult, compile_query,
                          find_git_root, fuzzy_score, parse_iso_ms)


def get_sessions_dir(override=None):
    if override:
        return os.path.abspath(override)
    env = os.environ.get("PI_SESSIONS_DIR")
    if env:
        return os.path.abspath(env)
    return os.path.expanduser("~/.pi/agent/sessions")


def _iter_session_files(sessions_dir):
    try:
        subdirs = sorted(os.listdir(sessions_dir))
    except OSError:
        return
    for sub in subdirs:
        full = os.path.join(sessions_dir, sub)
        if not os.path.isdir(full):
            continue
        try:
            names = sorted(os.listdir(full))
        except OSError:
            continue
        for name in names:
            if name.endswith(".jsonl"):
                yield os.path.join(full, name)


def _parts_from_content(content, role):
    """Yield searchable part dicts from one pi message content."""
    if isinstance(content, str):
        if content and role != "system":
            yield {"kind": "message", "text": content}
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text")
            if isinstance(t, str) and t:
                kind = ("toolio" if role == "toolResult" else "message")
                yield {"kind": kind, "text": t, "id": block.get("id"),
                       "title": "tool result" if kind == "toolio" else None}
        elif btype == "thinking":
            t = block.get("thinking")
            if isinstance(t, str) and t:
                yield {"kind": "thinking", "text": t,
                       "id": block.get("id")}
        elif btype == "toolCall":
            name = block.get("name")
            if not isinstance(name, str) or not name:
                continue
            yield {"kind": "tool", "text": name, "id": block.get("id"),
                   "title": name}
            args = block.get("arguments")
            if args:
                try:
                    dumped = json.dumps(args, ensure_ascii=False)
                except (TypeError, ValueError):
                    dumped = str(args)
                if dumped and dumped != "null":
                    yield {"kind": "toolio", "text": dumped,
                           "id": block.get("id"), "title": name}


def _derive_title(messages):
    for _ts, role, parts in messages:
        if role != "user":
            continue
        for p in parts:
            if p["kind"] == "message":
                text = re.sub(r"\s+", " ", p["text"]).strip()
                if text:
                    if len(text) > 80:
                        return text[:80] + "…"
                    return text
    return ""


def load_session(path):
    """Parse one session JSONL file. Returns a session dict or None if
    the file has no usable header."""
    header = None
    messages = []
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            rtype = rec.get("type")
            if rtype == "session" and header is None:
                header = rec
            elif rtype == "message":
                msg = rec.get("message") or {}
                role = msg.get("role")
                if role not in ("user", "assistant", "toolResult"):
                    continue
                ts = parse_iso_ms(rec.get("timestamp")) or 0
                parts = list(_parts_from_content(msg.get("content"), role))
                if parts:
                    messages.append((ts, role, parts))
    if not header:
        return None
    created = parse_iso_ms(header.get("timestamp")) or 0
    updated = max([m[0] for m in messages] + [created])
    title = _derive_title(messages)
    return {"id": header.get("id") or os.path.basename(path),
            "title": title,
            "directory": header.get("cwd") or os.path.dirname(
                os.path.dirname(path)),
            "created": created,
            "updated": updated,
            "file": path,
            "messages": messages}


def scope_session(session_dir, cwd, all_projects):
    """True if a pi session belongs to the current location.

    Inside a git repo: sessions whose cwd is the repo root or anywhere
    under it. Else: sessions in the exact cwd, in a child of it, or in a
    parent of it (work in subdirectories and parent-level sessions both
    count).
    """
    if all_projects:
        return True
    target = os.path.abspath(cwd)
    root = find_git_root(target)
    if root:
        d = session_dir
        return d == root or d.startswith(root + os.sep)
    d = session_dir
    return (d == target
            or target.startswith(d + os.sep)
            or d.startswith(target + os.sep))


def load_sessions(sessions_dir, cwd=None, all_projects=False):
    """Yield session dicts, newest (by last activity) first."""
    cwd = os.path.abspath(cwd or os.getcwd())
    sessions = []
    for path in _iter_session_files(sessions_dir):
        s = load_session(path)
        if s and scope_session(s["directory"], cwd, all_projects):
            sessions.append(s)
    sessions.sort(key=lambda s: -s["updated"])
    return sessions


def _excerpt(text, hit, pad=100):
    idx = text.find(hit)
    cs, ce = max(0, idx - pad), min(len(text), idx + len(hit) + pad)
    return text[cs:ce]


def search_keyword(sessions, query, regex=False, case_sensitive=False,
                   limit=DEFAULT_LIMIT, role=None):
    try:
        pattern = compile_query(query, regex, case_sensitive)
    except re.error as e:
        raise HistoryError("invalid regex %r: %s" % (query, e))
    results = []
    for session in sessions:
        if len(results) >= limit:
            break
        for ts, mrole, parts in session["messages"]:
            if role and mrole != role:
                continue
            if len(results) >= limit:
                break
            for part in parts:
                if len(results) >= limit:
                    break
                kind, text = part["kind"], part["text"]
                if kind in ("message", "thinking"):
                    m = pattern.search(text)
                    if m:
                        results.append(Match(
                            session, ts, kind, m.group(0),
                            _excerpt(text, m.group(0)), None,
                            part.get("id"), source="pi"))
                elif kind == "tool" and pattern.search(text):
                    results.append(Match(
                        session, ts, "tool", text, part["title"], None,
                        part.get("id"), source="pi"))
                elif kind == "toolio":
                    m = pattern.search(text)
                    if m:
                        results.append(Match(
                            session, ts, "filepath", m.group(0),
                            part["title"], None, part.get("id"),
                            source="pi"))
    results.sort(key=lambda r: -r.timestamp)
    return results


def search_multiterm(sessions, terms, limit=DEFAULT_LIMIT):
    if not terms:
        raise HistoryError("--terms requires at least one term")
    results = []
    for session in sessions:
        if len(results) >= limit:
            break
        hits = []
        for ts, mrole, parts in session["messages"]:
            if mrole not in ("user", "assistant"):
                continue
            for part in parts:
                if len(hits) >= len(terms):
                    break
                if part["kind"] not in ("message", "thinking", "toolio"):
                    continue
                lower = part["text"].lower()
                for t in terms:
                    if len(hits) >= len(terms):
                        break
                    if any(h[0] == t for h in hits):
                        continue
                    tl = t.lower()
                    idx = lower.find(tl)
                    if idx == -1:
                        continue
                    s0, e0 = max(0, idx - 50), \
                        min(len(part["text"]), idx + len(tl) + 50)
                    hits.append((t, None, part["text"][s0:e0]))
        if len(hits) == len(terms):
            results.append(MultitermMatch(
                session["id"], session["title"], session["updated"],
                session["directory"], hits, source="pi"))
    return results


def _trace_match(path, query, is_exact):
    if is_exact:
        return path == query
    return path == query or path.endswith("/" + query)


def trace_file(sessions, file_path, limit=DEFAULT_LIMIT):
    """Find pi sessions that created or modified a file (write/edit
    tool calls). Exact paths match exactly; bare names also match when
    the path ends with '/name'."""
    norm = file_path.replace("\\", "/")
    is_exact = "/" in norm
    hits = []
    for session in sessions:
        last_user = None
        for ts, mrole, parts in session["messages"]:
            if mrole == "user":
                for p in parts:
                    if p["kind"] == "message" and p["text"].strip():
                        last_user = p["text"].strip()
                        break
            if mrole != "assistant":
                continue
            for p in parts:
                if p["kind"] != "tool":
                    continue
                if p["text"] not in ("write", "edit"):
                    continue
                # find the matching toolCall args (toolio part shares id)
                args = None
                for q in parts:
                    if q["kind"] == "toolio" and q.get("id") == p.get("id"):
                        try:
                            args = json.loads(q["text"])
                        except (ValueError, TypeError):
                            args = None
                        break
                if not isinstance(args, dict):
                    continue
                path = args.get("path")
                if isinstance(path, str) and \
                        _trace_match(path, norm, is_exact):
                    hits.append((session, ts, p["text"], path,
                                 last_user))
    if not hits:
        return []
    hits.sort(key=lambda h: h[1])
    earliest_ts = hits[0][1]
    out = []
    for session, ts, tool, path, prompt in hits[:limit]:
        out.append(TraceResult(
            session["id"], session["title"], ts, ts == earliest_ts,
            prompt, tool, path, source="pi"))
    return out


def search_fuzzy(sessions, query, threshold=0.4,
                 limit=DEFAULT_LIMIT, role=None):
    items = []
    for session in sessions:
        if session["title"]:
            items.append((session, "title", session["title"],
                          session["updated"], None, None))
        for ts, mrole, parts in session["messages"]:
            if role and mrole != role:
                continue
            for part in parts:
                kind = part["kind"]
                if kind in ("message", "thinking"):
                    items.append((session, kind, part["text"], ts, None,
                                  part.get("id")))
                elif kind == "tool":
                    items.append((session, "tool",
                                  part["text"] + " " + part["title"], ts,
                                  None, part.get("id")))
                elif kind == "toolio":
                    for path in re.findall(r"(?:/[^\s]+)+", part["text"]):
                        items.append((session, "filepath", path, ts, None,
                                      part.get("id")))
    scored = []
    for session, kind, text, ts, _mid, part_id in items:
        score, seg = fuzzy_score(query, text)
        if score <= threshold:
            scored.append((score, ts, session, kind, text, seg, part_id))
    scored.sort(key=lambda x: (x[0], -x[1]))
    results = []
    for _s, ts, session, kind, text, seg, part_id in scored[:limit]:
        if seg:
            idx = max(0, text.lower().find(seg))
            excerpt = text[max(0, idx - 20):idx + len(seg) + 80]
            context = text[max(0, idx - 100):idx + len(seg) + 200]
        else:
            excerpt, context = query, text[:200]
        results.append(Match(session, ts, kind, excerpt, context, None,
                             part_id, source="pi"))
    results.sort(key=lambda r: -r.timestamp)
    return results
