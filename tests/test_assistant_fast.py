"""Fast paths: the common asks (typos and Kannada-English too) are routed from rules, the model is never called."""
import unittest
from unittest import mock

from _home import make_home, use

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter import assistant  # noqa: E402
from jobhunter.db import DB  # noqa: E402

CASES = [
    ("any KPSC notifications?", ("govt", {"filter": "kpsc"})),
    ("kea notices closing this week", ("govt", {"filter": "kea", "closing_within_days": 7})),
    ("find ML intern jobs in Bengaluru posted this week", ("search_jobs", {"query": "ml intern", "location": "Bengaluru", "days": 7})),
    ("any genai roles at zoho?", ("search_jobs", {"query": "genai zoho", "location": "India"})),
    ("show me python developer jobs in Mysuru", ("search_jobs", {"query": "python developer", "location": "Mysuru"})),
    ("LangGraph jobs posted in the last 3 days", ("search_jobs", {"query": "langgraph", "location": "Karnataka", "days": 3})),
    ("pyhton devloper jobs blr", ("search_jobs", {"query": "python developer", "location": "Bengaluru"})),
    ("machne lerning intern", ("search_jobs", {"query": "machine learning intern", "location": "Karnataka"})),
    ("Bengaluru alli ML intern jobs", ("search_jobs", {"query": "ml intern", "location": "Bengaluru"})),
    ("top 10 jobs", ("top_jobs", {"n": 10})),
    ("ivattu hosa jobs yaavudu?", ("today", {})),
    ("what's new today", ("today", {})),
    ("govt jobs closing this week", ("govt", {"closing_within_days": 7})),
    ("government jobs closing in 3 days", ("govt", {"closing_within_days": 3})),
    ("karnataka govt notices", ("govt", {"filter": "karnataka"})),
    ("ee vaara yaavudu govt job ide?", ("govt", {})),
    ("show my stats", ("stats", {})),
    ("how am I doing?", ("stats", {})),
    ("mark 3f2a9c applied", ("set_status", {"id": "3f2a9c", "status": "applied"})),
    ("I applied to 3f2a9c", ("set_status", {"id": "3f2a9c", "status": "applied"})),
    ("3f2a9c ge apply maadide", ("set_status", {"id": "3f2a9c", "status": "applied"})),
    ("I got an interview call for 3f2a9c", ("set_status", {"id": "3f2a9c", "status": "interview"})),
    ("3f2a9c rejected me", ("set_status", {"id": "3f2a9c", "status": "rejected"})),
    ("skip 3f2a9c", ("skip", {"id": "3f2a9c"})),
    ("not interested in 3f2a9c", ("skip", {"id": "3f2a9c"})),
    ("should I apply to 3f2a9c?", ("explain_job", {"id": "3f2a9c"})),
    ("nanage fit aagutta 3f2a9c?", ("explain_job", {"id": "3f2a9c"})),
    ("put referral asked in Notes for 3f2a9c", ("sheet_set", {"id": "3f2a9c", "column": "Notes", "value": "referral asked"})),
    ("stipend for data science intern in Bengaluru", ("salary_estimate", {"role": "data science intern", "company": "", "location": "Bengaluru"})),
    ("genai engineer salary in bangalore", ("salary_estimate", {"role": "genai engineer", "company": "", "location": "Bengaluru"})),
    ("sallary for ml engineer at zoho", ("salary_estimate", {"role": "ml engineer", "company": "zoho", "location": "Bengaluru"})),
    ("data scientist sambala estu Bengaluru alli?", ("salary_estimate", {"role": "data scientist", "company": "", "location": "Bengaluru"})),
    ("how much does 3f2a9c pay?", ("salary_estimate", {"role": "", "id": "3f2a9c"})),
    ("more", ("more", {})),
]


class Fast(unittest.TestCase):
    def setUp(self):
        self.db = DB(HOME / "data" / "fast.db")
        p = mock.patch("jobhunter.llm.chat", side_effect=AssertionError("the model was called"))
        p.start()
        self.addCleanup(p.stop)

    def test_each_fast_path(self):
        for text, want in CASES:
            with self.subTest(text):
                self.assertEqual(assistant.route(text, self.db), want)

    def test_unclear_asks_still_go_to_the_model(self):
        for text in ("same in Mysuru", "only this week", "what can you do?", "tell me about Zoho", "hi",
                     "is the first one a good fit?", "and at Infosys?"):
            with self.subTest(text), self.assertRaises(AssertionError):
                assistant.route(text, self.db)

    def test_reply_uses_the_same_fast_paths(self):
        out = assistant.reply("pyhton devloper jobs blr", chat="f", db=self.db)
        self.assertIn("python developer · Bengaluru", "\n".join(out if isinstance(out, list) else [out]))


if __name__ == "__main__":
    unittest.main()
