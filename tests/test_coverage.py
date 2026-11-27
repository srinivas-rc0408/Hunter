"""Scan coverage (v2.5.4): searches and boards rotate so a cut-short scan resumes where it stopped, company boards
read only postings new since their last read, LinkedIn reads descriptions only for jobs that pass the filters,
and every scan logs how much of each source it covered."""
import datetime as dt
import threading
import unittest
from unittest import mock

from _home import make_home, use

HOME = make_home()


def setUpModule():
    use(HOME)


import pandas as pd  # noqa: E402
from jobhunter import health, pipeline  # noqa: E402
from jobhunter.db import DB  # noqa: E402
from jobhunter.sources import ats, boards  # noqa: E402

TODAY = str(dt.date.today())


def fake_jobspy(scrape):
    m = mock.MagicMock()
    m.scrape_jobs = scrape
    return mock.patch.dict("sys.modules", {"jobspy": m})


def board_cfg(**linkedin):
    return {"boards": {"indeed": {"enabled": True}, "linkedin": {"enabled": True, **linkedin}},
            "queries": [{"term": f"q{i}"} for i in range(5)], "locations": ["Bengaluru", "Mysuru"]}


class Rotation(unittest.TestCase):
    def scan(self, site, start, cut_after, cfg=None):
        """One board's scan that gets through `cut_after` searches; returns (searches run, cov)."""
        calls, cov = [], {"stop": threading.Event()}

        def scrape(site_name, search_term, location, **k):
            calls.append((search_term, location))
            if len(calls) >= cut_after:
                cov["stop"].set()                       # the 90 s limit lands here
            return pd.DataFrame()
        with fake_jobspy(scrape), mock.patch.object(boards.config, "cfg", lambda n: cfg or board_cfg()), \
                mock.patch.object(boards, "_tag_indeed_estimates", lambda: None), \
                mock.patch.object(boards.time, "sleep", lambda s: None):
            boards.scan([], sites=[site], start=start, cov=cov, quiet=True)
        return calls, cov

    def test_rotate(self):
        self.assertEqual(boards.rotate([1, 2, 3, 4], 1), [2, 3, 4, 1])
        self.assertEqual(boards.rotate([1, 2, 3, 4], 6), [3, 4, 1, 2])     # a position from a longer list: wraps
        self.assertEqual(boards.rotate([], 3), [])

    def test_a_cut_scan_resumes_where_it_stopped_and_every_search_runs(self):
        everything = {(f"q{i}", c) for c in ("Bengaluru", "Mysuru") for i in range(5)}
        batches, start = [], 0
        for _ in range(3):                              # three scans of 4 searches each cover all 10
            calls, cov = self.scan("indeed", start, cut_after=4)
            self.assertEqual((cov["done"], cov["total"]), (4, 10))
            batches.append(set(calls))
            start = (start + cov["done"]) % cov["total"]
        self.assertFalse(batches[0] & batches[1])       # the second scan picks up where the first was cut
        self.assertEqual(set().union(*batches), everything)

    def test_linkedin_cap_applies_after_rotation(self):
        calls, cov = self.scan("linkedin", 3, cut_after=99, cfg=board_cfg(max_searches=6))
        self.assertEqual(calls, [(f"q{i}", "Bengaluru") for i in (3, 4)] + [(f"q{i}", "Mysuru") for i in range(4)])
        self.assertEqual((cov["done"], cov["total"]), (6, 10))

    def test_a_rate_limited_search_is_not_done_so_it_goes_first_next_time(self):
        calls = []

        def scrape(site_name, search_term, location, **k):
            calls.append(search_term)
            df = pd.DataFrame()
            if len(calls) == 3:
                df.attrs = {"errors": {"linkedin": "response status code 429"}}
            return df
        cov = {}
        with fake_jobspy(scrape), mock.patch.object(boards.config, "cfg", lambda n: board_cfg()), \
                mock.patch.object(boards, "_tag_indeed_estimates", lambda: None), \
                mock.patch.object(boards.time, "sleep", lambda s: None):
            boards.scan([], sites=["linkedin"], cov=cov, quiet=True)
        self.assertEqual(cov["done"], 2)                # q2 @ Bengaluru got the 429: next scan starts there

    def test_the_position_is_stored_in_the_db_and_dry_runs_never_move_it(self):
        db = DB()
        db.set_state("rot:indeed", 7)
        starts = []

        def fake_scan(errors, sites, start, cov, out, **k):
            starts.append(start)
            cov.update(total=10, done=4)
        with mock.patch.object(pipeline.boards, "enabled", lambda light=False: ["indeed"]), \
                mock.patch.object(pipeline.boards, "scan", fake_scan):
            pipeline.run(only={"boards"}, dry=True)
            self.assertEqual(DB().state("rot:indeed"), 7)     # dry: the throwaway copy moved, not the real DB
            r = pipeline.run(only={"boards"}, notify_on=False)
        self.assertEqual(starts, [7, 7])
        self.assertEqual(DB().state("rot:indeed"), 1)         # (7 + 4) % 10
        self.assertEqual(r["coverage"], {"indeed": "4/10 queries"})


class PostedSince(unittest.TestCase):
    def test_new_means_an_unseen_id_or_with_no_id_posted_on_or_after_the_last_read(self):
        jobs = [{"_sid": "1", "posted": "2026-11-01"}, {"_sid": "2", "posted": "2026-11-01"},
                {"_sid": "", "posted": "2026-11-20"}, {"_sid": "", "posted": "2026-11-27"}, {"_sid": "", "posted": None}]
        got = ats.fresh(jobs, seen={"1"}, since="2026-11-27")
        self.assertEqual(got, [jobs[1], jobs[3], jobs[4]])    # id 2 unseen (even posted long ago); undated kept
        self.assertEqual(ats.fresh(jobs, set(), None), jobs)   # a board read for the first time: everything

    def test_a_second_scan_skips_seen_postings_and_their_detail_pages(self):
        listing = {"jobs": [{"id": 11, "title": "Machine Learning Intern", "location": {"name": "Bengaluru, India"},
                             "absolute_url": "https://x/11", "first_published": "2026-11-20T00:00:00Z"},
                            {"id": 12, "title": "Data Scientist", "location": {"name": "Bengaluru, India"},
                             "absolute_url": "https://x/12", "first_published": "2026-11-21T00:00:00Z"}]}
        fetched = []

        class Fetcher:
            def __init__(self, **k):
                pass

            def get(self, url, name, body=None):
                fetched.append(url)
                if url.endswith("/jobs"):
                    return listing
                jid = int(url.rsplit("/", 1)[1])
                return {**next(j for j in listing["jobs"] if j["id"] == jid), "content": "&lt;p&gt;Python, PyTorch&lt;/p&gt;"}
        sc = ats.scout()
        cfg = {"companies": {"boards": [{"type": "greenhouse", "company": "Acme", "token": "acme"}]},
               "profile": {"targets": {"roles": ["machine learning", "data scientist"]}}}
        db = DB()
        with mock.patch.object(sc, "Fetcher", Fetcher), \
                mock.patch.object(ats.config, "cfg", lambda n: cfg.get(n, {})), \
                mock.patch("jobhunter.companies.watchlist", lambda: []):
            cov = {}
            first = ats.scan([], db=db, cov=cov)
            self.assertEqual(len(first), 2)
            self.assertIn("Python", first[0].description)       # detail page read for a new posting
            for key, sids, day in cov["marks"]:                  # what the pipeline does once they're saved
                db.set_board_seen(key, sids)
                db.set_state(f"since:{key}", day)
            fetched.clear()
            listing["jobs"].append({"id": 13, "title": "Machine Learning Engineer Intern", "location": {"name": "Bengaluru"},
                                    "absolute_url": "https://x/13", "first_published": "2026-11-27T00:00:00Z"})
            second = ats.scan([], db=db, cov={})
        self.assertEqual([j.url for j in second], ["https://x/13"])  # only the new posting
        self.assertEqual(fetched, [sc.GH_LIST % "acme", sc.GH_JOB % ("acme", 13)])   # no detail page for 11, 12

    def test_a_board_cut_mid_read_is_not_remembered(self):
        def fn(out, c):
            c.update(total=2, done=1, marks=[("greenhouse:a", ["1"], TODAY)])
            out.append("a's job")
            c["stop"].wait(5)
            c["marks"].append(("greenhouse:b", ["2"], TODAY))   # b's mark after the cut: never kept
        covs = {}
        jobs = pipeline._source("company boards", [], fn, limit=0.2, covs=covs)
        self.assertEqual(jobs, ["a's job"])
        self.assertEqual(covs["company boards"]["marks"], [("greenhouse:a", ["1"], TODAY)])
        self.assertTrue(covs["company boards"]["cut"])


class LinkedInDescriptions(unittest.TestCase):
    def test_only_jobs_that_pass_the_filters_get_their_page_read(self):
        rows = pd.DataFrame([
            {"site": "linkedin", "title": "Machine Learning Intern", "company": "Acme", "location": "Bengaluru, Karnataka, India",
             "job_url": "https://www.linkedin.com/jobs/view/1", "date_posted": TODAY},
            {"site": "linkedin", "title": "Machine Learning Intern", "company": "Far", "location": "Pune, Maharashtra, India",
             "job_url": "https://www.linkedin.com/jobs/view/2", "date_posted": TODAY},
            {"site": "linkedin", "title": "Senior Sales Manager", "company": "Acme", "location": "Bengaluru, Karnataka, India",
             "job_url": "https://www.linkedin.com/jobs/view/3", "date_posted": TODAY}])
        read = []
        cfg = {**boards.config.cfg("search"), "boards": {"linkedin": {"enabled": True, "fetch_description": False}},
               "queries": [{"term": "ml intern"}], "locations": ["Bengaluru"]}
        real = boards.config.cfg
        with fake_jobspy(lambda **k: rows), \
                mock.patch.object(boards.config, "cfg", lambda n: cfg if n == "search" else real(n)), \
                mock.patch.object(boards, "_tag_indeed_estimates", lambda: None), \
                mock.patch.object(boards, "_linkedin_text", lambda url: read.append(url) or "Python, PyTorch"), \
                mock.patch.object(boards.time, "sleep", lambda s: None):
            jobs = boards.scan([], sites=["linkedin"], quiet=True)
        self.assertEqual(read, ["https://www.linkedin.com/jobs/view/1"])     # Pune and Sales: filtered, never read
        self.assertEqual([bool(j.description) for j in jobs], [True, False, False])

    def test_a_job_we_have_keeps_its_text_and_page_reads_are_capped(self):
        rows = pd.DataFrame([{"site": "linkedin", "title": "Machine Learning Intern", "company": f"Co{i}",
                              "location": "Bengaluru, Karnataka, India", "job_url": f"https://www.linkedin.com/jobs/view/{i}",
                              "date_posted": TODAY} for i in range(5)])
        read = []
        cfg = {**boards.config.cfg("search"), "boards": {"linkedin": {"enabled": True, "max_descriptions": 2}},
               "queries": [{"term": "ml intern"}], "locations": ["Bengaluru"]}
        real = boards.config.cfg
        with fake_jobspy(lambda **k: rows), \
                mock.patch.object(boards.config, "cfg", lambda n: cfg if n == "search" else real(n)), \
                mock.patch.object(boards, "_tag_indeed_estimates", lambda: None), \
                mock.patch.object(boards, "_linkedin_text", lambda url: read.append(url) or "fresh text"), \
                mock.patch.object(boards.time, "sleep", lambda s: None):
            first = boards.scan([], sites=["linkedin"], quiet=True)
            have = {first[0].id: "stored text"}
            jobs = boards.scan([], sites=["linkedin"], quiet=True, stored=have.get)
        self.assertEqual(jobs[0].description, "stored text")                # Co0: already ours, page not read
        self.assertEqual([j.description for j in jobs[1:]], ["fresh text", "fresh text", "", ""])   # cap of 2
        self.assertEqual(len(read), 4)                                       # 2 + 2 across the two scans

    def test_a_429_on_a_job_page_stops_linkedin(self):
        rows = pd.DataFrame([{"site": "linkedin", "title": "Machine Learning Intern", "company": "Acme",
                              "location": "Bengaluru, Karnataka, India", "job_url": "https://www.linkedin.com/jobs/view/1"}])
        calls, errors = [], []

        def page(url):
            raise boards.RateLimited("response status code 429")
        cfg = {**boards.config.cfg("search"), **board_cfg(fetch_description=False)}
        cfg["boards"] = {"linkedin": cfg["boards"]["linkedin"]}
        real = boards.config.cfg
        with fake_jobspy(lambda **k: calls.append(1) or rows), \
                mock.patch.object(boards.config, "cfg", lambda n: cfg if n == "search" else real(n)), \
                mock.patch.object(boards, "_tag_indeed_estimates", lambda: None), \
                mock.patch.object(boards, "_linkedin_text", page), \
                mock.patch.object(boards.time, "sleep", lambda s: None):
            boards.scan(errors, sites=["linkedin"], quiet=True)
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("LinkedIn rate-limited, skipped rest" in e for e in errors), errors)


class Health(unittest.TestCase):
    def test_the_daily_total_says_whether_every_search_ran(self):
        db = DB(HOME / "data" / "cov.db")
        db.log_coverage("indeed", 40, 95)
        db.log_coverage("indeed", 40, 95)
        db.log_coverage("company boards", 22, 22)
        db.log_coverage("internshala", 1, 1)
        ok, text, fix = health._coverage(db)
        self.assertFalse(ok)
        self.assertEqual(text, "scan coverage, last 24 h: company boards 22/22 · indeed 80/95")   # 1/1 sources left out
        self.assertIn("indeed", fix)
        db.log_coverage("indeed", 40, 95)
        ok, text, _ = health._coverage(db)
        self.assertTrue(ok)
        self.assertIn("indeed 95/95", text)                     # capped at the total


if __name__ == "__main__":
    unittest.main()
