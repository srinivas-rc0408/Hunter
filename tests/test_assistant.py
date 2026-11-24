"""Free-text chat assistant with a fake LLM: the 12 example messages, confirmation before changes, truth rules,
Telegram HTML validity, 3500-char pages, and the cloud → local fallback."""
import datetime as dt
import json
import re
import unittest
from unittest import mock

from _home import use, make_home

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter import assistant, llm  # noqa: E402
from jobhunter.bot import handle  # noqa: E402
from jobhunter.db import DB, Job  # noqa: E402
from jobhunter.score import score  # noqa: E402
from test_ops import assert_telegram_html  # noqa: E402


def seed(db, title, company, desc="Python FastAPI LLMs freshers", loc="Bengaluru, Karnataka", posted=None, **kw):
    j = score(Job(title, company, loc, url="https://x.example/" + company.replace(" ", ""), source="linkedin",
                  description=desc, posted_at=posted or str(dt.date.today()), **kw))
    db.upsert(j)
    db.set_score(j.id, max(j.score, 60), j.reasons, j.flags, j.dream)
    db.c.execute("UPDATE jobs SET status='new' WHERE id=?", (j.id,))      # open, whatever the filters said
    db.c.commit()
    return j


PLANS = {   # what a good model answers for each example (first step)
    "find ML intern jobs in Bengaluru posted this week": {"tool": "search_jobs", "args": {"query": "ML intern", "location": "Bengaluru", "days": 7}},
    "top 5 GenAI roles at product companies": {"tool": "search_jobs", "args": {"query": "GenAI", "limit": 5},
                                               "note": "I can't tell product from service companies from the postings: here are the best GenAI roles."},
    "what does the Birlasoft job need that I don't have?": {"tool": "explain_job", "args": {"id": "Birlasoft"}},
    "what should i focus on this week?": {"answer": "I don't have that data."},
    r"is ([0-9a-f]{8}) a good fit for me\?": lambda m: {"tool": "explain_job", "args": {"id": m.group(1)}},
}


class FakeLLM:
    def __init__(self, plans=None, prov="nvidia"):
        self.plans, self.prov, self.calls = plans or PLANS, prov, []

    def __call__(self, system, user, **kw):
        self.calls.append((system, user, kw))
        llm.LAST.update(prov=self.prov, model="fake")
        msg = re.findall(r"^User: (.*)$", user, re.M)[-1]
        if "Tool results" in user:
            return json.dumps({"answer": "From the posting: not in the posting."})
        for k, v in self.plans.items():
            m = re.fullmatch(k, msg, re.I)
            if k.lower() == msg.lower() or m:
                return v if isinstance(v, str) else json.dumps(v(m) if callable(v) else v)
        return json.dumps({"answer": "ok"})


class Chat(unittest.TestCase):
    def setUp(self):
        self.db = DB(HOME / "data" / f"chat-{self._testMethodName}.db")
        assistant._CACHE.clear()
        assistant._LIVE.clear()
        assistant._MORE.clear()
        self.intern = seed(self.db, "ML Intern", "Acme AI", desc="Machine learning intern: Python, PyTorch")
        seed(self.db, "ML Intern", "Old Co", desc="ML intern", posted=str(dt.date.today() - dt.timedelta(days=20)))
        seed(self.db, "ML Intern", "Pune Co", desc="ML intern", loc="Pune, Maharashtra")
        self.genai = seed(self.db, "Generative AI Engineer", "Gen Labs", desc="Build GenAI apps with LLMs and RAG")
        self.birla = seed(self.db, "Data Engineer", "Birlasoft",
                          desc="You will build data pipelines. Python, Docker, Kubernetes and Spark required.")
        self.fake = FakeLLM()
        p = mock.patch("jobhunter.llm.chat", self.fake)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch("jobhunter.assistant.live_scan", return_value=0)
        self.live = q.start()
        self.addCleanup(q.stop)

    def say(self, text):
        sent = []
        out = handle(text, self.db, chat="42", progress=sent.append)
        parts = out if isinstance(out, list) else [out]
        for p in parts:
            self.assertLessEqual(len(p), 3500)
            if p != "__SHEET__":
                assert_telegram_html(self, p)
        return "\n".join(parts), sent

    # ── the 12 examples ────────────────────────────────────────────────────
    def test_find_ml_intern_jobs_in_bengaluru_this_week(self):
        out, _ = self.say("find ML intern jobs in Bengaluru posted this week")
        self.assertIn("Acme AI", out)
        self.assertNotIn("Old Co", out)                     # posted 20 days ago
        self.assertNotIn("Pune Co", out)
        for part in ("📍", "💰", "🕒", "🔗", f"/check_{self.intern.id[:8]}", "/applied_", "/skip_"):
            self.assertIn(part, out)
        self.live.assert_not_called()                       # fresh results: no live scan

    def test_top_genai_is_a_question_not_the_top_command(self):
        out, _ = self.say("top 5 GenAI roles at product companies")
        self.assertIn("Gen Labs", out)
        self.assertNotIn("Acme AI", out)
        self.assertIn("can't tell “product companies” from the postings", out)
        self.assertEqual(self.fake.calls, [])                          # obvious intent: no model

    def test_what_does_birlasoft_need_that_i_dont_have(self):
        out, _ = self.say("what does the Birlasoft job need that I don't have?")
        self.assertIn("Birlasoft", out)
        miss = re.search(r"Missing:</b> (.*)", out).group(1)
        have = re.search(r"You have:</b> (.*)", out).group(1)
        self.assertIn("Kubernetes", miss)
        self.assertIn("Python", have)
        self.assertNotIn("Python", miss)

    def test_what_do_i_miss_never_needs_the_model(self):
        for q in ("what does the Birlasoft job need that I dont have?", "what am I missing for Birlasoft?"):
            self.fake.calls.clear()
            out, _ = self.say(q)
            self.assertEqual(self.fake.calls, [], q)
            self.assertIn("Kubernetes", re.search(r"Missing:</b> (.*)", out).group(1))

    def test_weak_matches_come_after_strong_ones(self):
        weak = seed(self.db, "MLOps Engineer", "Weak Co")
        seed(self.db, "Platform Engineer", "Strong Co", desc="MLOps and Kubernetes")
        self.db.c.execute("UPDATE jobs SET score=0 WHERE id=?", (weak.id,))
        self.db.c.commit()
        out, _ = self.say("search mlops")
        self.assertLess(out.index("Strong Co"), out.index("Weak Co"))

    def test_is_it_a_good_fit(self):
        out, _ = self.say(f"is {self.genai.id[:8]} a good fit for me?")
        self.assertIn("Fit:", out)
        self.assertIn("Gen Labs", out)

    def test_salary_question_never_invents_a_number(self):
        out, _ = self.say("how much would a fresher AI engineer get at Zoho?")     # no salary data reachable in tests
        self.assertNotRegex(out, r"₹|\d+\s*LPA")
        self.assertIn("I won't guess", out)
        self.assertEqual(self.fake.calls, [])

    def test_mark_as_interview_needs_yes(self):
        jid = self.intern.id[:8]
        out, _ = self.say(f"mark {jid} as interview")
        self.assertIn("I'll mark", out)
        self.assertIn("Reply <b>yes</b>", out)
        self.assertEqual(self.db.get(self.intern.id)["status"], "new")      # not yet
        self.assertEqual(self.fake.calls, [])                               # obvious: no model needed
        with mock.patch("jobhunter.prep._what_they_do", return_value=""):
            out, _ = self.say("yes")
        self.assertEqual(self.db.get(self.intern.id)["status"], "interview")
        self.assertIn("→ <b>interview</b>", out)

    def test_yes_confirms_even_when_a_company_is_called_yes(self):
        seed(DB(), "Beautician", "Yes Madam")      # company names come from the main DB: "yes" matched it for real
        self.say(f"mark {self.intern.id[:8]} as rejected")
        with mock.patch("jobhunter.bot._company_cmd") as company:
            out, _ = self.say("yes")
        company.assert_not_called()
        self.assertEqual(self.db.get(self.intern.id)["status"], "rejected")
        self.assertIn("→ <b>rejected</b>", out)

    def test_anything_but_yes_cancels(self):
        self.say(f"mark {self.intern.id[:8]} as rejected")
        self.say("actually never mind")
        out, _ = self.say("yes")
        self.assertIn("Nothing is waiting", out)
        self.assertEqual(self.db.get(self.intern.id)["status"], "new")

    def test_model_asked_change_also_waits(self):
        self.fake.plans = {"skip the birlasoft one": {"tool": "skip", "args": {"id": "Birlasoft"}}}
        out, _ = self.say("skip the birlasoft one")
        self.assertIn("I'll mark", out)
        self.assertEqual(self.db.get(self.birla.id)["status"], "new")

    def test_send_me_the_sheet(self):
        out, _ = self.say("send me the sheet")
        self.assertEqual(out, "__SHEET__")
        self.assertEqual(self.fake.calls, [])

    def test_what_did_i_apply_to_this_week(self):
        self.db.set_status(self.genai.id, "applied")
        self.db.set_status(self.birla.id, "applied")
        self.db.c.execute("UPDATE events SET at=? WHERE job_id=?", ((dt.date.today() - dt.timedelta(days=40)).isoformat(), self.birla.id))
        self.db.c.commit()
        out, _ = self.say("what did I apply to this week?")
        self.assertIn("Gen Labs", out)
        self.assertNotIn("Birlasoft", out)
        self.assertEqual(self.fake.calls, [])

    def test_govt_closing_soon(self):
        soon = Job("Recruitment of Project Engineer (AI)", "IISc", "Bengaluru", url="https://iisc.example/x", kind="govt",
                   deadline=str(dt.date.today() + dt.timedelta(days=3)))
        later = Job("Recruitment of Scientist B", "DRDO", "Delhi", url="https://drdo.example/x", kind="govt",
                    deadline=str(dt.date.today() + dt.timedelta(days=40)))
        for g in (soon, later):
            self.db.upsert(g)
        out, _ = self.say("any govt jobs closing soon?")
        self.assertIn("IISc", out)
        self.assertNotIn("DRDO", out)

    def test_no_cancels_without_the_model(self):
        self.say(f"mark {self.intern.id[:8]} as rejected")
        out, _ = self.say("no")
        self.assertEqual(out, "Cancelled: nothing changed.")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.db.get(self.intern.id)["status"], "new")

    def test_fit_question_never_lets_the_model_judge_skills(self):
        self.fake.plans = {}                                   # a fit question must not reach the model at all
        out, _ = self.say(f"is {self.birla.id[:8]} a good fit for me?")
        self.assertEqual(self.fake.calls, [])
        self.assertIn("Python", re.search(r"You have:</b> (.*)", out).group(1))
        self.assertIn("Kubernetes", re.search(r"Missing:</b> (.*)", out).group(1))

    def test_posting_question_gets_skills_from_resume_not_the_model(self):
        self.fake.plans = {r"does .* need java\?": {"tool": "job_details", "args": {"id": self.birla.id[:8]}}}
        self.say(f"does {self.birla.id[:8]} need Java?")
        sent = self.fake.calls[-1][1]
        self.assertIn('"you_have": ["Docker", "Python"]', sent)
        self.assertIn('"you_miss": ["Kubernetes", "Spark"]', sent)

    def test_model_side_remarks_are_never_shown(self):
        self.fake.plans = {"find ml intern jobs": {"tool": "search_jobs", "args": {"query": "ML intern"},
                                                   "note": "couldn't apply the filter product companies"}}
        out, _ = self.say("find ml intern jobs")
        self.assertIn("Acme AI", out)
        self.assertNotIn("product companies", out)

    def test_intern_never_matches_international(self):
        seed(self.db, "Applied Scientist, International ML", "Big Co", desc="International machine learning team")
        out, _ = self.say("search ML intern")
        self.assertIn("Acme AI", out)
        self.assertNotIn("Big Co", out)

    def test_role_summary_skips_the_company_blurb(self):
        d = ("About Us: we care about corporate social responsibility (CSR) activities. "
             "Job Summary We are looking for an ML engineer to build RAG pipelines.")
        self.assertTrue(assistant.role_summary(d).startswith("We are looking for an ML engineer"))
        self.assertEqual(assistant.role_summary(""), "not in the posting")

    # ── behaviour ──────────────────────────────────────────────────────────
    def test_stale_results_trigger_a_live_search_first(self):
        self.db.c.execute("UPDATE jobs SET found_at=?", ((dt.datetime.now() - dt.timedelta(days=3)).isoformat(),))
        self.db.c.commit()
        out, sent = self.say("search ML intern")
        self.live.assert_called_once()
        self.assertEqual(sent, ["🔎 searching…"])
        self.assertIn("Acme AI", out)
        self.say("search ML intern")                        # same query again today: no second scan
        self.live.assert_called_once()

    def test_search_shows_5_and_more(self):
        for i in range(8):
            seed(self.db, f"Python Developer {i}", f"Py Co {i}")
        out, _ = self.say("search python developer")
        self.assertEqual(out.count("/check_"), 5)
        self.assertIn("3 more: say 'more'", out)
        out, _ = self.say("more")
        self.assertEqual(out.count("/check_"), 3)

    def test_all_llms_down_still_answers(self):
        with mock.patch("jobhunter.llm.chat", return_value=None):
            out, _ = self.say("tell me something about my search")
        self.assertEqual(out, "I can't think right now: use /help commands")

    def test_local_backup_ai_is_named(self):
        self.fake.prov = "ollama"
        out, _ = self.say("what should i focus on this week?")
        self.assertIn("(slower backup AI)", out)
        self.fake.prov = "nvidia"
        out, _ = self.say("what should i focus on this week?")
        self.assertNotIn("backup", out)

    def test_question_about_a_posting_uses_its_text(self):
        self.fake.plans = {r"does .* need java\?": {"tool": "job_details", "args": {"id": self.birla.id[:8]}}}
        out, _ = self.say(f"does {self.birla.id[:8]} need Java?")
        self.assertIn("not in the posting", out)
        self.assertIn("Kubernetes", json.dumps(self.fake.calls[-1][1]))          # the model read the posting
        self.assertIn("/check_", out)                                             # and the card is shown

    def test_at_most_4_tool_steps(self):
        class Loop(FakeLLM):
            def __call__(self, system, user, **kw):
                self.calls.append(user)
                return json.dumps({"tool": "job_details", "args": {"id": "nope"}})
        loop = Loop()
        with mock.patch("jobhunter.llm.chat", loop):
            out, _ = self.say("keep going forever")
        self.assertEqual(len(loop.calls), 4)
        self.assertIn("4 steps", out)

    def test_history_is_kept_per_chat(self):
        self.say("what should i focus on this week?")
        self.say("and next week?")
        self.assertIn("focus on this week", self.fake.calls[-1][1])                            # last turns go to the model
        self.assertEqual(len(assistant.history(self.db, "42")), 4)
        self.assertEqual(assistant.history(self.db, "other"), [])

    def test_fast_settings_reach_the_model(self):
        self.say("what should i focus on this week?")
        kw = self.fake.calls[-1][2]
        self.assertEqual(kw["temperature"], 0)
        self.assertEqual(kw["models"][0], assistant.MODELS[0])
        self.assertLessEqual(len(kw["models"]), 3)
        self.assertLess(len(self.fake.calls[-1][0]), 3000)                      # one short system prompt

    def test_stray_args_and_bad_json(self):
        self.fake.plans = {"xx": {"tool": "stats", "args": {"bogus": 1}}}
        self.assertIn("Your numbers", self.say("xx")[0])
        with mock.patch("jobhunter.llm.chat", return_value="Plain words <b>reply</b> & more"):
            out, _ = self.say("zz")
        self.assertIn("&lt;b&gt;", out)                                         # escaped, valid HTML

    def test_long_replies_are_paged(self):
        for i in range(12):
            seed(self.db, f"AI Engineer {i}", f"Long Co {i}", desc="AI " * 50)
        out = handle("top 5 AI roles at big companies", self.db, chat="42")
        for p in (out if isinstance(out, list) else [out]):
            self.assertLessEqual(len(p), 3500)


if __name__ == "__main__":
    unittest.main()
