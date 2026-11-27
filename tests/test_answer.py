"""v2.7 grounded answers: the job a question names is read before the model answers, the parts of a long posting
that answer it, the job's own page for deadline / pay / eligibility (24 h cache, a browser only for JS pages),
job-id citations with cards = exactly the cited jobs, the scam warning, the stronger model for analysis,
"close tomorrow", Confirm / Cancel buttons, "typing…" on every message."""
import datetime as dt
import json
import re
import unittest
from unittest import mock

from _home import make_home, use

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter import answer, assistant, outcomes  # noqa: E402
from jobhunter.bot import handle  # noqa: E402
from jobhunter.db import DB, Job  # noqa: E402
from jobhunter.score import score  # noqa: E402

REAL_PAGE = answer.page

STACK = "Our stack: Python, TensorFlow and Kubeflow on Google Cloud."
LONG = ("About us. " + "We build robots for warehouses and care deeply about safety. " * 60 + "Experience: 0–2 years. " + STACK)


def seed(db, title, company, desc="Python ML", deadline=None, flags=(), url=None, **kw):
    j = score(Job(title, company, "Bengaluru, Karnataka", url=url or f"https://jobs.example/{company}/{title}".replace(" ", ""),
                  source="linkedin", description=desc, posted_at=str(dt.date.today()), deadline=deadline, **kw))
    db.upsert(j)
    db.set_score(j.id, max(j.score, 60), j.reasons + [f"⚠ asks “fee”: check before applying" for f in flags if f == "scam?"],
                 list(flags), j.dream)
    db.c.execute("UPDATE jobs SET status='new' WHERE id=?", (j.id,))
    db.c.commit()
    return db.get(j.id)


class Model:
    """A fake model: answers once it has tool results, else what `first` says. Records every call."""

    def __init__(self, ans, first=None):
        self.ans, self.first, self.calls = ans, first or {"answer": "not in the posting"}, []

    def __call__(self, system, user, **kw):
        self.calls.append((user, kw))
        return json.dumps({"answer": self.ans(user) if callable(self.ans) else self.ans} if "Tool results" in user else self.first)


class Base(unittest.TestCase):
    def setUp(self):
        self.db = DB(HOME / "data" / f"ans-{self._testMethodName}.db")
        assistant._CACHE.clear()
        self.artha = seed(self.db, "Machine Learning Engineer", "Artha Labs (part of Attention Tag)", desc=LONG)
        self.uni = seed(self.db, "Full Stack AI Engineer Intern", "Uniphore", desc="6 months. React and Python.")
        self.ciq = seed(self.db, "SDE Intern", "CommerceIQ", desc="LLM APIs, React, Node.")
        self.agentic = seed(self.db, "Agentic AI Intern", "Agentic Universe", desc="Agents.")
        self.inspot = seed(self.db, "Agentic AI Intern (Junior/Fresher)", "Inspot IT", desc="Python or JavaScript.")
        for p in (mock.patch("jobhunter.assistant.live_scan", return_value=0),
                  mock.patch("jobhunter.answer.page", side_effect=AssertionError("no page read expected"))):
            p.start()
            self.addCleanup(p.stop)

    def say(self, text, model):
        with mock.patch("jobhunter.llm.chat", model):
            out = handle(text, self.db, chat="42", progress=lambda s: None)
        return "\n\n".join(out if isinstance(out, list) else [out])


class Named(Base):
    def test_jobs_are_found_by_id_by_company_and_title_and_in_the_order_named(self):
        ids = lambda t: [j["id"] for j in answer.mentioned(self.db, t)]  # noqa: E731
        self.assertEqual(ids(f"which company posted {self.uni['id'][:8]}?"), [self.uni["id"]])
        self.assertEqual(ids("what's the stack for the Artha Labs machine learning engineer job?"), [self.artha["id"]])
        self.assertEqual(ids("compare the Uniphore intern and the CommerceIQ SDE intern"), [self.uni["id"], self.ciq["id"]])
        self.assertEqual(ids("interview process for the Inspot IT agentic AI intern?"), [self.inspot["id"]])  # not "Agentic Universe"
        self.assertEqual(ids("find ML intern jobs in Bengaluru"), [])

    def test_a_long_posting_keeps_the_part_that_answers(self):
        got = answer.passages(LONG, "what's the tech stack?")
        self.assertLessEqual(len(got), 2600)
        self.assertIn(STACK, got)                                      # the last sentence, 3,000+ characters in
        self.assertEqual(answer.passages("short posting", "x"), "short posting")

    def test_the_model_sees_the_named_posting_before_it_answers(self):
        m = Model(lambda u: "TensorFlow and Kubeflow." if "Kubeflow" in u else "not in the posting")
        out = self.say("what's the tech stack for the Artha Labs machine learning engineer job?", m)
        self.assertIn("Tool results", m.calls[0][0])                   # the first call already has the posting
        self.assertIn("TensorFlow and Kubeflow.", out)
        aid = self.artha["id"][:8]
        self.assertIn(f"[{aid}]", out)                                 # one job, no id named: cited for it
        self.assertEqual(re.findall(r"/check_([0-9a-f]{8})", out), [aid])

    def test_invented_ids_are_dropped_and_cards_are_exactly_the_cited_jobs(self):
        u, c = self.uni["id"][:8], self.ciq["id"][:8]
        m = Model(f"Uniphore is 6 months [{u}]; CommerceIQ wants LLM APIs [{c}]. Also see [deadbeef].", first={"answer": "x"})
        out = self.say("compare the Uniphore intern and the CommerceIQ SDE intern", m)
        self.assertNotIn("deadbeef", out)
        self.assertEqual(re.findall(r"/check_([0-9a-f]{8})", out), [u, c])
        self.assertTrue(m.calls[0][1]["models"][0].startswith("nvidia/nemotron-3-ultra"))   # analysis: the strong model
        self.assertEqual(m.calls[0][1]["timeout"], 6)
        self.assertEqual(m.calls[0][1]["models"][1:], assistant._models())   # then the fast ones

    def test_data_scientist_intern_finds_data_science_internships(self):
        j = seed(self.db, "Data Science Internship", "Zenotalent", desc="Six months.")
        for q in ("data scientist intern", "data science internship", "data science intern"):
            self.assertIn(j["id"], [x["id"] for x in assistant.db_search(self.db, q, "any")], q)

    def test_a_plain_lookup_uses_the_fast_model(self):
        m = Model("Six months.")
        self.say("how long is the Uniphore full stack AI intern role?", m)
        self.assertEqual(m.calls[0][1]["models"][0], assistant._models()[0])
        self.assertEqual(m.calls[0][1]["timeout"], 8)

    def test_a_stipend_question_about_a_named_job_reads_its_posting_not_salary_sites(self):
        m = Model("₹25,000 a month.")
        with mock.patch("jobhunter.salary.estimate", side_effect=AssertionError("no estimate")), \
                mock.patch("jobhunter.answer.page", return_value={"url": "x", "error": "the page answered 404"}):
            out = self.say("what stipend does the Uniphore full stack AI intern pay?", m)
        self.assertIn("6 months", m.calls[0][0])                     # its posting, not a salary site
        self.assertNotIn("estimate", out)


class Scam(Base):
    def test_applying_to_a_flagged_job_starts_with_the_warning(self):
        bad = seed(self.db, "Data Entry Intern", "Quickcash Jobs", desc="Pay a registration fee of ₹2000.", flags=("scam?",))
        out = self.say("should I apply to the Quickcash Jobs data entry intern?", Model("Yes, apply: it fits."))
        self.assertTrue(out.startswith("<b>⚠️ Data Entry Intern · Quickcash Jobs"), out[:120])
        self.assertIn("Don't pay anything", out)
        out = self.say("should I apply to the Uniphore full stack AI intern?", Model("Yes, apply: it fits."))
        self.assertNotIn("⚠️", out)
        self.assertNotIn(bad["id"][:8], out)


class Page(Base):
    def test_a_deadline_question_reads_the_jobs_own_page_once_a_day(self):
        jid = seed(self.db, "Data Science Internship", "Zenotalent", deadline="2026-12-28",
                   url="https://unstop.example/zeno")["id"][:8]
        html = "<html><body><nav>menu</nav><main><h1>Data Science Internship</h1><ul><li>Apply by 30 Dec 2026</li></ul>" + \
               "<p>" + "Details about the role. " * 30 + "</p></main><script>x()</script></body></html>"
        r = mock.MagicMock(status_code=200, text=html)
        m = Model(lambda u: "The page says apply by 30 Dec 2026." if "30 Dec 2026" in u else "not in the posting")
        with mock.patch("jobhunter.answer.page", REAL_PAGE), \
                mock.patch("requests.get", return_value=r) as get, mock.patch("jobhunter.companies._render") as render:
            out = self.say("when does the Zenotalent data science internship close?", m)
            out2 = self.say("what's the last date for the Zenotalent data science internship?", m)
        self.assertIn("30 Dec 2026", out)
        self.assertIn("Checked on the job's page today: https://unstop.example/zeno", out)
        self.assertIn(f"[{jid}]", out)
        self.assertEqual(get.call_count, 1)                            # the second question used the 24 h cache
        render.assert_not_called()                                     # plain HTML: no browser
        self.assertIn("30 Dec 2026", out2)

    def test_markdown_keeps_headings_and_lists_and_a_js_shell_gets_the_browser(self):
        md = answer._markdown("<body><header>x</header><main><h2>Pay</h2><ul><li>₹25,000 a month</li></ul></main></body>")
        self.assertIn("## Pay", md)
        self.assertIn("- ₹25,000 a month", md)
        shell = mock.MagicMock(status_code=200, text="<html><body><div id=root></div><noscript>enable JavaScript</noscript></body></html>")
        full = "<main><p>" + "Stipend ₹25,000 per month. " * 40 + "</p></main>"
        with mock.patch("requests.get", return_value=shell), mock.patch("jobhunter.companies._render", return_value=full) as render:
            got = REAL_PAGE(self.db, "https://spa.example/job")
        render.assert_called_once()
        self.assertEqual(got["how"], "browser")
        with mock.patch("requests.get", return_value=mock.MagicMock(status_code=404, text="")):
            self.assertIn("404", REAL_PAGE(self.db, "https://gone.example/job")["error"])


class Closing(Base):
    def test_close_tomorrow_is_exactly_tomorrows_jobs(self):
        today, tom = dt.date.today(), dt.date.today() + dt.timedelta(days=1)
        a = seed(self.db, "Data Analyst", "Todayco", deadline=str(today))
        b = seed(self.db, "ML Intern", "Tomorrowco", deadline=str(tom))
        out = self.say("which jobs close tomorrow?", Model("x"))
        self.assertIn(f"/check_{b['id'][:8]}", out)
        self.assertNotIn(a["id"][:8], out)
        self.assertEqual(answer.closing_days("anything closing this week?"), {"days": 7})
        self.assertIsNone(answer.closing_days("what does Zoho do?"))


class Confirm(Base):
    def test_a_change_waits_for_the_confirm_button_and_an_old_button_does_nothing(self):
        jid = self.uni["id"][:8]
        out = self.say(f"mark {jid} as interview", Model("x", first={"tool": "set_status", "args": {"id": jid, "status": "interview"}}))
        self.assertIn("Tap ✅ Confirm", out)
        kb = outcomes.buttons_for(out, self.db)
        yes = kb["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual([b["text"] for b in kb["inline_keyboard"][0]], ["✅ Confirm", "✗ Cancel"])
        self.assertEqual(self.db.get(jid)["status"], "new")              # nothing changed yet
        calls, sent = [], []
        post = lambda method, **kw: calls.append((method, kw))  # noqa: E731
        cq = lambda data, chat=42: {"id": "q", "data": data, "message": {"message_id": 9, "chat": {"id": chat}}}  # noqa: E731
        with mock.patch.object(outcomes, "DB", lambda: self.db), mock.patch("jobhunter.bot.send", lambda a, c, t: sent.append(t)):
            outcomes.on_button("api", "42", cq(yes, chat=7), post)          # another chat: refused
            self.assertEqual(self.db.get(jid)["status"], "new")
            outcomes.on_button("api", "42", cq("c:yes:1"), post)            # a stamp that isn't the waiting change
            self.assertEqual(self.db.get(jid)["status"], "new")
            outcomes.on_button("api", "42", cq(yes), post)
        self.assertEqual(self.db.get(jid)["status"], "interview")
        self.assertTrue(any("interview" in t.lower() for t in sent), sent)
        self.assertIn(("answerCallbackQuery", {"callback_query_id": "q", "text": "Done"}), calls)

    def test_cancel_drops_it(self):
        jid = self.uni["id"][:8]
        out = self.say(f"mark {jid} as rejected", Model("x", first={"tool": "set_status", "args": {"id": jid, "status": "rejected"}}))
        no = outcomes.buttons_for(out, self.db)["inline_keyboard"][0][1]["callback_data"]
        with mock.patch.object(outcomes, "DB", lambda: self.db), mock.patch("jobhunter.bot.send", lambda *a: None):
            outcomes.on_button("api", "42", {"id": "q", "data": no, "message": {"chat": {"id": 42}}}, lambda m, **k: None)
        self.assertEqual(self.db.get(jid)["status"], "new")


class Typing(unittest.TestCase):
    def test_every_message_shows_typing_first(self):
        from jobhunter import bot
        posts = []
        with mock.patch("requests.post", lambda url, **kw: posts.append(url) or mock.MagicMock(ok=True)), \
                mock.patch.object(bot, "handle", lambda *a, **k: "hi"):
            bot._reply("api", "42", "/help")
        self.assertTrue(posts[0].endswith("/sendChatAction"), posts)


if __name__ == "__main__":
    unittest.main()
