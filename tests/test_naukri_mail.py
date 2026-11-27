"""Naukri: its search is captcha-blocked, so the scan skips it ("Naukri skipped (blocked)") and its jobs come from
Naukri's own job alert emails instead: parsed into jobs with their apply links, scored and filtered like any scan."""
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from _home import make_home, use

HOME = make_home()
FIX = Path(__file__).parent / "fixtures" / "mail" / "naukri_alert.html"


def setUpModule():
    use(HOME)


from jobhunter import mail  # noqa: E402
from jobhunter.db import DB  # noqa: E402
from test_mail import FakeIMAP  # noqa: E402


def alert_mail(sender="recommendationnc@naukri.com", html=None):
    m = EmailMessage()
    m["From"], m["Subject"] = f"Naukri <{sender}>", "Asha, New Job Opportunities for Intern"
    m["Date"], m["Message-ID"] = "Mon, 23 Nov 2026 20:51:20 +0530", f"<{sender}-1@naukri>"
    m.set_content("Seize all job opportunities of this week!")
    m.add_alternative(html if html is not None else FIX.read_text(), subtype="html")
    return m.as_bytes()


class Parse(unittest.TestCase):
    def setUp(self):
        self.jobs = {j.company: j for j in mail.naukri_jobs(FIX.read_text(), "2026-11-23T20:51:20+05:30")}

    def test_every_card_with_title_company_place_and_link(self):
        self.assertEqual(sorted(self.jobs), ["Acme AI Labs", "Gen Labs", "Lowpay Tech", "Zen Retail"])
        a = self.jobs["Acme AI Labs"]
        self.assertEqual((a.title, a.location, a.source, a.job_type), ("Machine Learning Intern", "Bengaluru", "naukri", "internship"))
        self.assertEqual(a.url, "https://www.naukri.com/job-listings-machine-learning-intern-acme-ai-labs-bengaluru-0-to-1-years-201126500001")
        self.assertEqual(self.jobs["Gen Labs"].location, "Hyderabad, Bengaluru")
        self.assertEqual(self.jobs["Zen Retail"].location, "Mumbai")
        self.assertEqual(a.posted_at, "2026-11-23")

    def test_links_drop_tracking_and_your_user_id(self):
        for j in self.jobs.values():
            self.assertNotIn("?", j.url)
            self.assertNotIn("uid", j.url)

    def test_pay_only_when_shown(self):
        a, g, z = self.jobs["Acme AI Labs"], self.jobs["Gen Labs"], self.jobs["Zen Retail"]
        self.assertEqual((a.pay_min, a.pay_max, a.pay_period, a.currency), (20000, 20000, "month", "INR"))
        self.assertEqual((g.pay_min, g.pay_max, g.pay_period), (800000, 1200000, "year"))
        self.assertEqual((z.pay_min, z.pay_max), (None, None))            # not in the email: never guessed
        self.assertIn("Experience: 0-1 years", a.description)
        self.assertNotIn("relevant", self.jobs["Lowpay Tech"].description)  # the email's own buttons never leak in

    def test_alert_senders(self):
        for s in ("jobalert@naukri.com", "jobalerts@naukri.com", "recommendationnc@naukri.com", "naukrialerts@naukri.com"):
            self.assertTrue(mail.NAUKRI_ALERT.match(s), s)
        for s in ("eventnc@naukri.com", "contentnc@naukri.com", "password@naukri.com", "jobalert@naukri.com.evil.io"):
            self.assertFalse(mail.NAUKRI_ALERT.match(s), s)

    def test_empty_or_foreign_html(self):
        self.assertEqual(mail.naukri_jobs(""), [])
        self.assertEqual(mail.naukri_jobs('<a href="https://www.naukri.com/mnjuser/recommendedjobs">View all</a>'), [])


class Sync(unittest.TestCase):
    def test_alert_jobs_scored_and_filtered_like_a_scan(self):
        db = DB()
        mail.ensure(db)
        fake = FakeIMAP([alert_mail(), alert_mail("eventnc@naukri.com", "<p>Play &amp; win</p>")])
        with mock.patch("jobhunter.pipeline.notify.alert_jobs", return_value=0) as sent:
            r = mail.sync(imap_factory=lambda: fake, quiet=True)
        got = {j["company"]: j for j in db.query(limit=1000) if "naukri" in (j.get("sources") or [])}
        self.assertIn(got["Acme AI Labs"]["status"], ("new", "alerted"))                     # Bengaluru intern, ₹20k/month
        self.assertIn(got["Gen Labs"]["status"], ("new", "alerted"))                         # 8-12 LPA, Bengaluru
        self.assertEqual(got["Zen Retail"]["status"], "filtered")                    # Mumbai: Karnataka only
        self.assertEqual(got["Lowpay Tech"]["status"], "filtered")                   # 3-5 LPA < the 6 LPA floor
        self.assertTrue(any("LPA" in x for x in got["Lowpay Tech"]["reasons"]), got["Lowpay Tech"]["reasons"])
        self.assertTrue(got["Acme AI Labs"]["url"].startswith("https://www.naukri.com/job-listings-"))
        self.assertEqual(r.get("naukri_jobs"), 2)
        self.assertTrue(sent.called)                                                 # alerted like scan finds
        bodies = [ids for what, ids in fake.fetched if "HEADER" not in what]
        self.assertNotIn([2], bodies)                                                # a promo's body is never read
        self.assertEqual(db.c.execute("SELECT COUNT(*) FROM mail").fetchone()[0], 0)  # alerts aren't "replies"
        fake.fetched = []
        self.assertEqual(mail.sync(imap_factory=lambda: fake, quiet=True), {})      # read once


class ScanSkip(unittest.TestCase):
    def test_naukri_is_skipped_not_retried(self):
        from jobhunter.sources import boards
        calls, printed = [], []
        fake_jobspy = mock.MagicMock()
        fake_jobspy.scrape_jobs = lambda site_name, **k: calls.append(site_name[0])
        with mock.patch.dict("sys.modules", {"jobspy": fake_jobspy}), \
                mock.patch.object(boards, "_tag_indeed_estimates", lambda: None), \
                mock.patch.object(boards.console, "print", lambda *a, **k: printed.append(" ".join(map(str, a)))), \
                mock.patch.object(boards.time, "sleep", lambda s: None):
            boards.scan([], queries=[{"term": "ml intern"}], locations=["Bengaluru"])
        self.assertNotIn("naukri", calls)
        self.assertIn("linkedin", calls)
        self.assertEqual(sum("Naukri skipped (blocked)" in p for p in printed), 1)

    def _scan(self, scrape, queries, locations, cap=None):
        from jobhunter.sources import boards
        printed, errors = [], []
        fake_jobspy = mock.MagicMock()
        fake_jobspy.scrape_jobs = scrape
        cfg = {"boards": {"linkedin": {"enabled": True, **({"max_searches": cap} if cap else {})}}}
        with mock.patch.dict("sys.modules", {"jobspy": fake_jobspy}), \
                mock.patch.object(boards.config, "cfg", lambda n: cfg), \
                mock.patch.object(boards, "_tag_indeed_estimates", lambda: None), \
                mock.patch.object(boards.console, "print", lambda *a, **k: printed.append(" ".join(map(str, a)))), \
                mock.patch.object(boards.time, "sleep", lambda s: None):
            boards.scan(errors, queries=queries, locations=locations)
        return printed, errors

    def test_linkedin_rate_limit_stops_linkedin_for_the_run(self):
        """26 Nov 2026: LinkedIn 429'd most searches and the scan sat on it for 25 min. JobSpy doesn't raise a
        429: it keeps it in df.attrs["errors"]. First 429 → no more LinkedIn this run, no waiting, no retry."""
        import pandas as pd
        calls = []

        def scrape(site_name, search_term, location, **k):
            calls.append((search_term, location))
            df = pd.DataFrame()
            if len(calls) == 2:
                df.attrs = {"errors": {"linkedin": "response status code 429 (blocked: use a proxy)"}}
            return df
        printed, errors = self._scan(scrape, [{"term": "ml intern"}, {"term": "ai engineer"}, {"term": "data"}],
                                     ["Bengaluru", "Mysuru"])
        self.assertEqual(len(calls), 2)                       # 4 searches left, none tried
        self.assertEqual(sum("LinkedIn rate-limited, skipped rest" in p for p in printed), 1)
        self.assertTrue(any("LinkedIn rate-limited, skipped rest" in e for e in errors), errors)

    def test_linkedin_caps_searches_bengaluru_first(self):
        calls = []
        self._scan(lambda site_name, search_term, location, **k: calls.append((search_term, location)),
                   [{"term": f"q{i}"} for i in range(5)], ["Bengaluru", "Mysuru"], cap=6)
        self.assertEqual(calls, [(f"q{i}", "Bengaluru") for i in range(5)] + [("q0", "Mysuru")])


if __name__ == "__main__":
    unittest.main()
