#!/usr/bin/env python3
"""Search past conversation history from pi and/or OpenCode (read-only).

Standalone, cross-agent. Python 3 stdlib only.

Sources:
  pi       pi session JSONL files under ~/.pi/agent/sessions
           (override: --sessions-dir or $PI_SESSIONS_DIR)
  opencode OpenCode's SQLite history DB (opencode.db)
           (override: --db or $OPENCODE_DB)

Examples:
  python3 search_history.py --query "docker compose"
  python3 search_history.py --terms migration rollback
  python3 search_history.py --file-path src/auth.ts
  python3 search_history.py --query deploy --mode fuzzy
  python3 search_history.py --query ci --date "last 7 days" --role user
  python3 search_history.py --query llama --source pi
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from history_core import (DEFAULT_LIMIT, HistoryError, filter_by_date,
                          format_multiterm_results, format_results,
                          format_trace_results, get_data_dir,
                          open_db, parse_date_filter, resolve_db_file,
                          resolve_project)
import opencode_search
import pi_search


def search_opencode(args, cwd):
    """Run the requested backend over OpenCode history.
    Returns (results, formatter) or None if no DB is available."""
    db = args.db or resolve_db_file(get_data_dir())
    if not db or not os.path.exists(db):
        where = " at %s" % db if db else ""
        print("note: no OpenCode history database found%s; skipping "
              "opencode source" % where, file=sys.stderr)
        return None
    conn = open_db(db)
    try:
        project_ids = resolve_project(conn, os.path.abspath(cwd),
                                      args.all_projects)
        if args.file_path:
            results = opencode_search.trace_file(
                conn, project_ids, args.file_path, args.limit)
            return results, format_trace_results
        if args.terms:
            results = opencode_search.search_multiterm(
                conn, project_ids, args.terms, args.limit)
            return results, format_multiterm_results
        if args.mode == "fuzzy":
            results = opencode_search.search_fuzzy(
                conn, project_ids, args.query, args.fuzzy_threshold,
                args.limit, args.role)
            return results, format_results
        results = opencode_search.search_keyword(
            conn, project_ids, args.query, regex=args.regex,
            case_sensitive=args.case_sensitive, limit=args.limit,
            role=args.role)
        return results, format_results
    finally:
        conn.close()


def search_pi(args, cwd):
    """Run the requested backend over pi sessions.
    Returns (results, formatter) or None if no sessions dir."""
    sessions_dir = pi_search.get_sessions_dir(args.sessions_dir)
    if not os.path.isdir(sessions_dir):
        print("note: no pi sessions directory found at %s; skipping "
              "pi source" % sessions_dir, file=sys.stderr)
        return None
    sessions = pi_search.load_sessions(sessions_dir, cwd,
                                       args.all_projects)
    if args.file_path:
        return (pi_search.trace_file(sessions, args.file_path,
                                     args.limit),
                format_trace_results)
    if args.terms:
        return (pi_search.search_multiterm(sessions, args.terms,
                                           args.limit),
                format_multiterm_results)
    if args.mode == "fuzzy":
        return (pi_search.search_fuzzy(
            sessions, args.query, args.fuzzy_threshold, args.limit,
            args.role), format_results)
    return (pi_search.search_keyword(
        sessions, args.query, regex=args.regex,
        case_sensitive=args.case_sensitive, limit=args.limit,
        role=args.role), format_results)


def build_arg_parser():
    p = argparse.ArgumentParser(
        description="Search past conversation history from pi and/or "
                    "OpenCode (read-only).")
    p.add_argument("--query", help="keyword to search for")
    p.add_argument("--terms", nargs="+", metavar="TERM",
                   help="multi-term AND: every term must appear in one "
                        "session")
    p.add_argument("--file-path",
                   help="trace which sessions created or modified a file")
    p.add_argument("--source", choices=["pi", "opencode", "all"],
                   default="all",
                   help="which history to search (default: all)")
    p.add_argument("--all-projects", action="store_true",
                   help="search all projects on this machine")
    p.add_argument("--mode", choices=["keyword", "fuzzy"], default="keyword",
                   help="search mode (default: keyword)")
    p.add_argument("--regex", action="store_true",
                   help="treat --query as a regular expression")
    p.add_argument("--case-sensitive", action="store_true",
                   help="case-sensitive matching (default: off)")
    p.add_argument("--fuzzy-threshold", type=float, default=0.4,
                   help="fuzzy threshold, 0.0 exact .. 1.0 anything "
                        "(default 0.4)")
    p.add_argument("--date",
                   help='date filter: today, yesterday, "last N '
                        'days/weeks/months", YYYY-MM-DD, or an '
                        '"A to B" range')
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help="max results per source (default 50)")
    p.add_argument("--role", choices=["user", "assistant"],
                   help="only match messages from this role")
    p.add_argument("--db", help="override the OpenCode history DB path")
    p.add_argument("--sessions-dir",
                   help="override the pi sessions directory "
                        "(default: $PI_SESSIONS_DIR or "
                        "~/.pi/agent/sessions)")
    p.add_argument("--cwd", default=None,
                   help="project directory for scoping (default: $PWD)")
    return p


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    if not (args.query or args.terms or args.file_path):
        print("Nothing to do: pass --query, --terms, or --file-path.",
              file=sys.stderr)
        return 2
    try:
        cwd = os.path.abspath(args.cwd or os.getcwd())
        explicit = args.source != "all"
        sources = []
        if args.source in ("all", "opencode"):
            sources.append(("opencode",
                            lambda: search_opencode(args, cwd)))
        if args.source in ("all", "pi"):
            sources.append(("pi", lambda: search_pi(args, cwd)))
        merged = None
        fmt = format_results
        for name, runner in sources:
            try:
                pair = runner()
            except HistoryError as e:
                print("note: %s source failed: %s" % (name, e),
                      file=sys.stderr)
                continue
            if pair is None:
                if explicit:
                    print("error: %s source requested but no history "
                          "found there" % name, file=sys.stderr)
                    return 1
                continue
            results, fmt = pair
            if results:
                if merged is None:
                    merged = list(results)
                else:
                    merged.extend(results)
        if merged is None:
            print("No matches found in conversation history.",
                  file=sys.stdout)
            return 0
        if args.date:
            start, end = parse_date_filter(args.date)
            merged = filter_by_date(merged, start, end)
        merged.sort(key=lambda r: -r.timestamp)
        print(fmt(merged), file=sys.stdout)
        return 0
    except HistoryError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
