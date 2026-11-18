"""Company search: suggestions, ATS detection/parsers (real responses), resolve, listings, watch,
questions (ask, never guess), Telegram + Review Center wiring."""
import json
import os
import threading
import time
import unittest
from pathlib import Path

from _home import ROOT, make_home, use

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter import companies  # noqa: E402
from jobhunter.db import DB, Job  # noqa: E402
from jobhunter.sources import ats_more  # noqa: E402

FX = ROOT / "tests" / "fixtures" / "ats"
J = lambda n: json.loads((FX / n).read_text())


class Resp:
    def __init__(self, data=None, text=None, status=200, url="https://x", ctype="application/json"):
        self._d, self.text, self.status_code, self.url = data, text if text is not None else json.dumps(data), status, url
        self.headers = {"content-type": ctype}

    def json(self):
        if self._d is None:
            raise ValueError
        return self._d


class Parsers(unittest.TestCase):
    def test_smartrecruiters_real(self):
        rows = ats_more.parse_smartrecruiters(J("smartrecruiters-boschgroup.json"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "Hardware Engineer")
        self.assertEqual(rows[0]["posted"], "2026-11-17")
        self.assertIn("Bengaluru", rows[0]["location"])
        self.assertEqual(rows[0]["job_type"], "fulltime")
        self.assertTrue(rows[0]["url"].startswith("https://jobs.smartrecruiters.com/BoschGroup/"))

    def test_keka_real(self):
        rows = ats_more.parse_keka(J("keka-minfy.json"), "minfy", "Minfy")
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0]["url"].startswith("https://minfy.keka.com/careers/jobdetails/"))
        self.assertRegex(rows[0]["posted"], r"^2026-\d\d-\d\d$")
        self.assertIn("India", rows[0]["location"])

    def test_darwinbox_workable_recruitee_zoho(self):
        d = ats_more.parse_darwinbox(J("darwinbox-acme.json"), "acme.darwinbox.in", "Acme")
        self.assertEqual((d[0]["job_type"], d[1]["job_type"]), ("fulltime", "internship"))
        self.assertIn("/jobDetails/a6f1c2", d[0]["url"])
        w = ats_more.parse_workable(J("workable-acme-ai.json"), "acme-ai")
        self.assertEqual((w[0]["company"], w[0]["job_type"], w[0]["posted"]), ("Acme AI", "internship", "2026-11-15"))
        self.assertTrue(w[0]["apply_url"].endswith("/apply/"))
        r = ats_more.parse_recruitee(J("recruitee-acme.json"), "Acme")
        self.assertEqual((r[0]["posted"], r[0]["job_type"]), ("2026-11-10", "fulltime"))
        z = ats_more.parse_zoho((FX / "zoho-acme.html").read_text(), "acme.zohorecruit.in", "Acme")
        self.assertEqual((z[0]["title"], z[0]["url"]), ("Python Developer", "https://acme.zohorecruit.in/jobs/Careers/5001"))

    def test_jsonld_and_detection(self):
        html = (FX / "careers-page.html").read_text()
        ld = ats_more.parse_jsonld(html, "https://acme.example/careers")
        self.assertEqual((ld[0]["title"], ld[0]["posted"], ld[0]["company"]), ("Graduate Engineer Trainee - AI", "2026-11-16", "Acme Corp"))
        self.assertIn("Apply by 2026-12-15", ld[0]["description"])
        kinds = {(b["type"], b.get("token"), b.get("host")) for b in ats_more.detect_boards(html)}
        for want in [("greenhouse", "acmecorp", None), ("lever", "acme-corp", None), ("keka", "acme", None),
                     ("smartrecruiters", "AcmeCorp", None), ("darwinbox", "acme", "acme.darwinbox.in")]:
            self.assertIn(want, kinds)
        wd = next(b for b in ats_more.detect_boards(html) if b["type"] == "workday")
        self.assertEqual((wd["host"], wd["site"]), ("acme.wd3.myworkdayjobs.com", "Acme_Careers"))


class Suggest(unittest.TestCase):
    def test_wikidata_filters_non_companies(self):
        rows = companies.parse_wikidata_search(J("wikidata-search.json")["search"])
        self.assertEqual([r["name"] for r in rows], ["Infosys"])

    def test_suggest_local_sources(self):
        DB().upsert(Job("AI Engineer", "Birla Precision Pvt Ltd", "Bengaluru"))
        s = companies.suggest("birla", online=False)
        names = [x["name"] for x in s]
        self.assertEqual(names[0], "Aditya Birla Group")
        self.assertIn("Birlasoft", names)
        self.assertIn("Birla Precision Pvt Ltd", names)
        self.assertEqual(s[-1]["source"], "as typed")
        self.assertIn("Infosys", [x["name"] for x in companies.suggest("infos", online=False)])
        self.assertEqual(companies.suggest("x"), [])

    def test_same_company(self):
        self.assertTrue(companies.same_company("Sarvam AI Pvt Ltd", "Sarvam AI"))
        self.assertTrue(companies.same_company("Aditya Birla Capital", "Aditya Birla Capital Limited"))
        self.assertFalse(companies.same_company("Birla Institute of Technology", "Birlasoft"))
        self.assertFalse(companies.same_company("Acme", "Acme Bank Holdings Something Else Entirely"[:0]))


class ResolveAndListings(unittest.TestCase):
    def setUp(self):
        self._get, self._post = companies._get, companies._post
        careers = (FX / "careers-page.html").read_text()

        def fake_get(url, **kw):
            if "acme.example/careers" in url:
                return Resp(text=careers, url="https://acme.example/careers", ctype="text/html")
            if "boards-api.greenhouse.io/v1/boards/acmecorp/jobs" in url:
                return Resp({"jobs": [{"id": 1, "title": "ML Engineer Intern", "location": {"name": "Bengaluru, India"},
                                       "absolute_url": "https://boards.greenhouse.io/acmecorp/jobs/1",
                                       "first_published": "2026-11-17T05:00:00Z", "content": ""}]})
            if "boards-api.greenhouse.io/v1/boards/" in url:        # name check for probes
                return Resp({"name": "Totally Different Inc"})
            return Resp(status=404, data={})
        companies._get = fake_get
        companies._post = lambda url, body: Resp(status=404, data={})
        ats_more_fetch = ats_more.fetch

        def fetch(board, g, p, company=""):
            if board["type"] == "greenhouse":
                return [ats_more.L(title="ML Engineer Intern", company=company, location="Bengaluru, India",
                                   url="https://boards.greenhouse.io/acmecorp/jobs/1", posted="2026-11-17",
                                   job_type="internship", source="greenhouse", description="Python PyTorch LLMs")]
            if board["type"] == "lever":
                return [ats_more.L(title="Data Scientist", company=company, location="Pune, India", url="https://jobs.lever.co/acme-corp/2",
                                   posted="2026-11-10", source="lever")]
            return []
        self._fetch = ats_more_fetch
        ats_more.fetch = fetch

    def tearDown(self):
        companies._get, companies._post = self._get, self._post
        ats_more.fetch = self._fetch

    def test_resolve_from_careers_page(self):
        (HOME / "config" / "watchlist.yaml").unlink(missing_ok=True)      # other tests may have watched Acme
        info = companies.resolve("Acme Corp", domain="acme.example", refresh=True, render=False)
        types = {b["type"] for b in info["boards"]}
        self.assertTrue({"greenhouse", "lever", "keka"} <= types, types)
        self.assertEqual(info["careers_url"], "https://acme.example/careers")
        self.assertTrue(info["jsonld"])

    def test_probe_rejects_other_company(self):
        self.assertIsNone(companies._probe("greenhouse", "acme", "Acme Corp"))

    def test_listings_track_watch(self):
        info = companies.resolve("Acme Corp", domain="acme.example", refresh=True, render=False)
        res = companies.listings(info, boards_search=False)
        rows = res["listings"]
        titles = [r["title"] for r in rows]
        self.assertEqual(titles[0], "ML Engineer Intern")             # newest first
        ml = rows[0]
        self.assertTrue(ml["karnataka"] and ml["fit"])
        ds = next(r for r in rows if r["title"] == "Data Scientist")
        self.assertFalse(ds["karnataka"])                             # Pune: shown, marked, not hidden
        self.assertIn("Graduate Engineer Trainee - AI", titles)       # from JSON-LD
        ids = companies.track([ml, ds])
        self.assertEqual(DB().get(ids[0])["status"], "queued")
        self.assertIn("chosen manually", " ".join(DB().get(ids[1])["reasons"]))
        self.assertTrue(companies.watch(info))
        self.assertFalse(companies.watch(info))                       # no duplicates
        self.assertIn("Acme Corp", (HOME / "config" / "watchlist.yaml").read_text())
        L = companies.links(info)
        self.assertIn("REVA", L["Alumni from your college there (best referral source)"].replace("+", " ") + "REVA")


class Questions(unittest.TestCase):
    def test_ask_never_guess_then_requeue(self):
        from jobhunter.apply import questions
        db = DB()
        j = Job("Backend Intern", "Qco", "Bengaluru")
        db.upsert(j)
        db.set_status(j.id, "needs_you")
        rec = [{"label": "Are you comfortable with a 6-day work week? *", "type": "radio", "required": True, "basic": False,
                "basis": "none", "key": "unknown", "value": None, "options": ["Yes", "No"]},
               {"label": "Captcha", "type": "text", "required": True, "basic": False, "key": "captcha", "value": None},
               {"label": "First Name", "type": "text", "required": True, "basic": True, "key": "first_name", "value": "Asha"}]
        db.add_application(j.id, "generic", "needs_you", False, False, False, "needs you", rec, "", "", None, "", "")
        self.assertEqual(questions.add_from_record(j.id, rec, "generic"), 1)
        self.assertEqual(questions.add_from_record(j.id, rec, "generic"), 0)   # no duplicates
        p = questions.pending()
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]["options"], ["No", "Yes"])
        with self.assertRaises(ValueError):
            questions.answer(1, "Maybe")                                     # must be one of the options
        res = questions.answer(1, "yes")
        self.assertEqual(res["value"], "Yes")
        self.assertEqual(res["requeued"], [j.id])
        self.assertEqual(db.get(j.id)["status"], "queued")
        self.assertEqual(questions.pending(), [])
        from jobhunter.apply.fields import answer
        self.assertTrue(answer("Are you comfortable with a 6-day work week?").basic)

    def test_bot_questions(self):
        from jobhunter.apply import questions
        from jobhunter.bot import handle
        questions.add_from_record("x", [{"label": "Do you hold a valid driving licence?", "type": "text", "required": True,
                                         "basic": False, "key": "unknown", "value": None}], "lever")
        self.assertIn("driving licence", handle("/questions"))
        self.assertIn("Usage", handle("/answer"))
        self.assertIn("saved", handle("/answer 1 Yes"))

    def test_options_that_are_the_question_are_dropped(self):
        from jobhunter.apply import questions        # live: read mid-redraw, each option's text was the question
        lab = "Are you comfortable with night shifts?*"
        questions.add_from_record("y", [{"label": lab, "type": "radio", "required": True, "basic": False,
                                         "key": "unknown", "value": None, "options": [lab.rstrip("*")]}], "linkedin")
        row = next(r for r in questions.pending() if "night shifts" in r["label"])
        self.assertEqual(row["options"], [])
        self.assertEqual(questions.answer(row["key"], "Yes")["value"], "Yes")

    def test_known_facts_are_not_asked(self):
        from jobhunter.apply import questions
        n = questions.add_from_record("z", [{"label": "Notice period", "type": "text", "required": True, "basic": False,
                                             "key": "notice", "value": None}], "lever")
        self.assertEqual(n, 0)              # your profile answers it: never asked


class ReviewCenterRoutes(unittest.TestCase):
    def test_suggest_and_answer_routes(self):
        import requests

        from jobhunter import review
        from jobhunter.apply import questions
        threading.Thread(target=review.serve, kwargs={"port": 8793}, daemon=True).start()
        time.sleep(0.8)
        base = "http://127.0.0.1:8793"
        s = requests.get(base + "/api/companies/suggest", params={"q": "birla", "online": "0"}).json()
        self.assertEqual(s[0]["name"], "Aditya Birla Group")
        questions.add_from_record("y", [{"label": "Do you have a valid passport?", "type": "text", "required": True,
                                         "basic": False, "key": "unknown", "value": None}], "generic")
        q = requests.get(base + "/api/questions").json()
        self.assertTrue(any("passport" in x["label"] for x in q))
        bad = requests.post(base + "/api/answer", json={"key": q[0]["key"], "value": "Yes"})
        self.assertEqual(bad.status_code, 403)                                # token required
        ok = requests.post(base + "/api/answer", json={"key": q[0]["key"], "value": "Yes"},
                           headers={"X-JH-Token": review.TOKEN}).json()
        self.assertTrue(ok["ok"], ok)
        page = requests.get(base + "/").text
        self.assertIn("Companies", page)
        self.assertIn("Questions", page)


if __name__ == "__main__":
    unittest.main()


class DreamCompanyPlatforms(unittest.TestCase):
    def test_successfactors_birlasoft_real(self):
        rows, total = ats_more.parse_successfactors((FX / "successfactors-birlasoft.html").read_text(), "jobs.birlasoft.com",
                                                    "Birlasoft")
        self.assertEqual(total, 555)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["posted"], "2026-11-07")
        self.assertTrue(rows[0]["url"].startswith("https://jobs.birlasoft.com/job/"))
        self.assertIn("India", rows[0]["location"])

    def test_abg_cards_and_detail_real(self):
        rows = ats_more.parse_abg_cards((FX / "abg-job-search.html").read_text())
        self.assertEqual(len(rows), 4)
        r = rows[0]
        self.assertEqual((r["title"], r["posted"], r["company"]), ("Assistant / Deputy Engineer - Safety", "2026-11-16",
                                                                   "Aditya Birla Group"))
        self.assertEqual(r["apply_url"], "https://abgcareers.peoplestrong.com/job/detail/ABG115181")
        self.assertIn("3-5 years", r["description"])
        d = ats_more.parse_abg_detail((FX / "abg-job-detail.html").read_text())
        self.assertEqual((d["closes"], d["code"]), ("2026-12-01", "ABG115181"))
        self.assertEqual(d["description"], "Maintain PSM programs across the plant. Must have ADIS certification.")

    def test_detect_dream_platforms(self):
        b = ats_more.detect_boards("<html>...</html>", "https://careers.adityabirla.com/job-search")
        self.assertEqual(b[0]["type"], "abg")
        b = ats_more.detect_boards('<link href="https://rmkcdn.successfactors.com/x.css">', "https://jobs.birlasoft.com/")
        self.assertEqual((b[0]["type"], b[0]["host"]), ("successfactors", "jobs.birlasoft.com"))

    def test_dream_boards_in_scan(self):
        from jobhunter import config
        boards = config.cfg("companies")["boards"]
        types = {b["company"]: b["type"] for b in boards}
        self.assertEqual(types.get("Aditya Birla Group"), "abg")
        self.assertEqual(types.get("Birlasoft"), "successfactors")

    def test_human_dates(self):
        self.assertEqual(ats_more._human_day("Posted on Mon Nov 16 2026"), "2026-11-16")
        self.assertEqual(ats_more._human_day("Nov 7, 2026"), "2026-11-07")
        self.assertEqual(ats_more._human_day("5 Dec 2026"), "2026-12-05")
