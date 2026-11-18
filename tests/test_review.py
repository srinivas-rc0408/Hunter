"""Review Center + approve flow + tailoring + privacy guard."""
import os
import subprocess
import threading
import time
import unittest
from pathlib import Path

import yaml
from _home import use, ROOT, make_home, write_pdf

HOME = make_home()


def setUpModule():
    use(HOME)

os.environ.setdefault("DISPLAY", ":99")   # the approve test runs headless; the service checks a display exists
sp = HOME / "config" / "search.yaml"
d = yaml.safe_load(sp.read_text())
d["apply"].update(headless=True, pause_range_sec=[0, 0])
sp.write_text(yaml.safe_dump(d))

from jobhunter import config  # noqa: E402

config.reload()
from jobhunter.apply.fields import answer  # noqa: E402
from jobhunter.db import DB, Job  # noqa: E402
from jobhunter.score import score  # noqa: E402

FX = ROOT / "tests" / "fixtures" / "forms"


def seed(title, company, form, desc, intern=False):
    j = score(Job(title, company, "Bengaluru, Karnataka", url=(FX / f"{form}.html").as_uri(), source="linkedin",
                  description=desc, posted_at="2026-11-17", job_type="internship" if intern else None))
    db = DB()
    db.upsert(j)
    db.set_score(j.id, j.score, j.reasons, j.flags, j.dream)
    return j


class Answers(unittest.TestCase):
    def test_ctc_formats(self):
        prof = config.cfg("profile")
        prof["answers"]["expected_ctc_lpa"] = 8
        prof["answers"]["expected_stipend_month"] = 30000
        self.assertEqual(answer("Expected CTC (LPA)").value, "8")
        self.assertEqual(answer("Expected CTC", "number").value, "800000")
        self.assertEqual(answer("Expected CTC").value, "8 LPA")
        self.assertEqual(answer("Expected Stipend").value, "30000 per month")
        self.assertEqual(answer("Expected CTC", job={"title": "AI Intern"}).value, "30000 per month")
        config.reload()

    def test_addresses(self):
        self.assertEqual(answer("Permanent Address Pincode").key, "perm_pincode")
        a = answer("Native District")
        self.assertIn("Mysore", a.alts)


class Tailoring(unittest.TestCase):
    def test_truth_checked_one_page(self):
        from pypdf import PdfReader

        from jobhunter import resume_pdf
        r = resume_pdf.tailor_job({"id": "tailor000001", "title": "ML Engineer", "company": "X",
                                   "description": "Python FastAPI Docker LangGraph RAG PostgreSQL"}, use_llm=False)
        self.assertTrue(r["clean"])
        self.assertGreaterEqual(r["after"], r["before"])
        self.assertEqual(len(PdfReader(r["pdf"]).pages), 1)
        self.assertTrue((HOME / "hunter" / "resumes" / "base.md").is_file())


class ReviewCenter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        write_pdf(HOME / "config" / "resume.pdf")
        from jobhunter import review
        from jobhunter.apply.runner import apply
        cls.basic = seed("Machine Learning Engineer Intern", "Sarvam AI", "basic_ats", "Python FastAPI LLMs RAG", True)
        cls.hard = seed("Data Scientist", "Acme Analytics", "needs_you", "SQL Python freshers pandas")
        apply(ids=[cls.hard.id], headless=True)              # queue mode → needs_you, recorded
        apply(ids=[cls.basic.id], dry=True, headless=True)   # preview, recorded
        cls.port = 8791
        threading.Thread(target=review.serve, kwargs={"port": cls.port}, daemon=True).start()
        time.sleep(0.8)
        cls.token = review.TOKEN

    def test_records_every_field(self):
        apps = DB().applications(self.hard.id)
        self.assertEqual(apps[0]["outcome"], "needs_you")
        labels = [f["label"] for f in apps[0]["fields"]]
        self.assertTrue(any("Expected CTC" in x for x in labels))
        self.assertTrue(Path(apps[0]["screenshot"]).is_file())
        self.assertEqual(DB().get(self.hard.id)["status"], "needs_you")

    def test_page_and_security(self):
        import requests
        base = f"http://127.0.0.1:{self.port}"
        self.assertIn("Review Center", requests.get(base).text)
        st = requests.get(base + "/api/state").json()
        self.assertTrue(any(j["apps"] for j in st["jobs"]))
        self.assertEqual(requests.post(f"{base}/api/mark/{self.hard.id}/skipped").status_code, 403)   # no token
        self.assertEqual(requests.post(f"{base}/api/mark/{self.hard.id}/skipped",
                                       headers={"X-JH-Token": self.token, "Host": "evil.com"}).status_code, 403)
        self.assertEqual(requests.get(base + "/file", params={"p": "/etc/passwd"}).status_code, 404)

    def test_approve_submits(self):
        import requests
        base = f"http://127.0.0.1:{self.port}"
        r = requests.post(f"{base}/api/approve/{self.basic.id}", headers={"X-JH-Token": self.token}).json()
        self.assertTrue(r["ok"], r)
        for _ in range(90):
            t = requests.get(base + "/api/task").json()
            if not t["running"]:
                break
            time.sleep(1)
        self.assertEqual(DB().get(self.basic.id)["status"], "applied", t.get("tail"))
        self.assertTrue(DB().applications(self.basic.id)[0]["approved"])


class PrivacyGuard(unittest.TestCase):
    def test_no_private_values_in_tracked_files(self):
        priv = ROOT / "config" / "private.yaml"
        if not priv.is_file() or not (ROOT / ".git").is_dir():
            self.skipTest("no private.yaml or not a git checkout")
        vals = set()

        def walk(v):
            if isinstance(v, dict):
                [walk(x) for x in v.values()]
            elif isinstance(v, list):
                [walk(x) for x in v]
            elif v is not None and len(str(v)) >= 6 and not str(v).replace(".", "").isdigit():
                vals.add(str(v))
        d = yaml.safe_load(priv.read_text())
        for k in ("phone", "phone_digits", "email", "alt_email", "father_name", "mother_name", "location"):
            walk(d.get(k))
        vals.add(str(d.get("date_of_birth")))
        files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
                               capture_output=True, text=True).stdout.split()
        leaks = []
        for f in files:
            p = ROOT / f
            if p.suffix in (".png", ".pdf", ".pyc") or not p.is_file():
                continue
            txt = p.read_text(errors="ignore")
            leaks += [f"{f}: {v}" for v in vals if v in txt and v not in ("Bengaluru", "Karnataka", "Bengaluru Urban")]
        self.assertFalse(leaks, leaks)


if __name__ == "__main__":
    unittest.main()
