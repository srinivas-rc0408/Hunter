"""E.4 referrals: `ref <jid> <name>` / `ref done <jid>` in Telegram → Referral columns on Applications + a Referrals
sheet. The name is only ever what you typed (no LinkedIn lookups)."""
import datetime as dt
import unittest
from unittest import mock

from _home import use, make_home

HOME = make_home()


def setUpModule():
    use(HOME)


from openpyxl import load_workbook  # noqa: E402
from jobhunter import tracker  # noqa: E402
from jobhunter.bot import handle  # noqa: E402
from jobhunter.db import DB, Job  # noqa: E402
from test_ops import assert_telegram_html  # noqa: E402


def job(db, company, status=None):
    j = Job("ML Intern", company, "Bengaluru", url="https://x.example/" + company, source="linkedin",
            description="Python", posted_at=str(dt.date.today()))
    db.upsert(j)
    if status:
        db.set_status(j.id, status)
    return j


def sheet(f, name):
    rows = list(load_workbook(f)[name].iter_rows(values_only=True))
    return [dict(zip(rows[0], r)) for r in rows[1:]]


class Referrals(unittest.TestCase):
    def setUp(self):
        self.db = DB(HOME / "data" / f"ref-{self._testMethodName}.db")
        self.f = HOME / "data" / f"ref-{self._testMethodName}.xlsx"
        self.a = job(self.db, "Acme", "applied")
        self.b = job(self.db, "Beta")                       # not applied yet: asking for a referral first is normal
        self.p = mock.patch("jobhunter.tracker.path", return_value=self.f)
        self.p.start()
        self.addCleanup(self.p.stop)

    def say(self, text):
        out = handle(text, self.db, chat="7")
        assert_telegram_html(self, out)
        return out

    def test_ref_then_done_fills_columns_and_referrals_sheet(self):
        out = self.say(f"ref {self.a.id[:8]} Priya   Sharma")
        self.assertIn("Priya Sharma", out)
        self.assertIn("asked", out)
        self.say(f"ref {self.b.id[:8]} Rahul")
        app = {r["Company"]: r for r in sheet(self.f, "Applications")}
        self.assertEqual(app["Acme"]["Referral name"], "Priya Sharma")
        self.assertEqual(app["Acme"]["Referral status"], "asked")
        self.assertEqual(app["Acme"]["Referral date"].date(), dt.date.today())
        self.assertNotIn("Beta", app)                       # Applications = applications only …
        refs = {r["Company"]: r for r in sheet(self.f, "Referrals")}
        self.assertEqual(refs["Beta"]["Referrer"], "Rahul")  # … the Referrals sheet has every one
        self.assertEqual(refs["Beta"]["Job status"], "new")
        out = self.say(f"ref done {self.a.id[:8]}")
        self.assertIn("referred", out)
        self.assertEqual({r["Company"]: r for r in sheet(self.f, "Referrals")}["Acme"]["Status"], "referred")

    def test_slash_and_inline_id_forms(self):
        self.say(f"/ref_{self.a.id[:8]} Asha K R")
        self.say(f"/ref done {self.a.id[:8]}")
        r = tracker.referral(self.db, self.a.id)
        self.assertEqual((r["name"], r["status"]), ("Asha K R", "referred"))

    def test_bare_ref_lists_and_bad_input_is_refused(self):
        self.assertIn("none yet", self.say("ref"))
        self.assertIn("Usage", self.say(f"ref {self.a.id[:8]}"))          # no name: nothing guessed
        self.assertIn("no job", self.say("ref deadbeef Priya"))
        self.assertIn("no referral", self.say(f"ref done {self.b.id[:8]}"))   # done before asking anyone
        self.assertIn("name", self.say(f"ref {self.a.id[:8]} https://linkedin.com/in/x"))
        self.assertIsNone(tracker.referral(self.db, self.a.id))
        self.say(f"ref {self.a.id[:8]} Priya")
        self.assertIn("Priya", self.say("ref"))

    def test_long_name_without_slash_is_still_the_command(self):
        self.say(f"ref {self.a.id[:8]} Asha K R Rao Kumar")
        self.assertEqual(tracker.referral(self.db, self.a.id)["name"], "Asha K R Rao Kumar")

    def test_html_in_a_name_is_escaped(self):
        out = self.say(f"ref {self.a.id[:8]} <b>Bob</b>")
        self.assertIn("&lt;b&gt;Bob", out)

    def test_referral_columns_are_standard_not_custom(self):
        """Read-back must never turn them into user columns (or duplicate them) on the next rewrite."""
        self.say(f"ref {self.a.id[:8]} Priya")
        tracker.write(self.db, self.f)
        self.assertEqual(tracker.columns(self.db), [])
        head = next(load_workbook(self.f)["Applications"].iter_rows(values_only=True))
        self.assertEqual(head.count("Referral name"), 1)


if __name__ == "__main__":
    unittest.main()
