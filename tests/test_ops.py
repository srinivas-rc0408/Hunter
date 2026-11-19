"""Morning brief, Telegram bot commands, LLM fallback, encrypted backup/restore."""
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

from _home import use, make_home

HOME = make_home()


def setUpModule():
    use(HOME)

os.environ["STATE_KEY"] = "test-key-123"
os.environ["JOBHUNTER_BACKUP_DIR"] = tempfile.mkdtemp(prefix="jh-bk-")

from jobhunter.db import DB, Job  # noqa: E402
from jobhunter.score import score  # noqa: E402


def seed(db, title, company, **kw):
    j = score(Job(title, company, "Bengaluru, Karnataka", url="https://x.example/j", source="linkedin",
                  description="Python FastAPI LLMs freshers", posted_at=str(dt.date.today()), **kw))
    db.upsert(j)
    db.set_score(j.id, j.score, j.reasons, j.flags, j.dream)
    return j


class Morning(unittest.TestCase):
    def test_brief(self):
        from jobhunter import morning
        db = DB(HOME / "data" / "m.db")
        a = seed(db, "Machine Learning Engineer Intern", "Aditya Birla Capital", job_type="internship")
        b = seed(db, "AI Engineer", "Foo Labs")
        db.set_status(b.id, "needs_you")
        g = Job("Recruitment of Project Engineer (AI)", "IISc", "Bengaluru", url="https://iisc/x", kind="govt",
                deadline=str(dt.date.today() + dt.timedelta(days=4)))
        g.score, g.flags = 80, ["tech", "eligibility:eligible"]
        db.upsert(g)
        db.set_score(g.id, g.score, [], g.flags, False)
        br = morning.build(db=db, today=dt.date.today(), heartbeat=False)
        self.assertEqual(br["new"][0]["id"], a.id)                 # dream first
        self.assertEqual(len(br["waiting"]), 1)
        self.assertEqual(br["govt_soon"][0][1]["id"], g.id)
        msg = morning.as_telegram(br)
        self.assertIn("Morning brief", msg)
        self.assertIn(f"/check_{a.id[:8]}", msg)
        self.assertIn("Top govt notices", msg)
        self.assertIn('<a href="https://x.example/j">Apply on company site</a>', msg)   # every job: one tap to apply
        self.assertIn('<a href="https://iisc/x">Apply / official page</a>', msg)

    def test_alert_and_top_have_apply_links(self):
        from jobhunter.bot import handle
        from jobhunter.notify import format_job
        db = DB(HOME / "data" / "links.db")
        j = seed(db, "Data Engineer", "Link Co")
        self.assertIn('<a href="https://x.example/j">Apply on company site</a>', format_job(db.get(j.id)))
        self.assertIn('<a href="https://x.example/j">Apply on company site</a>', handle("/top", db))


class Bot(unittest.TestCase):
    def test_commands(self):
        from jobhunter.bot import handle
        db = DB(HOME / "data" / "b.db")
        j = seed(db, "Data Scientist", "Sarvam AI")
        self.assertIn("Sarvam AI", handle("/top", db))
        self.assertIn("Sarvam AI", handle(f"/show_{j.id[:8]}", db))
        self.assertIn("skipped", handle(f"/skip {j.id[:8]}", db))
        self.assertEqual(db.get(j.id)["status"], "skipped")
        self.assertIn("interview", handle(f"/interview_{j.id[:8]}@JobBot", db))
        self.assertIn("tracked", handle("/stats", db))
        self.assertIn("Unknown", handle("/nope", db))
        self.assertIn("/help", handle("hello", db))
        self.assertIn("Usage", handle("/show", db))


TG_TAGS = {"b", "i", "u", "s", "a", "code", "pre"}


def assert_telegram_html(tc, text):
    """What Telegram's HTML parse mode accepts: only its tags, every < and & escaped, tags balanced."""
    import re as _re
    stack = []
    for m in _re.finditer(r"<(/?)([a-zA-Z]*)([^>]*)>|<|&(?!(?:lt|gt|amp|quot|#\d+);)", text):
        if m.group(0) in ("<", "&") or m.group(2) not in TG_TAGS:
            tc.fail(f"invalid Telegram HTML {m.group(0)!r} in: {text[max(0, m.start() - 40):m.end() + 40]!r}")
        if m.group(1):
            tc.assertEqual(stack.pop() if stack else None, m.group(2))
        else:
            stack.append(m.group(2))
    tc.assertEqual(stack, [], "unclosed tag")


class BotHTML(unittest.TestCase):
    def test_every_reply_is_valid_telegram_html(self):
        from jobhunter.bot import handle
        db = DB(HOME / "data" / "html.db")
        j = seed(db, "ML <Intern> & R&D", "A&B <Labs>")
        for cmd in ("/help", "help", "/start", "top 5", "/top", "/stats", "/govt", "/review", "/ping", "/questions",
                    f"/show_{j.id[:8]}", "/show", "/answer", "/answer x", "/company", "/nope", "hello", "",
                    f"/queue {j.id[:8]}", "/skip", "/search", "search ml intern", "search <x&y>", "/search a&b 2",
                    "stats", "applied", "/applied 2", "/deadlines", "/eligible", "govt karnataka", "govt central", "govt all"):
            out = handle(cmd, db)
            self.assertTrue(out.strip(), cmd)
            assert_telegram_html(self, out)
        self.assertIn("JobHunter", handle("help", db))
        self.assertIn("ML &lt;Intern&gt;", handle("search intern", db))
        self.assertIn("Response rate", handle("stats", db))
        self.assertIn("alive", handle("ping", db))

    def test_brief_is_valid_html(self):
        from jobhunter.morning import as_telegram, build
        db = DB(HOME / "data" / "html2.db")
        seed(db, "Data <Scientist> & co", "X&Y")
        assert_telegram_html(self, as_telegram(build(db=db, heartbeat=False)))

    def test_send_falls_back_to_plain_text(self):
        from unittest import mock

        from jobhunter import bot
        calls = []

        def post(url, timeout, json):
            calls.append(json)
            return mock.Mock(ok="parse_mode" not in json, status_code=400, text="can't parse entities")
        with mock.patch.object(bot.requests, "post", post):
            self.assertTrue(bot.send("http://x", "1", "<b>hi</b> &lt;id&gt;"))
        self.assertEqual(calls[1]["text"], "hi <id>")


class LLM(unittest.TestCase):
    def test_auto_falls_back_to_none(self):
        from jobhunter import llm
        os.environ["LLM_PROVIDER"] = "auto"
        for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY", "NVIDIA_API_KEYS"):
            os.environ.pop(k, None)
        llm._PICKED.clear()
        llm.PRESETS["ollama"] = ("http://127.0.0.1:9/v1", "x")     # nothing listens on port 9
        self.assertFalse(llm.available())
        self.assertIsNone(llm.chat("s", "u"))
        os.environ["LLM_PROVIDER"] = "none"


class Backup(unittest.TestCase):
    def test_roundtrip(self):
        from jobhunter import ops
        (HOME / "data" / "answers.yaml").write_text("q: {answer: '1'}\n")
        DB().upsert(Job("AI Engineer", "Backup Co", "Bengaluru"))
        f = ops.backup()
        self.assertTrue(f.name.endswith(".tar.gz.enc"))
        self.assertNotIn(b"Backup Co", f.read_bytes())               # encrypted
        (HOME / "data" / "answers.yaml").unlink()
        ops.restore(str(f))
        self.assertTrue((HOME / "data" / "answers.yaml").is_file())
        with self.assertRaises(SystemExit):
            ops.restore(str(f), key="wrong-key")


if __name__ == "__main__":
    unittest.main()


CARD_FIELDS = ("📍", "💰", "🕒 Posted", "found", "IST", "🔗", "/check_", "/skip_")


class JobCards(unittest.TestCase):
    """Every job in /top, the morning brief and alerts: role, company, location + work mode, pay as listed,
    posted + found dates, apply link, /check and /skip, and "Applied on" once applied."""

    def test_pay_exactly_as_listed(self):
        from jobhunter.notify import pay_text
        for j, want in [({"pay_min": 1000000, "pay_max": 1600000, "pay_period": "year", "currency": "INR"}, "₹10–16 LPA"),
                        ({"pay_min": 350000, "pay_period": "year", "currency": "INR"}, "from ₹3.5 LPA"),
                        ({"pay_min": 1100000, "pay_max": 1350000, "pay_period": "year", "currency": "INR"}, "₹11–13.5 LPA"),
                        ({"pay_min": 4000, "pay_max": 5000, "pay_period": "month", "currency": "INR"}, "₹4,000–5,000/month"),
                        ({"pay_min": 20, "pay_max": 100, "pay_period": "hour", "currency": "USD"}, "$20–100/hour (not INR)"),
                        ({"pay_min": 90000, "pay_max": 120000, "pay_period": "year", "currency": "CAD"}, "C$90,000–120,000/year (not INR)"),
                        ({"pay_min": 500, "pay_max": 500, "pay_period": "week", "currency": "EUR"}, "€500/week (not INR)"),
                        ({"pay_min": 30000, "pay_period": "month", "currency": "GBP"}, "from £30,000/month (not INR)"),
                        ({"pay_min": 1500, "pay_period": "day", "currency": "INR"}, "from ₹1,500/day"),
                        ({"pay_min": 60, "pay_max": 80, "pay_period": "hour", "currency": "SGD"}, "S$60–80/hour (not INR)"),
                        ({"pay_min": 12.5, "pay_period": "hour", "currency": "USD"}, "from $12.5/hour (not INR)"),
                        ({"pay_min": None}, "not listed"), ({}, "not listed")]:
            self.assertEqual(pay_text(j), want, j)

    def test_dates_as_given_and_found_in_ist(self):
        from jobhunter.notify import when_text
        self.assertEqual(when_text({"posted_at": "2026-11-17", "found_at": "2026-11-18T08:34:49+05:30"}),
                         "Posted 17 Nov 2026 · found 18 Nov, 8:34 am IST")
        self.assertEqual(when_text({"posted_at": "2026-11-17T09:30:00+00:00", "found_at": "2026-11-18T03:04:49+00:00"}),
                         "Posted 17 Nov 2026, 3:00 pm IST · found 18 Nov, 8:34 am IST")     # a time only when the source gave one
        self.assertEqual(when_text({"posted_at": None, "found_at": "2026-11-18T20:05:00+05:30"}),
                         "Posted date not listed · found 18 Nov, 8:05 pm IST")

    def test_work_mode_only_when_the_posting_says_so(self):
        from jobhunter.notify import mode_text
        for j, want in [({"is_remote": 1, "location": "India"}, "Remote"),
                        ({"location": "Remote - India"}, "Remote"),
                        ({"location": "Bengaluru (Hybrid)"}, "Hybrid"),
                        ({"location": "Bengaluru", "description": "This is a hybrid role, 3 days in office."}, "Hybrid"),
                        ({"location": "Bengaluru", "description": "Work mode: Hybrid"}, "Hybrid"),
                        ({"location": "Bengaluru", "description": "Work from office, Mon-Fri."}, "On-site"),
                        ({"location": "Bengaluru, On-site"}, "On-site"),
                        ({"location": "Bengaluru", "description": "Experience with hybrid cloud and on-site servers"}, None),
                        ({"location": "Bengaluru", "description": ""}, None)]:
            self.assertEqual(mode_text(j), want, j)

    def test_apply_link_prefers_the_company_page(self):
        from jobhunter.notify import apply_link
        j = {"url": "https://www.linkedin.com/jobs/view/1", "urls": ["https://www.linkedin.com/jobs/view/1",
                                                                      "https://jobs.lever.co/acme/2?a=1&b=2"]}
        self.assertEqual(apply_link(j), '<a href="https://jobs.lever.co/acme/2?a=1&amp;b=2">Apply on company site</a>')
        self.assertEqual(apply_link({"url": "https://www.naukri.com/job/3", "urls": []}),
                         '<a href="https://www.naukri.com/job/3">Apply on Naukri</a>')
        self.assertEqual(apply_link({"url": "https://in.linkedin.com/jobs/view/4"}),
                         '<a href="https://in.linkedin.com/jobs/view/4">Apply on LinkedIn</a>')
        self.assertEqual(apply_link({"url": ""}), "")

    def _db(self, name, n=1, url=None):
        db = DB(HOME / "data" / name)
        jobs = [seed(db, f"ML Engineer {chr(65 + i) * 3} " + "with a long title " * 3, f"Co & {chr(65 + i) * 3}", pay_min=1000000, pay_max=1600000,
                     pay_period="year", currency="INR") for i in range(n)]
        if url:
            db.c.execute("UPDATE jobs SET url=?", (url,))
        return db, jobs

    def assert_card(self, text, j):
        assert_telegram_html(self, text)
        for f in CARD_FIELDS:
            self.assertIn(f, text)
        for f in ("Bengaluru, Karnataka", "₹10–16 LPA", f"/check_{j.id[:8]}", f"/skip_{j.id[:8]}",
                  "Posted " + dt.date.today().strftime("%d %b %Y").lstrip("0")):
            self.assertIn(f, text)

    def test_card_has_every_field_and_applied_on(self):
        from jobhunter.notify import job_card
        db, (j,) = self._db("card.db")
        self.assert_card(job_card(db.get(j.id), db), j)
        self.assertNotIn("Applied on", job_card(db.get(j.id), db))
        db.set_status(j.id, "applied", "test")
        self.assertIn("✅ Applied on " + dt.date.today().strftime("%d %b %Y").lstrip("0"), job_card(db.get(j.id), db))
        self.assertIn("💰 not listed", job_card({**db.get(j.id), "pay_min": None, "pay_max": None}, db))

    def test_alert_has_every_field(self):
        from jobhunter.notify import format_job
        db, (j,) = self._db("alert.db")
        self.assert_card(format_job(db.get(j.id)), j)

    def test_top_is_paged_and_never_cuts_a_job(self):
        from jobhunter.bot import handle
        db, jobs = self._db("top.db", n=12, url="https://boards.greenhouse.io/acme/jobs/" + "9" * 300)
        p5 = handle("top 5", db)
        self.assertEqual(p5.count("/check_"), 5)
        p1, p2 = handle("top 10", db), handle("/top 10 2", db)
        for p in (p5, p1, p2):
            self.assertLessEqual(len(p), 3900)
            assert_telegram_html(self, p)
            for block in p.split("\n\n")[1:]:
                if "/check_" in block:
                    for f in CARD_FIELDS:
                        self.assertIn(f, block)                  # each job whole, in one message
        self.assertIn("/top 10 2", p1)                           # tells you how to get the rest
        self.assertEqual(p1.count("/check_") + p2.count("/check_"), 10)
        self.assertFalse(set(p1.split()) & {w for w in p2.split() if w.startswith("/check_")})   # no job twice
        self.assert_card(p1, max((x for x in jobs if f"/check_{x.id[:8]}" in p1), key=lambda x: 0))

    def test_brief_shows_full_cards_in_whole_pages(self):
        from jobhunter.morning import as_telegram_pages, build
        db, jobs = self._db("brief.db", n=8)
        pages = as_telegram_pages(build(db=db, heartbeat=False))
        self.assertIn("Morning brief", pages[0])
        self.assertEqual(sum(p.count("/check_") for p in pages), 8)
        for p in pages:
            self.assertLessEqual(len(p), 3900)
            assert_telegram_html(self, p)
        self.assert_card("\n".join(pages), jobs[0])


class Today(unittest.TestCase):
    """/today: jobs posted in the last 72 h (posting date, else the date we found it), best first, paged."""

    def test_72h_window_header_govt_block_and_paging(self):
        from jobhunter.bot import handle
        from jobhunter.util import iso, now
        db = DB(HOME / "data" / "today.db")
        t = now()
        day = lambda d: str((t - dt.timedelta(days=d)).date())
        cases = {"AAA in": day(0), "BBB in": day(3), "CCC out": day(4), "DDD in": None, "EEE out": None,
                 "FFF in": iso(t - dt.timedelta(hours=71)), "GGG out": iso(t - dt.timedelta(hours=73))}
        for title, posted in cases.items():
            j = score(Job(f"ML Engineer {title}", f"Firm {title[:3]}", "Bengaluru, Karnataka", url="https://x.example/j",
                          source="linkedin", description="Python LLMs freshers", posted_at=posted))
            db.upsert(j)
            db.set_score(j.id, j.score, j.reasons, j.flags, title.startswith("AAA"))
        db.c.execute("UPDATE jobs SET found_at=? WHERE title LIKE '%EEE%'", (iso(t - dt.timedelta(days=5)),))
        db.c.execute("UPDATE jobs SET found_at=? WHERE title LIKE '%CCC%'", (iso(t),))     # posting date wins
        g = Job("Recruitment of Project Engineer", "IISc", "Bengaluru", url="https://iisc/x", kind="govt")
        db.upsert(g)
        db.set_score(g.id, 70, [], ["tech"], False)
        for title, dl in (("Closed Role III", day(1)), ("Open Role JJJ", str(t.date()))):
            j = score(Job(title, f"Firm {title[-3:]}", "Bengaluru", url="https://x.example/c", source="linkedin",
                          description="Python LLMs freshers", posted_at=day(0), deadline=dl))
            db.upsert(j)
            db.set_score(j.id, 60, [], [], False)
        skipped = seed(db, "Skipped Role HHH", "Firm HHH")
        db.set_status(skipped.id, "skipped")
        db.c.commit()
        out = handle("today", db)
        assert_telegram_html(self, out)
        self.assertIn("5 new in 3 days · 1 ★", out)
        self.assertNotIn("III", out)                                       # last date passed: not offered
        self.assertNotIn("III", handle("top 50", db))
        self.assertIn("⏰ last date " + t.strftime("%d %b %Y").lstrip("0"), out)
        for title in cases:
            (self.assertIn if title.endswith(" in") else self.assertNotIn)(f"ML Engineer {title}", out)
        self.assertNotIn("HHH", out)
        self.assertLess(out.index("AAA"), out.index("BBB"))                   # dream first
        self.assertIn("🏛 Govt", out)
        self.assertGreater(out.index("IISc"), out.index("🏛 Govt"))
        for f in CARD_FIELDS:
            self.assertIn(f, out)
        self.assertEqual(handle("/new", db), out)
        for i in range(30):                                                    # many → whole pages, "/today 2"
            seed(db, f"Data Scientist {chr(65 + i % 26) * 2}{chr(65 + i // 26)}", f"Org {chr(65 + i % 26) * 3}{i // 26}")
        p1, p2 = handle("today", db), handle("/today 2", db)
        self.assertIn("/today 2", p1)
        for p in (p1, p2):
            assert_telegram_html(self, p)
            self.assertLessEqual(len(p), 3900)
            self.assertTrue(p.startswith("<b>📰"))
        self.assertIn("page 2/", p2)


class CompanySearch(unittest.TestCase):
    """/company <name>, "company x", or just the name: numbered choices, every open role as a card, then news,
    what it does, Bengaluru office, your applications and dream status. Live lookups are stubbed here."""

    def setUp(self):
        from unittest import mock

        from jobhunter import bot, companies
        self.db = DB(HOME / "data" / "co.db")
        bot._PICK.clear()
        bot._COMPANY_PAGES.clear()
        rows = [companies.ats_more.L(title=f"AI Engineer {k} <R&D>", company="Acme & Co", location="Bengaluru, Karnataka",
                                     url=f"https://acme.example/j/{k}", posted=str(dt.date.today()), source="greenhouse",
                                     description="Python LLMs freshers 0-1 years")
                for k in ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
                          "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel")]
        info = {"name": "Acme & Co", "boards": [], "careers_url": "https://acme.example/careers", "website": None}
        self.p = [mock.patch.object(companies, "live", lambda n, d=None, w=None, budget=22: (dict(info, name=n), rows if "Acme" in n else [], [])),
                  mock.patch.object(companies, "news", lambda n: [{"date": "2026-11-18", "title": "Acme <raises> & grows",
                                                                   "source": "ET", "url": "https://news.example/a?x=1&y=2"}]),
                  mock.patch.object(companies, "about", lambda i: {"description": "AI <lab>", "employees": "1200"}),
                  mock.patch.object(companies, "suggest", lambda q, online=True, limit=12: [
                      x for x in [{"name": "Acme & Co", "source": "directory"}, {"name": "Acme Capital", "source": "your jobs", "hint": "3 job(s)"},
                                  {"name": "Nadaco", "source": "wikidata"}, {"name": q, "source": "as typed"}]
                      if x["name"].lower().startswith(q.lower()[:4]) or x["source"] == "as typed"])]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_choices_then_number_then_cards_and_about(self):
        from jobhunter.bot import handle
        out = handle("acme", self.db)                                       # just the name, no slash
        assert_telegram_html(self, out)
        self.assertIn("1. Acme &amp; Co", out)
        self.assertIn("2. Acme Capital", out)
        self.assertNotIn("Nadaco", out)
        main, info = handle("1", self.db)
        for part in (main, info):
            assert_telegram_html(self, part)
        self.assertRegex(main, r"20 open roles · checked \d{1,2}:\d\d [ap]m IST")
        for f in CARD_FIELDS:
            self.assertIn(f, main)
        self.assertIn("More: /company Acme &amp; Co 2", main)
        self.assertIn("page 2/", handle("/company Acme & Co 2", self.db))   # page 2 from the cache, no new search
        for want in ("What it does: AI &lt;lab&gt;", "Size: 1,200 employees", "Bengaluru office: yes, 20",
                     "Dream: no", "none yet", "18 Nov", 'href="https://news.example/a?x=1&amp;y=2"',
                     "Careers: <a"):
            self.assertIn(want, info)

    def test_exact_name_goes_straight_and_zero_offers_watch(self):
        from jobhunter.bot import handle
        main, _ = handle("/company Acme & Co", self.db)
        self.assertIn("20 open roles", main)
        out, _ = handle("company Zzzcorp", self.db)
        assert_telegram_html(self, out)
        self.assertIn("0 open roles", out)
        self.assertIn("/watch Zzzcorp", out)
        self.assertIn("/help", handle("hello there", self.db))             # not a company: the help hint
