"""v2.4 scoring, ranking, freshness, repost dedupe and DB indexes."""
import datetime as dt
import unittest

from _home import use, make_home

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter.db import DB, Job  # noqa: E402
from jobhunter.score import rank_key, score  # noqa: E402

TODAY = dt.date(2026, 11, 25)


def J(title, company="Acme", loc="Bengaluru, Karnataka, India", **kw):
    return score(Job(title, company, loc, **kw), TODAY)


def early(j):
    return any(r.startswith("early-career") for r in j.reasons)


class EarlyCareer(unittest.TestCase):
    def test_word_boundaries(self):
        hp = J("Data Engineer", "HP", description="Work with internal teams and international partners on our internet "
                                                   "platform. Postgraduate degree preferred. Associated tools: Spark.")
        self.assertFalse(early(hp), hp.reasons)
        linde = J("Software Engineer - AI Hub", "Linde", description="Build internal AI tools for Linde International.")
        self.assertFalse(early(linde), linde.reasons)
        for d in ("Internship for final-year students", "Freshers welcome", "Open to 2026 graduates", "Interns join us"):
            self.assertTrue(early(J("Data Engineer", description=d)), d)

    def test_interns_title_is_an_internship(self):
        j = J("Software Development Engineer Interns", "Wellfound Co", job_type="fulltime",
              pay_min=10000, pay_period="month", currency="INR")
        self.assertEqual(j.status, "filtered")                      # stipend floor applies, not the CTC floor
        self.assertIn("stipend", j.reasons[0])
        self.assertNotEqual(J("Software Engineer", "Intertrust").status, "filtered")


class TitleAndSkills(unittest.TestCase):
    def test_fresher_roles_score_well(self):
        py = J("Python Development Intern", "Byteworks", job_type="internship", pay_min=15000, pay_period="month",
               currency="INR", description="Skills: Python, Django, Flask, Machine Learning, REST API. Build backend services.")
        sd = J("Software Development Intern", "Codenest", job_type="internship",
               description="Skills: Python, JavaScript, MongoDB, Prompt Engineering")
        gen = J("Gen AI Programmers", "Promptly Labs", job_type="fulltime",
                description="1+ years. Python, LLMs, RAG, LangGraph. Build agents.")
        self.assertGreaterEqual(py.score, 55, py.reasons)
        self.assertGreaterEqual(sd.score, 55, sd.reasons)
        self.assertGreaterEqual(gen.score, 45, gen.reasons)
        for t in ("LLM Engineer", "Computer Vision Engineer", "NLP Engineer", "ML Intern", "AI Intern",
                  "Data Science Intern", "SDE Intern", "Generative AI Developer", "AI Developer"):
            self.assertTrue(any(r.startswith("title fits") for r in J(t).reasons), t)

    def test_skill_plural_and_abbreviation(self):
        j = J("Backend Developer", description="Must know REST API design and FastAPI. LLM experience a plus.")
        hit = next(r for r in j.reasons if r.startswith("skills match"))
        for sk in ("REST APIs", "FastAPI", "LLMs"):
            self.assertIn(sk, hit)
        self.assertNotIn("C++", next(r for r in J("Backend Developer", description="C and Python").reasons
                                     if r.startswith("skills match")))


class Penalties(unittest.TestCase):
    def test_agencies_demoted(self):
        real = J("Data Scientist", "Zoho", description="Python")
        for co in ("NexaCare Talent Mobility", "Antal International", "Bright Staffing"):
            a = J("Data Scientist", co, description="Python")
            self.assertIn("agency", a.flags, co)
            self.assertIn("⚠ recruitment agency (−15)", a.reasons)
            self.assertEqual(real.score - a.score, 15, co)

    def test_usd_salary_is_not_a_gig(self):
        jerry = J("Data Scientist $140k – $160k", "Jerry.ai", loc="Remote, India", is_remote=True)
        self.assertFalse(any("gig" in r for r in jerry.reasons), jerry.reasons)
        for t in ("Data Scientist ($40/hr)", "AI Trainer $30 per hour", "Data Scientist $5 per task"):
            self.assertTrue(any("gig" in r for r in J(t, loc="Bengaluru").reasons), t)


class Freshness(unittest.TestCase):
    def test_boosts(self):
        base = J("ML Engineer", posted_at="2026-11-20")
        fresh = J("ML Engineer", posted_at="2026-11-24")
        self.assertIn("posted < 48 h (+6)", fresh.reasons)
        self.assertNotIn("posted < 48 h (+6)", base.reasons)
        few = J("ML Engineer", posted_at="2026-11-20", description="Be an early applicant")
        self.assertEqual(few.score - base.score, 4)
        self.assertIn("few applicants (+4)", few.reasons)
        tagged = score(Job("ML Engineer", "Acme", "Bengaluru", posted_at="2026-11-20", flags=["few-applicants"]), TODAY)
        self.assertEqual(tagged.score - base.score, 4)
        rep = score(Job("ML Engineer", "Acme", "Bengaluru", posted_at="2026-11-20", flags=["repost"]), TODAY)
        self.assertEqual(base.score - rep.score, 10)
        self.assertIn("repost", rep.flags)

    def test_rank_key(self):
        rows = [{"id": "a", "score": 60, "posted_at": "2026-11-10", "found_at": "2026-11-24T10:00:00+05:30"},
                {"id": "b", "score": 70, "posted_at": None, "found_at": "2026-11-01T10:00:00+05:30"},
                {"id": "c", "score": 60, "posted_at": None, "found_at": "2026-11-20T10:00:00+05:30"},
                {"id": "d", "score": 60, "posted_at": "2026-11-22", "found_at": "2026-11-22T10:00:00+05:30"}]
        self.assertEqual([r["id"] for r in sorted(rows, key=lambda r: rank_key(r, TODAY))], ["b", "d", "c", "a"])


class SaveRules(unittest.TestCase):
    def setUp(self):
        from jobhunter import pipeline
        self.save = pipeline.save
        self.db = DB(HOME / "data" / f"{self._testMethodName}.db")

    def test_old_posting_filtered_when_first_seen(self):
        old = str(dt.date.today() - dt.timedelta(days=20))
        j = Job("ML Engineer", "Acme", "Bengaluru", url="https://x.example/1", posted_at=old)
        self.save(self.db, [j])
        row = self.db.get(j.id)
        self.assertEqual(row["status"], "filtered")
        self.assertIn(f"old posting (posted {old})", row["reasons"][0])
        fresh = Job("AI Engineer", "Acme", "Bengaluru", url="https://x.example/2", posted_at=str(dt.date.today()))
        self.save(self.db, [fresh])
        self.assertEqual(self.db.get(fresh.id)["status"], "new")

    def test_repost_folds_into_old_row(self):
        a = Job("Machine Learning Engineer", "Sarvam AI Technologies", "Bengaluru", url="https://x.example/a",
                posted_at=str(dt.date.today()))
        b = Job("Machine Learning Engineer", "Sarvam AI Services", "Bengaluru, Karnataka", url="https://x.example/b",
                posted_at=str(dt.date.today()))
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(self.save(self.db, [a]), [a.id])
        self.assertEqual(self.db.find_repost(b.company, b.title, b.location), a.id)
        self.save(self.db, [b])
        old, new = self.db.get(a.id), self.db.get(b.id)
        self.assertIn("https://x.example/b", old["urls"])
        self.assertEqual(old["status"], "new")
        self.assertEqual(new["status"], "filtered")
        self.assertIn("repost", new["flags"])
        self.assertIn(f"repost of {a.id}", new["reasons"][0])
        self.assertIsNone(self.db.find_repost("Other Co", b.title, b.location))
        self.db.c.execute("UPDATE jobs SET found_at='2020-01-01T00:00:00+05:30'")
        self.assertIsNone(self.db.find_repost(b.company, b.title, b.location))


class Indexes(unittest.TestCase):
    def test_indexes(self):
        db = DB(HOME / "data" / "idx.db")

        def cols(table):
            return {tuple(c[2] for c in db.c.execute(f"PRAGMA index_info({i[1]})"))
                    for i in db.c.execute(f"PRAGMA index_list({table})")}
        jobs = cols("jobs")
        for want in [("status",), ("kind", "status", "score"), ("found_at",), ("company",), ("posted_at",),
                     ("deadline",)]:
            self.assertIn(want, jobs)
        self.assertIn(("job_id",), cols("applications"))
        self.assertIn(("job_id", "kind", "at"), cols("events"))
        self.assertIn(("kind", "at"), cols("events"))


if __name__ == "__main__":
    unittest.main()
