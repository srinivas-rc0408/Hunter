"""/intel: counts come only from stored postings; 'likely next' needs one role in 3+ months, else says so."""
import datetime as dt
import unittest

from _home import make_home, use

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter import intel  # noqa: E402


def job(title, posted, desc="", loc="Bengaluru, Karnataka, India", deadline=None):
    return {"title": title, "posted_at": posted, "found_at": posted + "T09:00:00+05:30", "description": desc,
            "location": loc, "deadline": deadline}


class Analyse(unittest.TestCase):
    def test_likely_next_from_a_repeating_role(self):
        jobs = [job("GenAI Engineer", "2026-08-05", "Python, RAG, Kubernetes"), job("Sr. GenAI Engineer - II", "2026-09-20", "Kubernetes"),
                job("GenAI Engineer (Bengaluru)", "2026-11-01", "Kubernetes, Python"), job("Data Analyst", "2026-11-02", "3+ years SQL")]
        a = intel.analyse(jobs, ["Python", "RAG"])
        role, when, gap, n, months = a["likely"][0]
        self.assertEqual((role, n, months), ("genai engineer", 3, ["Aug", "Sep", "Nov"]))
        self.assertEqual(when, dt.date(2026, 11, 1) + dt.timedelta(days=44))      # median of 46 and 42 days
        self.assertEqual(a["gaps"], ["Kubernetes"])                                # in 75% of postings, not on the resume
        self.assertEqual(a["fresher"], 2)      # not the "Sr." role, not the analyst asking 3+ years

    def test_too_little_history(self):
        a = intel.analyse([job("ML Engineer", "2026-11-01"), job("ML Engineer", "2026-11-20")], ["Python"])
        self.assertEqual(a["likely"], [])

    def test_report_says_not_enough_history(self):
        from jobhunter.db import DB
        t = intel.report(DB(), "Nowhere Labs", news_fn=lambda *a: [])
        self.assertIn("Not enough history yet", t)
        self.assertIn("none found", t)

    def test_stock_talk_is_not_funding(self):
        n = [{"title": "Why Is X Share Price Falling: Investor Analysis", "date": "2026-11-22", "url": "u", "source": "s"},
             {"title": "X raises ₹200 crore in Series B round", "date": "2026-11-20", "url": "u", "source": "s"}]
        self.assertEqual([k for k, _ in intel.signals(n)], ["funding"])


if __name__ == "__main__":
    unittest.main()
