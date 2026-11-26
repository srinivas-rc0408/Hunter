"""Eval sets (tests/evals/*.jsonl) and their runner: routing accuracy, form reading, model drift switching."""
import json
import re
import unittest
from collections import Counter
from unittest import mock

from _home import make_home, use

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter import assistant, evals  # noqa: E402

CHAT = evals.cases("chat")


def perfect(system, user, **kw):
    """A model that always picks the expected tool for the eval message."""
    if "User: " not in user:                         # a form question
        return "unknown"
    msg = re.findall(r"^User: (.*)$", user, re.M)[-1]
    c = next(c for c in CHAT if c["text"] == msg)
    return json.dumps({"answer": "Hi"} if c["tool"] == "none" else {"tool": c["tool"], "args": c["args"]})


class Sets(unittest.TestCase):
    def test_chat_set(self):
        self.assertGreaterEqual(len(CHAT), 40)
        cats = Counter(c["cat"] for c in CHAT)
        for cat in ("search", "fit", "salary", "status", "sheet", "govt", "followup", "typo", "mixed"):
            self.assertGreaterEqual(cats[cat], 6, cat)
        self.assertLessEqual(set(cats), {"search", "fit", "salary", "status", "sheet", "govt", "followup", "typo",
                                         "mixed", "general"})
        for c in CHAT:
            self.assertIn(c["tool"], {*assistant.TOOLS, "none", "more"}, c)
            self.assertIsInstance(c["args"], dict)


class Chat(unittest.TestCase):
    def test_perfect_model_scores_at_least_95(self):
        with mock.patch("jobhunter.llm.chat", perfect):
            r = evals.run_chat()
        self.assertGreaterEqual(r["accuracy"], 95, r["misses"])

    def test_fast_paths_alone_get_20(self):
        with mock.patch("jobhunter.llm.chat", return_value="garbage, not JSON"):
            r = evals.run_chat()
        self.assertGreaterEqual(r["right"], 20, r["misses"])
        wrong = [m for m in r["misses"] if m["got"] is not None]       # a fast path never picks the wrong tool
        self.assertEqual(wrong, [])

    def test_route_never_runs_a_write_tool(self):
        with mock.patch.object(assistant, "_finish", side_effect=AssertionError), \
                mock.patch("jobhunter.llm.chat", return_value='{"tool": "skip", "args": {"id": "3f2a9c"}}'):
            from jobhunter.db import DB
            db = DB(HOME / "data" / "route.db")
            self.assertEqual(assistant.route("skip that Birlasoft one", db), ("skip", {"id": "3f2a9c"}))
            self.assertEqual(assistant.route("mark 3f2a9c applied", db), ("set_status", {"id": "3f2a9c", "status": "applied"}))
            self.assertEqual(db.c.execute("SELECT COUNT(*) FROM chat_pending").fetchone()[0], 0)

    def test_report_table(self):
        with mock.patch("jobhunter.llm.chat", perfect):
            out = evals.report()
        self.assertIn("overall", out)

    def test_a_wrong_model_is_scored_wrong(self):
        """The scorer itself: a model that always says top_jobs can only be right where top_jobs is expected."""
        with mock.patch("jobhunter.llm.chat", return_value='{"tool": "top_jobs", "args": {}}'), \
                mock.patch.object(assistant, "_fast", return_value=None):
            r = evals.run_chat()
        self.assertEqual(r["right"], sum(c["tool"] == "top_jobs" for c in CHAT))

    def test_wrong_args_are_a_miss(self):
        c = next(c for c in CHAT if c["tool"] == "search_jobs" and c["args"].get("location"))
        r = evals.run_chat([c], reply_fn=lambda t, db, chat: ("search_jobs", {**c["args"], "location": "Nowhere"}))
        self.assertEqual(r["right"], 0)


if __name__ == "__main__":
    unittest.main()
