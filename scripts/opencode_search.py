"""OpenCode search backends (keyword, fuzzy, multi-term, file trace).

Read-only over the SQLite DB opened by history_core.open_db().
Ported from the opencode-history-search skill.
"""
import re

from history_core import (DEFAULT_LIMIT, HistoryError, Match,
                          MultitermMatch, TraceResult, _parse_data,
                          compile_query, fuzzy_score, iter_messages,
                          iter_parts, list_sessions, project_clause)


def _like_escape(term):
    return term.replace("\\", "\\\\").replace("%", "\\%").replace(
        "_", "\\_")


def search_keyword(conn, project_ids, query, regex=False,
                   case_sensitive=False, limit=DEFAULT_LIMIT, role=None):
    """Regex search over titles, message text, tool names, tool I/O, and
    patch file paths."""
    try:
        pattern = compile_query(query, regex, case_sensitive)
    except re.error as e:
        raise HistoryError("invalid regex %r: %s" % (query, e))
    results = []
    for session in list_sessions(conn, project_ids):
        if len(results) >= limit:
            break
        if pattern.search(session["title"]):
            results.append(Match(session, session["updated"], "title",
                                 session["title"], session["title"],
                                 source="opencode"))
        for msg_id, mtype, data, ts in iter_messages(conn, session["id"],
                                                     role):
            if len(results) >= limit:
                break
            for part in iter_parts(data, mtype):
                if len(results) >= limit:
                    break
                kind, text = part["kind"], part["text"]
                if kind == "message" and pattern.search(text):
                    hit = pattern.search(text).group(0)
                    idx = text.find(hit)
                    cs, ce = max(0, idx - 100), \
                        min(len(text), idx + len(hit) + 100)
                    results.append(Match(session, ts, "message", hit,
                                         text[cs:ce], msg_id,
                                         part.get("id"), source="opencode"))
                elif kind == "tool" and pattern.search(text):
                    results.append(Match(session, ts, "tool", text,
                                         part["title"], msg_id,
                                         part.get("id"), source="opencode"))
                elif kind == "toolio":
                    m = pattern.search(text)
                    if m:
                        results.append(Match(
                            session, ts, "filepath", m.group(0),
                            part["title"], msg_id, part.get("id"),
                            source="opencode"))
                elif kind == "filepath":
                    for line in text.split("\n"):
                        if len(results) >= limit:
                            break
                        if pattern.search(line):
                            results.append(Match(
                                session, ts, "filepath", line,
                                "Modified file: %s" % line, msg_id,
                                part.get("id"), source="opencode"))
    results.sort(key=lambda r: -r.timestamp)
    return results


def search_multiterm(conn, project_ids, terms, limit=DEFAULT_LIMIT):
    """Every term must appear in the same session."""
    if not terms:
        raise HistoryError("--terms requires at least one term")
    conditions = ", ".join(
        "MAX(CASE WHEN m.data LIKE ? ESCAPE '\\' OR s.title LIKE ? "
        "ESCAPE '\\' THEN 1 ELSE 0 END) AS match_%d" % i
        for i in range(len(terms)))
    having = " AND ".join("match_%d = 1" % i for i in range(len(terms)))
    clause, pbinds = project_clause(project_ids)
    sql = ("SELECT s.id, s.title, s.directory, s.time_updated, "
           + conditions +
           " FROM session_v2 s JOIN session_message m ON m.session_id = s.id "
           "WHERE 1=1 %s GROUP BY s.id HAVING %s "
           "ORDER BY s.time_updated DESC LIMIT ?" % (clause, having))
    binds = []
    for t in terms:
        escaped = "%" + _like_escape(t) + "%"
        binds += [escaped, escaped]
    binds += pbinds + [limit]
    rows = conn.execute(sql, binds).fetchall()
    if not rows:
        return []
    ids = [r[0] for r in rows]
    follow = conn.execute(
        "SELECT session_id, id, data FROM session_message "
        "WHERE session_id IN (%s) "
        "AND (type = 'user' OR type = 'assistant') "
        "ORDER BY seq ASC, time_created ASC"
        % ",".join("?" * len(ids)), ids).fetchall()
    by_session = {}
    for sid, mid, raw in follow:
        by_session.setdefault(sid, []).append((mid, raw or ""))
    results = []
    for r in rows:
        hits = []
        for mid, raw in by_session.get(r[0], []):
            if len(hits) >= len(terms):
                break
            lower = raw.lower()
            for t in terms:
                if len(hits) >= len(terms):
                    break
                if any(h[0] == t for h in hits):
                    continue
                tl = t.lower()
                idx = lower.find(tl)
                if idx == -1:
                    continue
                s0, e0 = max(0, idx - 50), min(len(raw), idx + len(tl) + 50)
                hits.append((t, mid, raw[s0:e0]))
        results.append(MultitermMatch(r[0], r[1] or "", r[3] or 0,
                                      r[2] or "", hits, source="opencode"))
    return results


def _trace_match(path, query, is_exact):
    if is_exact:
        return path == query
    return path == query or path.endswith("/" + query)


def trace_file(conn, project_ids, file_path, limit=DEFAULT_LIMIT):
    """Find sessions that created or modified a file. Paths containing
    '/' match exactly; bare names also match when the path ends with
    '/name'."""
    norm = file_path.replace("\\", "/")
    is_exact = "/" in norm
    clause, pbinds = project_clause(project_ids)
    rows = conn.execute(
        "SELECT s.id, s.title, m.id, m.time_created, m.data "
        "FROM session_v2 s JOIN session_message m ON m.session_id = s.id "
        "WHERE m.type = 'assistant' AND m.data LIKE ? ESCAPE '\\' %s "
        "ORDER BY m.time_created DESC, m.seq DESC" % clause,
        ["%" + _like_escape(norm) + "%"] + pbinds).fetchall()
    seen, deduped = set(), []
    for sid, title, mid, ts, raw in rows:
        content = _parse_data(raw).get("content")
        if not isinstance(content, list):
            continue
        for p in content:
            if not isinstance(p, dict):
                continue
            touched, tool = None, None
            if p.get("type") == "tool" and p.get("name") in ("write", "edit"):
                inp = (p.get("state") or {}).get("input") or {}
                if isinstance(inp.get("path"), str):
                    touched = inp["path"]
                elif isinstance(inp.get("filePath"), str):
                    touched = inp["filePath"]
                tool = p.get("name")
            elif (p.get("type") == "patch"
                  and isinstance(p.get("files"), list)):
                for f in p["files"]:
                    if isinstance(f, str) and _trace_match(f, norm,
                                                           is_exact):
                        touched = f
                        break
            if touched and _trace_match(touched, norm, is_exact):
                key = (sid, mid, touched)
                if key not in seen:
                    seen.add(key)
                    deduped.append((sid, title or "", ts, tool, touched))
    if not deduped:
        return []
    earliest_idx = min(range(len(deduped)), key=lambda i: deduped[i][2])
    prompts = {}

    def user_prompt(sid, ts):
        if (sid, ts) not in prompts:
            row = conn.execute(
                "SELECT data FROM session_message WHERE session_id = ? "
                "AND type = 'user' AND time_created < ? "
                "ORDER BY seq DESC LIMIT 1", (sid, ts)).fetchone()
            text = None
            if row:
                text = _parse_data(row[0]).get("text") or None
            prompts[(sid, ts)] = text
        return prompts[(sid, ts)]

    out = []
    for i, (sid, title, ts, tool, f) in enumerate(deduped[:limit]):
        out.append(TraceResult(sid, title, ts, i == earliest_idx,
                               user_prompt(sid, ts), tool, f,
                               source="opencode"))
    return out


def search_fuzzy(conn, project_ids, query, threshold=0.4,
                 limit=DEFAULT_LIMIT, role=None):
    """Rank titles, messages, tool names, and paths by fuzzy similarity
    (difflib stands in for fuse.js)."""
    items = []
    for session in list_sessions(conn, project_ids):
        items.append((session, "title", session["title"],
                      session["updated"], None, None))
        for msg_id, mtype, data, ts in iter_messages(conn, session["id"],
                                                     role):
            for part in iter_parts(data, mtype):
                kind, text = part["kind"], part["text"]
                if kind == "message":
                    items.append((session, "message", text, ts, msg_id,
                                  part.get("id")))
                elif kind == "tool":
                    extra = (" " + part["title"]
                             if part["title"] != text else "")
                    items.append((session, "tool", text + extra, ts,
                                  msg_id, part.get("id")))
                elif kind == "toolio":
                    for path in re.findall(r"(?:/[^\s]+)+", text):
                        items.append((session, "filepath", path, ts,
                                      msg_id, part.get("id")))
                elif kind == "filepath":
                    for line in text.split("\n"):
                        items.append((session, "filepath", line, ts,
                                      msg_id, part.get("id")))
    scored = []
    for session, kind, text, ts, msg_id, part_id in items:
        score, seg = fuzzy_score(query, text)
        if score <= threshold:
            scored.append((score, ts, session, kind, text, seg, msg_id,
                           part_id))
    scored.sort(key=lambda x: (x[0], -x[1]))
    results = []
    for _s, ts, session, kind, text, seg, msg_id, part_id in scored[:limit]:
        if seg:
            idx = max(0, text.lower().find(seg))
            excerpt = text[max(0, idx - 20):idx + len(seg) + 80]
            context = text[max(0, idx - 100):idx + len(seg) + 200]
        else:
            excerpt, context = query, text[:200]
        results.append(Match(session, ts, kind, excerpt, context, msg_id,
                             part_id, source="opencode"))
    results.sort(key=lambda r: -r.timestamp)
    return results
