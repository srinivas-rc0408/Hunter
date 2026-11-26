"""Paste a link → the company: homepage, ATS board, LinkedIn job (DB first, then page), LinkedIn company, blocked site."""
import unittest
from pathlib import Path

from _home import make_home, use

HOME = make_home()
FIX = Path(__file__).parent / "fixtures" / "links"


def setUpModule():
    use(HOME)


from jobhunter import linkinfo  # noqa: E402
from jobhunter.db import DB, Job  # noqa: E402


class R:
    def __init__(self, text, status=200, url=""):
        self.text, self.status_code, self.url = text, status, url


def site(pages: dict):
    calls = []

    def get(url, **kw):
        calls.append(url)
        hit = next((v for k, v in pages.items() if k in url), None)
        if hit is None:
            raise ConnectionError("offline")
        text, status = hit if isinstance(hit, tuple) else (hit, 200)
        return R(text, status, url)
    get.calls = calls
    return get


class Identify(unittest.TestCase):
    def test_homepage_facts_with_source(self):
        i = linkinfo.identify("acmerobotics.example/", site({"acmerobotics.example": (FIX / "homepage.html").read_text()}))
        self.assertEqual((i["name"], i["domain"], i["read"]), ("Acme Robotics", "acmerobotics.example", True))
        self.assertEqual(i["boards"][0]["type"], "greenhouse")
        facts = {k: (v, src) for k, v, src in i["facts"]}
        self.assertEqual(facts["Founded"], ("2015", "https://acmerobotics.example/"))
        self.assertIn("warehouse robots", facts["What it says it does"][0])
        self.assertEqual(facts["Address on its site"][0], "Bengaluru, Karnataka, IN")

    def test_greenhouse_board_url(self):
        i = linkinfo.identify("https://boards.greenhouse.io/acmerobotics/jobs/123",
                              site({"greenhouse.io": (FIX / "greenhouse.html").read_text()}))
        self.assertEqual((i["kind"], i["name"], i["boards"][0]["token"]), ("ats", "Acme Robotics", "acmerobotics"))

    def test_greenhouse_board_known_even_when_page_blocks(self):
        i = linkinfo.identify("https://boards.greenhouse.io/acmerobotics", site({}))
        self.assertEqual((i["kind"], i["name"]), ("ats", "Acmerobotics"))

    def test_linkedin_job_from_our_db_first(self):
        db = DB()
        j = Job(title="ML Intern", company="Acme Robotics", location="Bengaluru", url="https://www.linkedin.com/jobs/view/4123456789",
                source="linkedin")
        db.upsert(j)
        get = site({})
        i = linkinfo.identify("https://www.linkedin.com/jobs/view/ml-intern-at-acme-4123456789/", get, db)
        self.assertEqual((i["name"], i["job_id"], get.calls), ("Acme Robotics", j.id, []))

    def test_linkedin_job_page(self):
        i = linkinfo.identify("https://www.linkedin.com/jobs/search/?currentJobId=4999999999",
                              site({"jobPosting/4999999999": (FIX / "linkedin-job.html").read_text()}), DB())
        self.assertEqual((i["kind"], i["name"], i["read"]), ("linkedin-job", "Acme Robotics", True))

    def test_linkedin_login_wall(self):
        i = linkinfo.identify("https://www.linkedin.com/jobs/view/4888888888", site({"jobPosting": ("", 429)}), DB())
        self.assertFalse(i["read"])
        self.assertIn("HTTP 429", i["why"])
        self.assertEqual(i["name"], "")                       # never guessed

    def test_linkedin_company_page(self):
        i = linkinfo.identify("linkedin.com/company/acme-robotics/", site({}))
        self.assertEqual((i["kind"], i["name"]), ("linkedin-company", "Acme Robotics"))

    def test_blocked_site_says_couldnt_read(self):
        i = linkinfo.identify("https://acme.example", site({"acme.example": (FIX / "blocked.html").read_text()}))
        self.assertFalse(i["read"])
        self.assertIn("couldn't read", i["why"])
        self.assertEqual([f[0] for f in i["facts"]], ["Name"])  # only the name, marked as from the web address

    def test_is_url(self):
        self.assertTrue(linkinfo.is_url("https://zoho.com/careers"))
        self.assertTrue(linkinfo.is_url("zoho.com"))
        self.assertFalse(linkinfo.is_url("tell me about zoho"))


if __name__ == "__main__":
    unittest.main()


class BotLink(unittest.TestCase):
    def test_pasted_link_db_part_first_then_profile(self):
        from jobhunter import bot, companies, intel
        db = DB()
        db.c.execute("DELETE FROM jobs WHERE company='Acme Robotics'")     # test_grounding seeds one in the shared DB
        db.c.commit()
        j = Job(title="Computer Vision Intern", company="Acme Robotics", location="Bengaluru, Karnataka, India",
                url="https://acme.example/jobs/1", source="greenhouse", description="Python, PyTorch, OpenCV")
        db.upsert(j)
        sent = []
        orig = (companies._get, companies.news, bot._company_report)
        companies._get = site({"acmerobotics.example": (FIX / "homepage.html").read_text()})
        companies.news = lambda *a, **k: [{"title": "Acme Robotics raises ₹120 crore in Series B round", "date": "2026-11-01",
                                           "url": "https://news.example/a", "source": "News"}]
        bot._company_report = lambda pick, db, watch=False: [f"live {pick['name']}"]
        try:
            out = bot.handle("https://acmerobotics.example/", db, progress=sent.append)
        finally:
            companies._get, companies.news, bot._company_report = orig
            bot._LINKS.clear()
        self.assertIn("1 postings seen", sent[0])               # our DB, before anything live
        self.assertEqual(out[0], "live Acme Robotics")
        self.assertIn("funding: 2026-11-01", out[-1])
        self.assertIn('Founded: 2015 <i>(<a href="https://acmerobotics.example/">source</a>)</i>', out[-1])
        self.assertIn("Python", intel._skills())

    def test_blocked_link_says_so(self):
        from jobhunter import bot, companies
        orig = companies._get
        companies._get = site({"acme.example": (FIX / "blocked.html").read_text()})
        try:
            sent = []
            orig_r = bot._company_report
            bot._company_report = lambda pick, db, watch=False: ["live"]
            bot.handle("https://acme.example", DB(), progress=sent.append)
        finally:
            companies._get, bot._company_report = orig, orig_r
            bot._LINKS.clear()
        self.assertIn("couldn't read", sent[0])
        self.assertIn("nothing on this profile is filled in from memory", sent[0])
