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
from jobhunter.apply import fields  # noqa: E402

CHAT = evals.cases("chat")
FORMS = evals.cases("forms")


def perfect(system, user, **kw):
    """A model that always picks the expected tool for the eval message."""
    if "User: " not in user:                         # a form question
        return "unknown"
    msg = re.findall(r"^User: (.*)$", user, re.M)[-1]
    c = next(c for c in CHAT if c["text"] == msg)
    return json.dumps({"answer": "Hi"} if c["tool"] == "none" else {"tool": c["tool"], "args": c["args"]})


class Sets(unittest.TestCase):
    def test_chat_set(self):
        self.assertEqual(len(CHAT), 60)
        cats = Counter(c["cat"] for c in CHAT)
        for cat in ("search", "fit", "salary", "status", "sheet", "govt", "followup", "typo", "mixed"):
            self.assertGreaterEqual(cats[cat], 6, cat)
        self.assertLessEqual(set(cats), {"search", "fit", "salary", "status", "sheet", "govt", "followup", "typo",
                                         "mixed", "general"})
        for c in CHAT:
            self.assertIn(c["tool"], {*assistant.TOOLS, "none", "more"}, c)
            self.assertIsInstance(c["args"], dict)

    def test_forms_set(self):
        self.assertEqual(len(FORMS), 40)
        keys = {k for k, _ in fields.RULES} | {"unknown"}
        for c in FORMS:
            self.assertIn(c["type"], ("text", "select", "radio", "checkbox", "number"), c)
            self.assertIn(c["field"], keys, c)
            if c.get("private"):
                self.assertIn(c["field"], ("diversity", "unknown"))
        self.assertGreaterEqual(sum(bool(c.get("private")) for c in FORMS), 6)


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
        self.assertIn("never_invents", out)


class Forms(unittest.TestCase):
    def test_model_says_unknown(self):
        with mock.patch("jobhunter.llm.chat", return_value="unknown"):
            r = evals.run_forms()
        self.assertEqual(r["never_invents"], 100, r["misses"])
        self.assertGreaterEqual(r["right_field"], 90, r["misses"])

    def test_oracle_model(self):
        by_q = {c["q"]: c["field"] for c in FORMS}
        oracle = lambda s, u, **k: by_q[re.search(r"^Question: (.*)$", u, re.M).group(1)]   # noqa: E731
        with mock.patch("jobhunter.llm.chat", oracle):
            r = evals.run_forms()
        self.assertEqual(r["never_invents"], 100)
        self.assertGreaterEqual(r["right_field"], 90, r["misses"])

    def test_a_model_that_answers_never_invents(self):
        for reply in ("Yes", "Male", "OBC", "gender", "first_name", "5 years"):
            with mock.patch("jobhunter.llm.chat", return_value=reply):
                self.assertEqual(evals.run_forms()["never_invents"], 100, reply)

    def test_private_questions_are_diversity_and_never_guessed(self):
        self.assertIn("diversity", fields._NEVER_GUESS)
        for q in ("Gender", "Sex", "Marital status", "Category (General / OBC / SC / ST)", "Religion", "Caste",
                  "Do you have a disability?", "Are you a PwBD candidate?"):
            self.assertEqual(fields.classify(q, "select"), "diversity", q)
        a = fields.answer("Gender", "select", options=["Male", "Female"])
        self.assertEqual((a.value, a.basis), (None, "none"))

    def test_category_value_only_answers_a_category_question(self):
        prof = {**fields.config.cfg("profile"), "voluntary": {"diversity_category": "General"}}
        with mock.patch("jobhunter.config.cfg", side_effect=lambda n: prof if n == "profile" else {}):
            self.assertEqual(fields.answer("Caste category").value, "General")
            self.assertIsNone(fields.answer("Do you have a disability?").value)
            self.assertIsNone(fields.answer("Gender").value)

    def test_commute_question(self):
        self.assertEqual(fields.classify("Are you comfortable commuting to our Bengaluru office?", "radio"), "wfo")
        self.assertEqual(fields.classify("Are you comfortable commuting to this job's location?", "radio"), "unknown")


class Drift(unittest.TestCase):
    def setUp(self):
        evals._file().write_text(json.dumps({"chat": {"a": 96}, "best": 96, "last": 96}))
        self.addCleanup(lambda: evals._file().unlink(missing_ok=True))
        forms = {"right_field": 95, "never_invents": 100, "total": 40, "misses": []}
        for p in (mock.patch("jobhunter.llm.available", return_value=True),
                  mock.patch.object(evals, "_order", return_value=["a", "b", "c"]),
                  mock.patch.object(evals, "run_forms", return_value=forms)):
            p.start()
            self.addCleanup(p.stop)

    def run_with(self, scores):
        with mock.patch.object(evals, "run_chat", side_effect=lambda models=None, **k: {"accuracy": scores[models[0]]}):
            return evals.drift_check()

    def test_drop_switches_to_the_first_model_within_5_points(self):
        ok, text = self.run_with({"a": 88, "b": 80, "c": 95})
        self.assertFalse(ok)
        self.assertEqual(text, "⚠ chat accuracy fell 96→88% on a; switched to c (95%)")
        self.assertEqual(evals.saved()["models"][0], "c")
        self.assertEqual(assistant._models()[0], "c")                     # the assistant uses the saved order

    def test_small_dip_keeps_the_model(self):
        ok, text = self.run_with({"a": 93, "b": 80, "c": 95})
        self.assertTrue(ok)
        self.assertIn("chat 93% on a", text)
        self.assertNotIn("models", evals.saved())

    def test_no_live_model_skips(self):
        with mock.patch("jobhunter.llm.available", return_value=False):
            self.assertEqual(evals.drift_check(), (True, "evals skipped: no live model"))


if __name__ == "__main__":
    unittest.main()
