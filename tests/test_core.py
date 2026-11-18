import datetime as dt
import json
import unittest
from pathlib import Path

from _home import use, ROOT, make_home

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter import config  # noqa: E402
from jobhunter.apply.fields import answer, classify, learn, pick_option  # noqa: E402
from jobhunter.db import DB, Job, job_id  # noqa: E402
from jobhunter.eligibility import check  # noqa: E402
from jobhunter.score import is_dream, score, years_required  # noqa: E402
from jobhunter.sources import pages  # noqa: E402
from jobhunter.sources.boards import row_to_job  # noqa: E402

TODAY = dt.date(2026, 11, 17)


def J(title, company="Acme", loc="Bengaluru, Karnataka, India", **kw):
    return score(Job(title, company, loc, **kw), TODAY)


class Scoring(unittest.TestCase):
    def test_karnataka_only(self):
        self.assertEqual(J("ML Engineer", loc="Pune, Maharashtra").status, "filtered")
        self.assertEqual(J("ML Engineer", loc="Hyderabad").status, "filtered")
        self.assertEqual(J("ML Engineer", loc="Bangalore Urban").status, "new")
        self.assertEqual(J("ML Engineer", loc="Mysuru, KA").status, "new")
        self.assertEqual(J("ML Engineer", loc="Remote, India", is_remote=True).status, "new")
        self.assertEqual(J("ML Engineer", loc="Remote, United States", is_remote=True).status, "filtered")

    def test_seniority_and_experience(self):
        self.assertEqual(J("Senior ML Engineer").status, "filtered")
        self.assertEqual(J("ML Engineer", description="Requires 3+ years of experience").status, "filtered")
        self.assertEqual(J("ML Engineer", description="0-1 years experience, freshers welcome").status, "new")
        self.assertEqual(years_required("Experience: 2-5 Yrs"), 2)

    def test_levels_stale_and_gigs(self):
        import datetime as _dt
        for t in ("Data Scientist III", "(IND) Software Engineer II", "Security Engineer IV", "SDE-2 Backend"):
            self.assertEqual(J(t).status, "filtered", t)
        for t in ("Software Engineer", "Engineer I", "SDE I - Golang", "Forward Deployed Engineer Intern, 2027"):
            self.assertNotEqual(J(t).status, "filtered", t)
        old = str(_dt.date.today() - _dt.timedelta(days=400))
        self.assertEqual(J("ML Engineer", posted_at=old).status, "filtered")
        self.assertNotEqual(J("ML Engineer", posted_at=str(_dt.date.today())).status, "filtered")
        gig, real = J("Data Scientist (Remote | $150–$200/hr)", loc="Bengaluru"), J("Data Scientist", loc="Bengaluru")
        self.assertLess(gig.score, real.score - 15)

    def test_stipend_floor(self):
        self.assertEqual(J("AI Intern", pay_min=8000, pay_period="month", job_type="internship").status, "filtered")
        self.assertEqual(J("AI Intern", pay_min=25000, pay_period="month", job_type="internship").status, "new")
        self.assertEqual(J("AI Intern", job_type="internship").status, "new")   # unlisted stipend kept
        self.assertEqual(J("Software Engineer", pay_min=300000, pay_period="year").status, "filtered")

    def test_non_tech_dropped(self):
        for t in ("Sales Executive", "Telecaller", "Maintenance Supervisor"):
            self.assertEqual(J(t).status, "filtered", t)

    def test_dream_company_bonus(self):
        self.assertEqual(is_dream("Aditya Birla Capital Ltd"), "Aditya Birla Group")
        self.assertEqual(is_dream("Birlasoft Limited"), "Birlasoft")
        self.assertIsNone(is_dream("Birla Institute of Technology"))
        d = J("Data Scientist", "Aditya Birla Capital", description="Python SQL")
        n = J("Data Scientist", "Nobody Ltd", description="Python SQL")
        self.assertTrue(d.dream)
        self.assertEqual(d.score - n.score, 15)
        self.assertEqual(J("AI Engineer", "Birlasoft", description="2-4 years").status, "new")  # dream keeps it, penalised

    def test_ranking(self):
        good = J("Machine Learning Engineer Intern", description="Python PyTorch LLMs RAG FastAPI", job_type="internship",
                 posted_at="2026-11-17")
        meh = J("QA Engineer", description="manual testing")
        self.assertGreater(good.score, 75)
        self.assertLess(meh.score, good.score)

    def test_scam(self):
        self.assertLess(J("Python Developer Intern", description="Pay a refundable deposit of 2000").score, 30)


class Dedupe(unittest.TestCase):
    def test_same_job_across_boards(self):
        self.assertEqual(job_id("Sarvam AI", "ML Engineer", "Bengaluru, Karnataka"),
                         job_id("Sarvam AI Pvt Ltd", "ML Engineer (II)", "Bangalore, India"))
        db = DB(HOME / "data" / "dedupe.db")
        a = Job("ML Engineer", "Sarvam AI", "Bengaluru", url="https://www.linkedin.com/jobs/view/1", source="linkedin")
        b = Job("ML Engineer", "Sarvam AI Pvt Ltd", "Bangalore", url="https://jobs.ashbyhq.com/sarvam/1", source="ashby",
                description="longer description " * 10)
        self.assertTrue(db.upsert(a))
        self.assertFalse(db.upsert(b))
        row = db.get(a.id)
        self.assertEqual(set(row["sources"]), {"linkedin", "ashby"})
        self.assertIn("ashbyhq", row["url"])           # company link preferred for applying
        self.assertGreater(len(row["description"]), 50)

    def test_merge_keeps_advanced_status(self):
        a, b = DB(HOME / "data" / "a.db"), DB(HOME / "data" / "b.db")
        j = Job("AI Engineer", "Foo", "Bengaluru")
        a.upsert(j)
        b.upsert(j)
        b.upsert(Job("Data Scientist", "Bar", "Bengaluru"))
        a.set_status(j.id, "applied")
        b.set_status(j.id, "alerted")
        added, updated = a.merge_from(b.path)
        self.assertEqual(added, 1)
        self.assertEqual(a.get(j.id)["status"], "applied")


class Eligibility(unittest.TestCase):
    def setUp(self):
        self.p = config.cfg("profile")
        self.rules = {r["name"]: r for r in config.cfg("govt")["rules"]}

    def test_sc_age_relaxation_central(self):
        v = check("Upper age limit 28 years", {}, self.p, "central", on=dt.date(2027, 9, 1))
        self.assertTrue(any("28+5" in r for r in v.reasons), v.lines())
        v = check("Upper age limit 25 years", {}, self.p, "central", on=dt.date(2036, 6, 1))   # 31 > 25+5
        self.assertEqual(v.verdict, "ineligible")

    def test_state_age_with_temporary_relaxation(self):
        r = self.rules["Karnataka state posts"]
        v = check("KPSC recruitment", r, self.p, "state", on=dt.date(2027, 6, 1))
        self.assertTrue(any("40+5" in x for x in v.reasons), v.lines())
        v = check("KPSC recruitment", r, self.p, "state", on=dt.date(2028, 6, 1))
        self.assertTrue(any("limit 40" in x and "+5" not in x for x in v.reasons), v.lines())

    def test_final_year_blocked(self):
        v = check("Final year students are not eligible to apply. Last date 10-12-2026", {}, self.p, "state",
                  on=dt.date(2026, 12, 10))
        self.assertEqual(v.verdict, "ineligible")

    def test_branch_risk_flagged(self):
        v = check("Eligibility: B.E./B.Tech in Computer Science or Information Technology", {}, self.p, "central")
        self.assertEqual(v.verdict, "check")
        self.assertTrue(any("branch" in r for r in v.risks))
        v = check("B.Tech in CSE / Artificial Intelligence and Machine Learning or allied", {}, self.p, "central",
                  on=dt.date(2027, 8, 1))
        self.assertFalse(any("branch" in r for r in v.risks), v.lines())

    def test_gate_paper(self):
        v = check("Selection through GATE score in CS", {"gate_required": True, "gate_papers": ["CS"]}, self.p, "central")
        self.assertEqual(v.verdict, "ineligible")
        v = check("GATE", self.rules["NIC Scientist-B"], self.p, "central", on=dt.date(2028, 4, 1))
        self.assertFalse(v.blockers, v.lines())

    def test_kannada_and_sc_state(self):
        v = check("KEA notification", self.rules["Karnataka state posts"], self.p, "state")
        self.assertTrue(any("Kannada" in r for r in v.reasons))
        self.assertTrue(any("SC reservation" in r for r in v.reasons))


class Fields(unittest.TestCase):
    def test_basic_facts(self):
        for label, want in [("First Name", "Asha"), ("Father's Name", "Ramesh K"),
                            ("Mother's Name", "Lakshmi"), ("Date of Birth", "21/07/2004"),
                            ("10th Percentage", 89), ("12th %", 83), ("CGPA", 8.0), ("Year of Graduation", "2027")]:
            a = answer(label)
            self.assertEqual(a.value, want, label)
            self.assertTrue(a.basic, label)
        self.assertEqual(answer("Date of Birth", "date").value, "2004-07-21")
        self.assertEqual(answer("DOB", "text", "MM/DD/YYYY").value, "07/21/2004")

    def test_yes_no_degree_questions_follow_graduation_year(self):
        yn = ["Yes", "No"]                     # test profile: B.Tech 2023-2027, so in progress until 2027
        for label, want in [("Have you completed your Bachelor's degree?", "No"),
                            ("Are you currently pursuing a Bachelor's degree?", "Yes"),
                            ("Will you graduate in 2027?", "Yes"), ("Are you graduating in 2026?", "No"),
                            ("Will you complete your B.Tech degree by 2028?", "Yes"),
                            ("Have you completed a Master's degree?", None)]:      # no masters in profile: ask
            a = answer(label, "radio", "", yn)
            self.assertEqual(a.value, want, label)
            self.assertEqual(a.basis, "profile" if want else "none", label)
        self.assertEqual(answer("Highest degree", "select", "", ["B.Tech", "M.Tech"]).value, "B.Tech")

    def test_not_basic(self):
        for label in ("Expected CTC", "Why do you want to join us?", "Gender",
                      "How many years of experience do you have with Kubernetes?", "Caste category"):
            self.assertFalse(answer(label, "textarea" if "Why" in label else "text").basic, label)

    def test_name_is_not_company(self):
        self.assertEqual(classify("Company Name"), "unknown")
        self.assertEqual(classify("College Name"), "college")
        self.assertEqual(classify("Name"), "full_name")

    def test_unconfirmed_year_not_basic(self):
        self.assertFalse(answer("Class 12 passing year").basic)

    def test_learned_answers_become_basic(self):
        q = "How many years of experience do you have with Docker?"
        self.assertFalse(answer(q).basic)
        learn(q, "1", "test")
        self.assertTrue(answer(q).basic)
        self.assertEqual(answer(q).value, "1")

    def test_option_matching(self):
        self.assertEqual(pick_option("B.Tech", ["Select", "B.E./B.Tech", "M.Tech"]), "B.E./B.Tech")
        self.assertEqual(pick_option("Artificial Intelligence and Machine Learning",
                                     ["Computer Science", "Artificial Intelligence and Data Science", "AI & ML"]), "AI & ML")
        self.assertIsNone(pick_option("Artificial Intelligence and Machine Learning",
                                      ["Computer Science", "Artificial Intelligence and Data Science"]))
        self.assertEqual(pick_option("0", ["Yes", "No"]), "No")
        self.assertEqual(pick_option("0", ["0-1 years", "1-3 years"]), "0-1 years")
        self.assertEqual(pick_option("India", ["Afghanistan", "India (+91)"]), "India (+91)")


class Sources(unittest.TestCase):
    def test_jobspy_row(self):
        j = row_to_job({"site": "naukri", "title": "AI Engineer Intern", "company": "Foo", "location": "Bengaluru",
                        "job_url": "https://www.naukri.com/x", "min_amount": 20000.0, "interval": "monthly",
                        "currency": "INR", "date_posted": dt.date(2026, 11, 16), "description": "Python",
                        "emails": "hr@foo.in", "experience_range": "0-1 Yrs", "is_remote": float("nan")})
        self.assertEqual((j.job_type, j.pay_period, j.emails), ("internship", "month", ["hr@foo.in"]))
        self.assertIn("Experience: 0-1 Yrs", j.description)

    def test_ats_offline_with_hunter_fixtures(self):
        from jobhunter.sources import ats
        sc = ats.scout()
        data = json.loads((ROOT / "tests/hunter/fixtures/hunter-scout/lever-palantir.json").read_text())
        jobs = sc.from_lever({"company": "Palantir", "type": "lever"}, data)
        self.assertTrue(jobs and all("title" in j for j in jobs))

    def test_page_watch_and_deadline(self):
        html = """<table><tr><td><a href="/a.pdf">Recruitment of Project Engineer (Computer Science) Advt 5/2026</a>
                  Last date: 12-12-2026</td></tr><tr><td><a href="/r">Result of written test</a></td></tr></table>"""
        links = pages.extract_links(html, "https://x.gov.in/careers")
        inc = config.cfg("govt")["relevance"]["include"]
        exc = config.cfg("govt")["relevance"]["exclude"]
        rel = [l for l in links if pages.relevant(l, inc, exc)]
        self.assertEqual(len(rel), 1)
        self.assertTrue(rel[0].is_pdf)
        self.assertEqual(pages.find_deadline(rel[0].context), "2026-12-12")
        self.assertEqual(pages.find_deadline("apply on or before 5th January 2027"), None)  # ordinal: not parsed
        self.assertEqual(pages.find_deadline("Closing date: 05 Jan 2027"), "2027-01-05")

    def test_govt_scan_baseline_then_new(self):
        from jobhunter.sources import govt
        from jobhunter.util import Http

        pages_html = {"v1": '<a href="/n1.pdf">Recruitment notification Scientist B</a>',
                      "v2": '<a href="/n1.pdf">Recruitment notification Scientist B</a>'
                            '<a href="/n2.pdf">Recruitment of Project Associate - Machine Learning. Last date 30-12-2026</a>'}
        state = {"v": "v1"}

        class R:
            status_code = 200
            content = b""
            @property
            def text(self):
                return pages_html[state["v"]]
        Http.get = lambda self, url, **kw: R()
        Http.allowed = lambda self, url: True
        db = DB(HOME / "data" / "govt.db")
        g = config.cfg("govt")
        g["sources"] = [{"id": "iisc", "name": "IISc", "url": "https://iisc.ac.in/careers/", "kind": "central"}]
        self.assertEqual(govt.scan(db, []), [])            # first visit = baseline
        state["v"] = "v2"
        new = govt.scan(db, [])
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0].deadline, "2026-12-30")
        self.assertIn("tech", new[0].flags)
        config.reload()


class Outputs(unittest.TestCase):
    def test_cover_template_passes_verifier(self):
        from jobhunter import tailor
        r = config.cfg("resume")
        r["projects"][0]["bullets"] = ["Built a FastAPI + MLflow pipeline with 12 automated tests in GitHub Actions"]
        text, rep, how = tailor.cover({"id": "x", "title": "ML Engineer Intern", "company": "Sarvam AI",
                                       "description": "FastAPI MLflow"})
        self.assertEqual(how, "template")
        self.assertIn("Sarvam AI", text)
        fails = [c for c in rep["checks"] if c["status"] == "FAIL" and c["check"] in ("numbers", "placeholders", "company", "other names")]
        self.assertFalse(fails, rep["checks"])
        config.reload()

    def test_resume_pdf_and_dashboard(self):
        from jobhunter import dashboard, resume_pdf
        out = resume_pdf.build(HOME / "data" / "r.pdf", {"id": "x", "description": "Python FastAPI"})
        self.assertTrue(out.read_bytes().startswith(b"%PDF"))
        DB().upsert(Job("AI Engineer", "Foo", "Bengaluru"))
        f = Path(dashboard.build())
        self.assertIn("AI Engineer", f.read_text())


if __name__ == "__main__":
    unittest.main()
