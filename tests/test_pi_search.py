"""Unit tests for the pi backends of agent-history-search.

Builds a temporary pi-style sessions tree (JSONL files) and exercises
session parsing, scoping, keyword/fuzzy/multi-term search, and file
traces. Never touches the real ~/.pi/agent/sessions.

Run:  python3 -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scripts"))

from history_core import HistoryError, parse_iso_ms  # noqa: E402
import pi_search  # noqa: E402
from search_history import main  # noqa: E402


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


T0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
HOUR = 3600


def write_session(base_dir, sub, name, header, messages):
    """Write one pi session JSONL file."""
    d = os.path.join(base_dir, sub)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    lines = [json.dumps(header)]
    for msg in messages:
        lines.append(json.dumps(msg))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def user_msg(mid, parent, ts, text):
    return {"type": "message", "id": mid, "parentId": parent,
            "timestamp": _iso(ts),
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]}}


def asst_msg(mid, parent, ts, blocks):
    return {"type": "message", "id": mid, "parentId": parent,
            "timestamp": _iso(ts),
            "message": {"role": "assistant", "content": blocks}}


def tool_call(tid, name, arguments):
    return {"type": "toolCall", "id": tid, "name": name,
            "arguments": arguments}


def seed_sessions(base_dir):
    """Two sessions: s1 works on docker compose in /tmp/proj-a,
    s2 plans the week in /tmp/elsewhere."""
    write_session(
        base_dir, "--tmp-proj-a--", "2026-11-01T10-00-00-000Z_s1.jsonl",
        {"type": "session", "version": 3, "id": "s1",
         "timestamp": _iso(T0), "cwd": "/tmp/proj-a"},
        [
            user_msg("m1", None, T0,
                     "Fix the docker compose networking please, "
                     "and check the README too"),
            asst_msg("m2", "m1", T0 + timedelta(minutes=2), [
                {"type": "thinking",
                 "thinking": "Need to inspect the compose file."},
                {"type": "text",
                 "text": "I will inspect the compose file."},
                tool_call("t1", "write",
                          {"path": "/tmp/proj-a/docker-compose.yml"}),
            ]),
            {"type": "message", "id": "m3", "parentId": "m2",
             "timestamp": _iso(T0 + timedelta(minutes=3)),
             "message": {"role": "toolResult", "toolName": "write",
                         "content": [{"type": "text",
                                      "text": "Wrote 12 lines to "
                                              "/tmp/proj-a/docker-compose.yml"}]}},
        ])
    write_session(
        base_dir, "--tmp-elsewhere--",
        "2026-11-02T09-00-00-000Z_s2.jsonl",
        {"type": "session", "version": 3, "id": "s2",
         "timestamp": _iso(T0 + timedelta(hours=20)),
         "cwd": "/tmp/elsewhere"},
        [
            user_msg("m4", None, T0 + timedelta(hours=20),
                     "Plan the week"),
        ])


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        seed_sessions(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def sessions(self, *a, **kw):
        return pi_search.load_sessions(self.dir, *a, **kw)

    def test_parse(self):
        (s,) = self.sessions(cwd="/tmp/proj-a")
        self.assertEqual(s["id"], "s1")
        self.assertEqual(s["directory"], "/tmp/proj-a")
        self.assertEqual(s["title"], "Fix the docker compose networking "
                                     "please, and check the README too")
        self.assertTrue(s["updated"] > s["created"])

    def test_scoping_exact(self):
        ids = {s["id"] for s in self.sessions(cwd="/tmp/proj-a")}
        self.assertEqual(ids, {"s1"})

    def test_scoping_parent(self):
        ids = {s["id"] for s in self.sessions(cwd="/tmp")}
        self.assertEqual(ids, {"s1", "s2"})

    def test_scoping_all(self):
        ids = {s["id"] for s in self.sessions(
            cwd="/tmp/nowhere", all_projects=True)}
        self.assertEqual(ids, {"s1", "s2"})

    def test_newest_first(self):
        ids = [s["id"] for s in self.sessions(cwd="/tmp")]
        self.assertEqual(ids, ["s2", "s1"])

    def test_missing_dir(self):
        self.assertEqual(pi_search.load_sessions(
            "/nonexistent/nope", cwd="/tmp"), [])


class PiKeywordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        seed_sessions(self.tmp.name)
        self.sessions = pi_search.load_sessions(
            self.tmp.name, cwd="/tmp")

    def tearDown(self):
        self.tmp.cleanup()

    def test_message_and_thinking(self):
        hits = pi_search.search_keyword(self.sessions, "compose")
        kinds = {h.match_type for h in hits}
        self.assertIn("message", kinds)
        self.assertIn("thinking", kinds)
        self.assertTrue(all(h.source == "pi" for h in hits))

    def test_tool_result_io(self):
        hits = pi_search.search_keyword(self.sessions, "Wrote 12 lines")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].match_type, "filepath")
        self.assertEqual(hits[0].session_id, "s1")

    def test_tool_name(self):
        (hit,) = pi_search.search_keyword(self.sessions, "write")
        self.assertEqual(hit.match_type, "tool")
        self.assertEqual(hit.excerpt, "write")

    def test_tool_arguments_io(self):
        hits = pi_search.search_keyword(self.sessions,
                                        "docker-compose.yml")
        self.assertTrue(any(h.match_type == "filepath" for h in hits))

    def test_case_insensitive_default(self):
        self.assertTrue(pi_search.search_keyword(self.sessions,
                                                 "COMPOSE"))

    def test_case_sensitive(self):
        self.assertFalse(
            pi_search.search_keyword(self.sessions, "COMPOSE",
                                     case_sensitive=True))

    def test_regex(self):
        self.assertTrue(pi_search.search_keyword(
            self.sessions, "comp.*network", regex=True))
        self.assertFalse(
            pi_search.search_keyword(self.sessions, "comp[os]e",
                                     regex=True))

    def test_invalid_regex(self):
        with self.assertRaises(HistoryError):
            pi_search.search_keyword(self.sessions, "(", regex=True)

    def test_role_filter(self):
        self.assertEqual(
            len(pi_search.search_keyword(self.sessions, "Plan the week",
                                         role="user")), 1)
        self.assertEqual(
            len(pi_search.search_keyword(self.sessions, "Plan the week",
                                         role="assistant")), 0)

    def test_limit(self):
        self.assertEqual(
            len(pi_search.search_keyword(self.sessions, "the", limit=1)),
            1)


class PiMultitermTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        seed_sessions(self.tmp.name)
        self.sessions = pi_search.load_sessions(self.tmp.name, cwd="/tmp")

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_session(self):
        (r,) = pi_search.search_multiterm(
            self.sessions, ["docker", "README"])
        self.assertEqual(r.session_id, "s1")
        self.assertEqual(r.source, "pi")
        self.assertEqual({t for t, _, _ in r.term_hits},
                         {"docker", "README"})

    def test_cross_session_no_match(self):
        self.assertEqual(
            pi_search.search_multiterm(self.sessions,
                                       ["docker", "Plan the week"]), [])

    def test_empty_terms(self):
        with self.assertRaises(HistoryError):
            pi_search.search_multiterm(self.sessions, [])


class PiTraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        seed_sessions(self.tmp.name)
        self.sessions = pi_search.load_sessions(self.tmp.name, cwd="/tmp")

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_path(self):
        (hit,) = pi_search.trace_file(
            self.sessions, "/tmp/proj-a/docker-compose.yml")
        self.assertEqual(hit.session_id, "s1")
        self.assertTrue(hit.first_touch)
        self.assertEqual(hit.tool_name, "write")
        self.assertEqual(hit.user_prompt,
                         "Fix the docker compose networking please, "
                         "and check the README too")
        self.assertEqual(hit.source, "pi")

    def test_basename(self):
        (hit,) = pi_search.trace_file(self.sessions,
                                      "docker-compose.yml")
        self.assertEqual(hit.session_id, "s1")

    def test_no_match(self):
        self.assertEqual(pi_search.trace_file(self.sessions,
                                              "nope.xyz"), [])


class PiFuzzyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        seed_sessions(self.tmp.name)
        self.sessions = pi_search.load_sessions(self.tmp.name, cwd="/tmp")

    def tearDown(self):
        self.tmp.cleanup()

    def test_title(self):
        hits = pi_search.search_fuzzy(self.sessions, "plan the week",
                                      threshold=0.4)
        self.assertTrue(any(h.match_type == "title" and
                            h.session_id == "s2" for h in hits))

    def test_typo_needs_looser_threshold(self):
        self.assertEqual(
            pi_search.search_fuzzy(self.sessions, "netwrking",
                                   threshold=0.4), [])
        hits = pi_search.search_fuzzy(self.sessions, "netwrking",
                                      threshold=0.8)
        self.assertTrue(any(h.session_id == "s1" for h in hits))


class ParseIsoTest(unittest.TestCase):
    def test_z_suffix(self):
        expected = int(datetime(2026, 11, 2, 19, 1, 27, 617000,
                                tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(parse_iso_ms("2026-11-02T19:01:27.617Z"),
                         expected)

    def test_offset(self):
        expected = int(datetime(2026, 11, 2, 20, 1, 27,
                                tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(
            parse_iso_ms("2026-11-02T21:01:27+01:00"), expected)

    def test_epoch_seconds(self):
        self.assertEqual(parse_iso_ms(1_700_000_000),
                         1_700_000_000_000)

    def test_epoch_millis(self):
        self.assertEqual(parse_iso_ms(1_700_000_000_123),
                         1_700_000_000_123)

    def test_bad(self):
        self.assertIsNone(parse_iso_ms("banana"))
        self.assertIsNone(parse_iso_ms(None))
        self.assertIsNone(parse_iso_ms(""))


class PiCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        seed_sessions(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(err):
            rc = main(["--source", "pi", "--sessions-dir", self.tmp.name,
                       "--cwd", "/tmp", *args])
        return rc, buf.getvalue(), err.getvalue()

    def test_keyword(self):
        rc, out, _err = self.run_cli("--query", "compose")
        self.assertEqual(rc, 0)
        self.assertIn("matches", out)
        self.assertIn("- Source: pi", out)

    def test_trace(self):
        rc, out, _err = self.run_cli("--file-path", "docker-compose.yml")
        self.assertEqual(rc, 0)
        self.assertIn("Found 1 file trace match", out)
        self.assertIn("- Source: pi", out)

    def test_missing_sessions_dir_explicit(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            rc = main(["--source", "pi",
                       "--sessions-dir", "/nonexistent/nope",
                       "--query", "x"])
        self.assertEqual(rc, 1)
        self.assertIn("no history found", err.getvalue())

    def test_all_mode_skips_missing_opencode(self):
        """--source all with a fake (empty) opencode dir: opencode is
        skipped with a note, pi results still printed."""
        with tempfile.TemporaryDirectory() as empty:
            buf, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(err):
                rc = main(["--source", "all",
                           "--db", os.path.join(empty, "nope.db"),
                           "--sessions-dir", self.tmp.name,
                           "--cwd", "/tmp", "--query", "compose"])
            self.assertEqual(rc, 0)
            self.assertIn("matches", buf.getvalue())
            self.assertIn("skipping opencode source", err.getvalue())


if __name__ == "__main__":
    unittest.main()
