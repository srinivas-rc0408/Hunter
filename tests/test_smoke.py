"""End-to-end smoke: one fake job through the whole private-job path, offline.
job → score → Telegram card → /check → /applied → tracker row → a reply email matched back to it.
Plus the code-hygiene guards from the cleanup: no silent `except Exception: pass`, no file over 600 lines."""
import ast
import datetime as dt
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from _home import make_home, use

HOME = make_home()
ROOT = Path(__file__).resolve().parent.parent


def setUpModule():
    use(HOME)


from openpyxl import load_workbook  # noqa: E402
from jobhunter import mail, tracker  # noqa: E402
from jobhunter.bot import handle  # noqa: E402
from jobhunter.db import DB, Job  # noqa: E402
from jobhunter.notify import job_card  # noqa: E402
from jobhunter.score import score  # noqa: E402
from test_ops import assert_telegram_html  # noqa: E402


def email(sender, subject, body):
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"] = sender, subject, "Mon, 23 Nov 2026 10:15:00 +0530"
    m["Message-ID"] = "<smoke@x>"
    m.set_content(body)
    return mail.parse("1", m.as_bytes())


class Smoke(unittest.TestCase):
    def test_fake_job_end_to_end(self):
        db = DB(HOME / "data" / "smoke.db")
        mail.ensure(db)
        f = HOME / "data" / "smoke.xlsx"
        job = Job("Machine Learning Intern", "Acme AI Labs", "Bengaluru, Karnataka",
                  url="https://boards.greenhouse.io/acmeailabs/jobs/42", source="greenhouse",
                  description="Python, PyTorch and LLMs. Freshers and final-year students welcome. 0-1 years.",
                  posted_at=str(dt.date.today()))

        # score: a fresher ML internship in Bengaluru is a match, with reasons
        db.upsert(score(job))
        j = db.get(job.id)
        self.assertGreater(j["score"], 40, j.get("reasons"))
        self.assertTrue(j.get("reasons"))
        sid = j["id"][:8]

        # card: valid Telegram HTML, the job and its one-tap commands
        card = job_card(j, db)
        assert_telegram_html(self, card)
        for want in ("Machine Learning Intern", "Acme AI Labs", f"/check_{sid}"):
            self.assertIn(want, card)

        with mock.patch("jobhunter.tracker.path", return_value=f):
            # /check: apply from the phone: the posting's link
            out = handle(f"/check_{sid}", db)
            for part in (out if isinstance(out, list) else [out]):
                assert_telegram_html(self, part)
            self.assertIn("greenhouse.io/acmeailabs/jobs/42", "".join(out if isinstance(out, list) else [out]))

            # /applied: status changes and the tracker is rewritten
            out = handle(f"/applied_{sid}", db)
            assert_telegram_html(self, out)
            self.assertIn("applied", out)
        self.assertEqual(db.get(j["id"])["status"], "applied")

        # tracker row
        rows = list(load_workbook(f)["Applications"].iter_rows(values_only=True))
        row = dict(zip(rows[0], next(r for r in rows[1:] if "Acme AI Labs" in r)))
        self.assertEqual((row["Role"], row["Status"], row["Job id"]), ("Machine Learning Intern", "applied", sid))
        self.assertEqual(row["Applied on"].date(), dt.date.today())

        # a reply email finds its application
        m = email("no-reply@greenhouse.io", "Acme AI Labs: your application for Machine Learning Intern",
                  "Thanks for applying. We would like to invite you to an interview.")
        self.assertTrue(mail.is_job_mail(m, ["acme ai labs"]))
        jid, how = mail.match(db, m)
        self.assertEqual(jid, j["id"], how)
        self.assertEqual(mail.classify(m), "interview")


class DryRun(unittest.TestCase):
    def test_dry_scan_counts_per_source_and_saves_nothing(self):
        from jobhunter import notify, pipeline
        db = DB()
        before = db.count("1=1")
        found = [Job("ML Intern", "Acme AI Labs", "Bengaluru", url="https://x.example/1", source="linkedin",
                     description="Python freshers", posted_at=str(dt.date.today())),
                 Job("AI Engineer", "Beta", "Bengaluru", url="https://x.example/2", source="naukri",
                     description="Python LLM freshers", posted_at=str(dt.date.today()))]
        with mock.patch.object(pipeline.boards, "enabled", lambda light=False: ["linkedin"]), \
                mock.patch.object(pipeline.boards, "scan", lambda errors, out, **k: out.extend(found)), \
                mock.patch.object(notify, "alert_jobs", side_effect=AssertionError("a dry run sent an alert")):
            r = pipeline.run(only={"boards"}, dry=True)
        self.assertEqual(r["by_source"], {"linkedin": {"found": 1, "new": 1}, "naukri": {"found": 1, "new": 1}})
        self.assertEqual((r["found"], r["new"], r["alerted"]), (2, 2, 0))
        self.assertEqual(DB().count("1=1"), before)                        # the real DB never saw them


class SourceCrash(unittest.TestCase):
    """26 Nov 2026 live scan: one Ashby board timed out and the whole scan died. Never again."""

    def test_a_crashing_source_is_an_error_line_not_the_end(self):
        from jobhunter import pipeline
        ok = [Job("ML Intern", "Acme AI Labs", "Bengaluru", url="https://x.example/ok", source="linkedin",
                  description="Python freshers", posted_at=str(dt.date.today()))]
        with mock.patch.object(pipeline.boards, "enabled", lambda light=False: ["linkedin"]), \
                mock.patch.object(pipeline.boards, "scan", lambda errors, out, **k: out.extend(ok)), \
                mock.patch.object(pipeline.ats, "scan", side_effect=TimeoutError("The read operation timed out")):
            r = pipeline.run(only={"boards", "ats"}, dry=True)
        self.assertEqual(r["found"], 1)
        self.assertTrue(any("company boards crashed: TimeoutError" in e for e in r["errors"]), r["errors"])

    def test_one_bad_company_board_keeps_the_others(self):
        from jobhunter.sources import ats

        class Scout:
            class Fetcher:
                def __init__(self, **k):
                    pass

            @staticmethod
            def fetch_board(fetcher, board, today, roles, details=12, notes=None):
                if board["token"] == "slowco":
                    raise TimeoutError("The read operation timed out")
                return [{"title": "ML Intern", "company": "Goodco", "location": "Bengaluru, India", "url": "https://x/1"}]
        boards = [{"type": "ashby", "company": "Slowco", "token": "slowco"},
                  {"type": "greenhouse", "company": "Goodco", "token": "goodco"}]
        errors = []
        with mock.patch.object(ats, "scout", lambda: Scout), \
                mock.patch.object(ats.config, "cfg", lambda n: {"boards": boards} if n == "companies" else {}), \
                mock.patch("jobhunter.companies.watchlist", lambda: []):
            jobs = ats.scan(errors)
        self.assertEqual([j.company for j in jobs], ["Goodco"])
        self.assertTrue(any("slowco" in e and "TimeoutError" in e for e in errors), errors)

    def test_a_slow_source_is_cut_at_its_limit_and_keeps_what_it_found(self):
        import threading
        from jobhunter import pipeline
        hang = threading.Event()
        errors = []

        def slow(out):
            out.append("job found before the limit")
            hang.wait(5)
            out.append("too late")
        try:
            got = pipeline._source("company boards", errors, slow, limit=0.2)
        finally:
            hang.set()
        self.assertEqual(got, ["job found before the limit"])
        self.assertTrue(any("company boards: stopped at the 0.2 s limit (kept 1 jobs)" in e for e in errors), errors)

    def test_govt_and_company_boards_run_before_linkedin(self):
        from jobhunter import pipeline
        order = []
        with mock.patch.object(pipeline.govt, "scan", lambda *a, **k: order.append("govt") or []), \
                mock.patch.object(pipeline.ats, "scan", lambda *a, **k: order.append("ats")), \
                mock.patch.object(pipeline.boards, "enabled", lambda light=False: ["indeed", "linkedin"]), \
                mock.patch.object(pipeline.boards, "scan", lambda errors, sites, **k: order.append(sites[0])):
            pipeline.run(only={"boards", "ats", "govt"}, dry=True)
        self.assertEqual(order, ["govt", "ats", "indeed", "linkedin"])


class Hygiene(unittest.TestCase):
    """The cleanup's rules, kept: every swallowed error is logged, and no module grows past 600 lines."""

    def files(self):
        return sorted((ROOT / "jobhunter").rglob("*.py"))

    def test_no_silent_broad_except(self):
        silent = []
        for f in self.files():
            for n in ast.walk(ast.parse(f.read_text())):
                if not isinstance(n, ast.ExceptHandler):
                    continue
                names = n.type.elts if isinstance(n.type, ast.Tuple) else [n.type]
                broad = n.type is None or any(isinstance(x, ast.Name) and x.id in ("Exception", "BaseException") for x in names)
                quiet = all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                            for s in n.body)
                if broad and quiet:
                    silent.append(f"{f.relative_to(ROOT)}:{n.lineno}")
        self.assertEqual(silent, [], "log it (util.log) or handle it")

    def test_no_module_over_600_lines(self):
        big = {str(f.relative_to(ROOT)): n for f in self.files() if (n := len(f.read_text().splitlines())) > 600}
        self.assertEqual(big, {})


if __name__ == "__main__":
    unittest.main()
