"""Grounding: invented pay, skills, dates and companies in a model's answer are cut; faithful answers pass."""
import json
import unittest
from unittest import mock

from _home import make_home, use

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter.grounding import GONE, check  # noqa: E402

POSTING = json.dumps({"title": "ML Intern", "company": "Gen Labs", "pay": "₹12,000 a month", "deadline": "2026-11-30",
                      "description": "Python and PyTorch. Stipend 3 LPA after conversion. 2 years bond. 20% travel."})


class Check(unittest.TestCase):
    def test_faithful_answer_is_unchanged(self):
        ans = ("Gen Labs pays 12k a month. It asks for Python and PyTorch. Apply by 30 Nov. "
               "After conversion it's ₹3,00,000 a year, with a 2 years bond and 20% travel.")
        self.assertEqual(check(ans, [POSTING]), ans)

    def test_invented_facts_are_replaced(self):
        for lie in ("It pays ₹25,000 a month.", "It needs Kubernetes.", "The last date is 15 December.",
                    "It's like the role at Infosys.", "Acme Robotics Labs runs it.", "You'd need 3 years of experience."):
            out = check("Gen Labs is hiring. " + lie, [POSTING])
            self.assertEqual(out, f"Gen Labs is hiring.\n(1 line left out: {GONE} in the data I have)", lie)
        self.assertEqual(check("It pays ₹99,999.", [POSTING]), GONE)          # nothing left → just "not found"

    def test_advice_may_name_skills_to_learn(self):
        tip = "Revise DSA and practise system design. Learn Kubernetes and Docker basics."
        self.assertEqual(check(tip, ["", "Python PyTorch"], skills=False), tip)          # no tool used: advice
        self.assertIn(GONE, check("Learn Kubernetes. It pays 9 LPA.", [""], skills=False))  # numbers still checked

    def test_numbers_match_loosely(self):
        for a, src in (("12,000", "12000"), ("12k", "12,000"), ("3 LPA", "300000"), ("3 lakh", "3,00,000"),
                       ("3.5 LPA", "₹350000"), ("on 2026-11-30", "deadline 30/11/2026")):
            self.assertEqual(check(f"It is {a}.", [src]), f"It is {a}.", a)

    def test_job_ids_are_not_numbers(self):
        self.assertEqual(check("See /show_3f2a9c11 for it.", ["{}"]), "See /show_3f2a9c11 for it.")


class Wired(unittest.TestCase):
    """The chat assistant's model-written answers pass through check() with that turn's tool results."""

    def setUp(self):
        import datetime as dt
        from jobhunter import assistant
        from jobhunter.db import DB, Job
        self.db = DB(HOME / "data" / "ground.db")
        assistant._CACHE.clear()
        j = Job("ML Intern", "Gen Labs", "Bengaluru", url="https://x.example/g", source="linkedin",
                description="Python and PyTorch. Stipend ₹12,000 a month.", posted_at=str(dt.date.today()))
        self.db.upsert(j)
        self.id = j.id[:8]

    def ask(self, answer):
        from jobhunter.bot import handle
        def fake(system, user, **kw):
            if "Tool results" in user:
                return json.dumps({"answer": answer})
            return json.dumps({"tool": "job_details", "args": {"id": self.id}})
        with mock.patch("jobhunter.llm.chat", fake):
            out = handle(f"does {self.id} need Java?", self.db, chat="g")
        return "\n".join(out if isinstance(out, list) else [out])

    def test_invented_salary_is_cut(self):
        out = self.ask("It needs Python. It pays ₹40,000 a month.")
        self.assertIn("It needs Python.", out)
        self.assertNotIn("40,000", out)
        self.assertIn(GONE, out)

    def test_faithful_answer_is_shown(self):
        self.assertIn("It needs Python and PyTorch, and pays ₹12,000 a month.",
                      self.ask("It needs Python and PyTorch, and pays ₹12,000 a month."))

    def test_plain_answer_without_tools_is_checked_too(self):
        from jobhunter.bot import handle
        with mock.patch("jobhunter.llm.chat", return_value=json.dumps({"answer": "Freshers get 6 LPA at Zoho."})):
            out = handle("what do freshers earn?", self.db, chat="g2")
        self.assertNotIn("6 LPA", "\n".join(out if isinstance(out, list) else [out]))


if __name__ == "__main__":
    unittest.main()
