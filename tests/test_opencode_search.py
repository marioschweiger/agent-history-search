"""Unit tests for the OpenCode backends of agent-history-search.

Ported from the opencode-history-search skill: builds a temporary SQLite
database with the V2 schema (session_v2 + session_message) and exercises
every search backend, the date filter, and the formatters. Never touches
the real history database.

Run:  python3 -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))

import history_core as core  # noqa: E402
from history_core import (HistoryError, Match, MultitermMatch,  # noqa: E402
                          TraceResult)
from opencode_search import (search_fuzzy, search_keyword,  # noqa: E402
                             search_multiterm, trace_file)
from search_history import main  # noqa: E402

BASE = 1_700_000_000_000  # 2023-11-14T22:13:20Z
MIN = 60_000
HOUR = 3_600_000
DAY = 86_400_000


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, project_id TEXT, "
        "directory TEXT, title TEXT, time_created INTEGER, "
        "time_updated INTEGER)")
    conn.execute(
        "CREATE TABLE session_message (id TEXT PRIMARY KEY, "
        "session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, "
        "time_updated INTEGER, data TEXT)")
    return conn


def seed(conn):
    conn.executemany(
        "INSERT INTO session_v2 VALUES (?,?,?,?,?,?)",
        [("s1", "proj-a", "/tmp/proj-a", "Fix docker compose networking",
          BASE, BASE + HOUR),
         ("s2", "proj-b", "/tmp/proj-b", "Migrate database schema",
          BASE + DAY, BASE + DAY + HOUR),
         ("s3", "global", "/tmp/elsewhere", "Weekly planning",
          BASE + 2 * DAY, BASE + 2 * DAY + MIN)])
    msgs = [
        ("m1", "s1", "user", 0, BASE,
         "Please fix the docker compose file, the networking is broken"),
        ("m2", "s1", "assistant", 1, BASE + 60 * MIN, json.dumps({
            "content": [
                {"type": "text", "id": "p1",
                 "text": "I'll inspect the compose file."},
                {"type": "tool", "id": "p2", "name": "write",
                 "state": {"title": "Write docker-compose.yml",
                           "input": {"path":
                                     "/tmp/proj-a/docker-compose.yml"},
                           "output": "ok"}}]})),
        ("m3", "s1", "user", 2, BASE + 120 * MIN,
         "Now also update README.md"),
        ("m4", "s1", "assistant", 3, BASE + 180 * MIN, json.dumps(
            {"content": [{"type": "patch", "id": "p3",
                          "files": ["README.md"]}]},)),
        ("m5", "s2", "user", 0, BASE + DAY + 15 * MIN,
         "Migrate the schema and handle rollback"),
        ("m6", "s2", "assistant", 1, BASE + DAY + 16 * MIN, json.dumps({
            "content": [
                {"type": "text", "text": "Running the migration now."},
                {"type": "tool", "name": "edit",
                 "state": {"input": {"filePath":
                                     "/tmp/proj-b/schema.sql"}}}]})),
        ("m7", "s3", "user", 0, BASE + 2 * DAY + MIN, "Plan the week"),
        ("m8", "s1", "assistant", 4, BASE + 240 * MIN, json.dumps({
            "content": [
                {"type": "tool", "id": "p4", "name": "write",
                 "state": {"title": "Write docker-compose.yml",
                           "input": {"path":
                                     "/tmp/proj-a/docker-compose.yml"},
                           "output": "ok"}}]})),
    ]
    conn.executemany(
        "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
        [(mid, sid, typ, seq, ts, ts,
          json.dumps({"text": t}) if typ == "user" else t)
         for mid, sid, typ, seq, ts, t in msgs])
    conn.commit()


class TestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "opencode.db")
        c = make_db(self.db_path)
        seed(c)
        c.close()
        self.conn = core.open_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class KeywordTest(TestCase):
    def search(self, *a, **kw):
        return search_keyword(self.conn, None, *a, **kw)

    def test_title_message_filepath(self):
        hits = self.search("docker")
        self.assertEqual(len(hits), 4)
        self.assertEqual({h.match_type for h in hits},
                         {"title", "message", "filepath"})
        self.assertTrue(all(h.session_id == "s1" for h in hits))

    def test_source_tagged(self):
        hits = self.search("docker")
        self.assertTrue(all(h.source == "opencode" for h in hits))

    def test_message_excerpt_and_context(self):
        hits = self.search("networking")
        self.assertEqual(len(hits), 2)  # title + message
        (hit,) = [h for h in hits if h.match_type == "message"]
        self.assertEqual(hit.excerpt, "networking")
        self.assertIn("docker compose file", hit.context)

    def test_case_insensitive_default(self):
        self.assertEqual(len(self.search("NETWORKING")), 2)

    def test_case_sensitive(self):
        self.assertEqual(len(self.search("NETWORKING",
                                         case_sensitive=True)), 0)

    def test_regex(self):
        self.assertEqual(len(self.search("netw.*ing", regex=True)), 2)
        self.assertEqual(len(self.search("netw.*ing")), 0)

    def test_tool_match(self):
        (hit,) = self.search("edit")
        self.assertEqual(hit.match_type, "tool")
        self.assertEqual(hit.excerpt, "edit")
        self.assertEqual(hit.context, "edit")

    def test_tool_name_multiple(self):
        hits = self.search("write")
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(h.match_type == "tool" for h in hits))

    def test_role_filter(self):
        self.assertEqual(len(self.search("update", role="user")), 1)
        self.assertEqual(len(self.search("update", role="assistant")), 0)

    def test_limit(self):
        self.assertEqual(len(self.search("the", limit=1)), 1)

    def test_project_scope(self):
        self.assertEqual(
            len(search_keyword(self.conn, ["proj-b"], "docker")), 0)
        self.assertEqual(
            len(search_keyword(self.conn, ["proj-a"], "docker")), 4)

    def test_invalid_regex(self):
        with self.assertRaises(HistoryError):
            self.search("(", regex=True)


class MultitermTest(TestCase):
    def test_and_same_session(self):
        (r,) = search_multiterm(self.conn, None, ["docker", "README"])
        self.assertEqual(r.session_id, "s1")
        self.assertEqual({t for t, _, _ in r.term_hits}, {"docker",
                                                          "README"})
        self.assertEqual(r.source, "opencode")

    def test_no_single_session(self):
        self.assertEqual(
            search_multiterm(self.conn, None, ["docker", "rollback"]), [])

    def test_case_insensitive(self):
        (r,) = search_multiterm(self.conn, None, ["DOCKER", "COMPOSE"])
        self.assertEqual(r.session_id, "s1")

    def test_excerpts(self):
        (r,) = search_multiterm(self.conn, None, ["migration", "schema"])
        self.assertEqual(r.session_id, "s2")
        for term, _mid, excerpt in r.term_hits:
            self.assertIn(term.lower(), excerpt.lower())

    def test_empty_terms(self):
        with self.assertRaises(HistoryError):
            search_multiterm(self.conn, None, [])


class TraceTest(TestCase):
    def test_exact_path(self):
        hits = trace_file(self.conn, None,
                          "/tmp/proj-a/docker-compose.yml")
        self.assertEqual(len(hits), 2)
        self.assertEqual({h.first_touch for h in hits}, {True, False})
        self.assertTrue(all(h.tool_name == "write" for h in hits))
        self.assertTrue(all(h.session_id == "s1" for h in hits))
        self.assertTrue(all(h.source == "opencode" for h in hits))

    def test_basename(self):
        hits = trace_file(self.conn, None, "docker-compose.yml")
        self.assertEqual(len(hits), 2)

    def test_legacy_filepath(self):
        (hit,) = trace_file(self.conn, None, "schema.sql")
        self.assertEqual(hit.session_id, "s2")
        self.assertEqual(hit.tool_name, "edit")
        self.assertEqual(hit.user_prompt,
                         "Migrate the schema and handle rollback")

    def test_patch_file(self):
        (hit,) = trace_file(self.conn, None, "README.md")
        self.assertEqual(hit.session_id, "s1")
        self.assertIsNone(hit.tool_name)
        self.assertEqual(hit.user_prompt, "Now also update README.md")

    def test_no_match(self):
        self.assertEqual(trace_file(self.conn, None, "nonexistent.xyz"),
                         [])

    def test_user_prompt_of_later_touch(self):
        hits = trace_file(self.conn, None, "docker-compose.yml")
        by_time = sorted(hits, key=lambda h: h.timestamp)
        self.assertEqual(by_time[0].user_prompt,
                         "Please fix the docker compose file, "
                         "the networking is broken")
        self.assertEqual(by_time[-1].user_prompt,
                         "Now also update README.md")


class FuzzyTest(TestCase):
    def test_exact_title(self):
        hits = search_fuzzy(self.conn, None, "weekly planning",
                            threshold=0.4)
        self.assertTrue(any(h.session_id == "s3" and
                            h.match_type == "title" for h in hits))

    def test_exact_message(self):
        hits = search_fuzzy(self.conn, None, "plan the week",
                            threshold=0.4)
        self.assertTrue(any(h.match_type == "message" for h in hits))

    def test_typo_needs_looser_threshold(self):
        self.assertEqual(
            search_fuzzy(self.conn, None, "netwrking", threshold=0.4), [])
        hits = search_fuzzy(self.conn, None, "netwrking", threshold=0.7)
        self.assertTrue(any(h.session_id == "s1" and
                            h.match_type == "title" for h in hits))

    def test_no_match(self):
        self.assertEqual(
            search_fuzzy(self.conn, None, "zzzz", threshold=0.4), [])

    def test_limit(self):
        hits = search_fuzzy(self.conn, None, "weekly planning",
                            threshold=0.4, limit=1)
        self.assertEqual(len(hits), 1)


NOW = datetime(2026, 10, 28, 15, 30, 0)


class DateFilterTest(unittest.TestCase):
    def parse(self, spec):
        return core.parse_date_filter(spec, now=NOW)

    def test_today(self):
        start, end = self.parse("today")
        self.assertEqual(
            start, core._ms(NOW.replace(hour=0, minute=0, second=0,
                                        microsecond=0)))
        self.assertEqual(end - start, core.DAY_MS - 1)

    def test_yesterday(self):
        start, end = self.parse("yesterday")
        today0 = core._ms(NOW.replace(hour=0, minute=0, second=0,
                                      microsecond=0))
        self.assertEqual(end, today0 - 1)
        self.assertEqual(end - start, core.DAY_MS - 1)

    def test_last_7_days(self):
        start, end = self.parse("last 7 days")
        self.assertEqual(
            start, core._ms((NOW - timedelta(days=7)).replace(
                hour=0, minute=0, second=0, microsecond=0)))
        self.assertEqual(end, core._ms(NOW))

    def test_last_2_months(self):
        start, _ = self.parse("last 2 months")
        self.assertEqual(start, core._ms(datetime(2026, 8, 28)))

    def test_single_day(self):
        start, end = self.parse("2026-10-01")
        self.assertEqual(start, core._ms(datetime(2026, 10, 1)))
        self.assertEqual(end, start + core.DAY_MS - 1)

    def test_month(self):
        start, end = self.parse("2026-10")
        self.assertEqual(start, core._ms(datetime(2026, 10, 1)))
        self.assertEqual(end, core._ms(datetime(2026, 11, 1)) - 1)

    def test_range(self):
        start, end = self.parse("2026-10-01 to 2026-10-05")
        self.assertEqual(start, core._ms(datetime(2026, 10, 1)))
        self.assertEqual(end, core._ms(datetime(2026, 10, 5)) +
                         core.DAY_MS - 1)

    def test_range_reversed(self):
        with self.assertRaises(HistoryError):
            self.parse("2026-10-05 to 2026-10-01")

    def test_bad_spec(self):
        with self.assertRaises(HistoryError):
            self.parse("banana")

    def test_invalid_month(self):
        with self.assertRaises(HistoryError):
            self.parse("2026-13-01")

    def test_filter_by_date(self):
        s = {"id": "s1", "project_id": "p", "title": "t",
             "directory": "d", "created": 0, "updated": 0}
        kept = core.filter_by_date([Match(s, 100, "message", "x", "x"),
                                    Match(s, 500, "message", "x", "x")],
                                   90, 200)
        self.assertEqual([m.timestamp for m in kept], [100])


class FormattingTest(TestCase):
    def _session(self):
        return {"id": "s1", "project_id": "p",
                "title": "Fix docker compose networking",
                "directory": "/tmp/proj-a", "created": 0, "updated": 0}

    def test_results_empty(self):
        self.assertEqual(core.format_results([]),
                         "No matches found in conversation history.")

    def test_results(self):
        out = core.format_results(
            [Match(self._session(), BASE, "title", "t", "t")])
        self.assertIn("Found 1 match", out)
        self.assertIn("## Fix docker compose networking", out)
        self.assertIn("- Session ID: s1", out)
        self.assertIn("- Match Type: title", out)
        self.assertNotIn("- Source:", out)

    def test_results_with_source(self):
        m = Match(self._session(), BASE, "title", "t", "t",
                  source="opencode")
        out = core.format_results([m])
        self.assertIn("- Source: opencode", out)

    def test_trace(self):
        t = TraceResult("s1", "T", BASE, True, "prompt", "write",
                        "/tmp/x.py")
        out = core.format_trace_results([t])
        self.assertIn("Found 1 file trace match", out)
        self.assertIn("First seen", out)
        self.assertIn("- File: /tmp/x.py", out)
        self.assertIn('- Preceding User Prompt: "prompt"', out)

    def test_trace_empty(self):
        self.assertEqual(core.format_trace_results([]),
                         "No file trace matches found in "
                         "conversation history.")

    def test_multiterm(self):
        r = MultitermMatch("s1", "T", BASE, "d",
                           [("a", "m1", "ex a"), ("b", "m1", "ex b")])
        out = core.format_multiterm_results([r])
        self.assertIn("Found 1 session", out)
        self.assertIn("- Matched terms: a, b", out)

    def test_multiterm_empty(self):
        self.assertEqual(core.format_multiterm_results([]),
                         "No sessions found in conversation history.")


class CoreTest(TestCase):
    def test_open_db_missing_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            c = sqlite3.connect(path)
            c.execute("CREATE TABLE session (id TEXT)")
            c.close()
            with self.assertRaises(HistoryError):
                core.open_db(path)

    def test_resolve_db_file_stable_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("opencode.db", "opencode-rc.db"):
                open(os.path.join(tmp, name), "w").close()
            self.assertEqual(core.resolve_db_file(tmp),
                             os.path.join(tmp, "opencode.db"))

    def test_resolve_db_file_newest_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = os.path.join(tmp, "opencode-a.db")
            b = os.path.join(tmp, "opencode-b.db")
            open(a, "w").close()
            open(b, "w").close()
            os.utime(a, (100, 100))
            os.utime(b, (200, 200))
            self.assertEqual(core.resolve_db_file(tmp), b)

    def test_resolve_db_file_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(core.resolve_db_file(tmp))

    def test_resolve_project_non_git(self):
        self.assertEqual(core.resolve_project(self.conn, "/tmp/elsewhere",
                                              False), ["global"])
        self.assertEqual(core.resolve_project(self.conn, "/tmp/proj-a",
                                              False),
                         ["global", "proj-a"])

    def test_resolve_project_all(self):
        self.assertIsNone(core.resolve_project(self.conn, "/tmp/proj-a",
                                               True))


class CliTest(TestCase):
    def run_cli(self, *args):
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(err):
            rc = main(["--db", self.db_path, "--source", "opencode",
                       *args])
        return rc, buf.getvalue(), err.getvalue()

    def test_keyword(self):
        rc, out, _err = self.run_cli("--query", "docker",
                                     "--all-projects")
        self.assertEqual(rc, 0)
        self.assertIn("Found 4 matches", out)
        self.assertIn("- Source: opencode", out)

    def test_trace(self):
        rc, out, _err = self.run_cli("--file-path", "schema.sql",
                                     "--all-projects")
        self.assertEqual(rc, 0)
        self.assertIn("Found 1 file trace match", out)

    def test_date_filters_out(self):
        rc, out, _err = self.run_cli("--query", "docker",
                                     "--all-projects", "--date",
                                     "2020-01-01")
        self.assertEqual(rc, 0)
        self.assertIn("No matches found", out)

    def test_nothing_to_do(self):
        rc, _out, err = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("Nothing to do", err)

    def test_missing_db_explicit_source(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            rc = main(["--db", "/nonexistent/nope.db", "--source",
                       "opencode", "--query", "x"])
        self.assertEqual(rc, 1)
        self.assertIn("no history found", err.getvalue())


if __name__ == "__main__":
    unittest.main()
