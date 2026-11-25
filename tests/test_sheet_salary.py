"""Part 2: your sheet edits survive every rewrite (custom columns, hidden rows, edits typed into the .xlsx).
Part 3: salary estimates: sources, confidence, never in pay_min/pay_max, cached 30 days, stipend per month."""
import datetime as dt
import time
import unittest
from unittest import mock

from _home import use, make_home

HOME = make_home()


def setUpModule():
    use(HOME)


from openpyxl import load_workbook  # noqa: E402
from jobhunter import salary, tracker  # noqa: E402
from jobhunter.bot import handle  # noqa: E402
from jobhunter.db import DB, Job  # noqa: E402
from jobhunter.notify import job_card  # noqa: E402
from test_ops import assert_telegram_html  # noqa: E402


def applied(db, title, company):
    j = Job(title, company, "Bengaluru, Karnataka", url="https://x.example/" + company, source="linkedin",
            description="Python", posted_at=str(dt.date.today()))
    db.upsert(j)
    db.set_status(j.id, "applied")
    return j


def table(f):
    ws = load_workbook(f)["Applications"]
    rows = list(ws.iter_rows(values_only=True))
    head = list(rows[0])
    return head, [dict(zip(head, r)) for r in rows[1:]], ws


class Sheet(unittest.TestCase):
    def setUp(self):
        self.db = DB(HOME / "data" / f"sheet-{self._testMethodName}.db")
        self.f = HOME / "data" / f"{self._testMethodName}.xlsx"
        self.a = applied(self.db, "ML Intern", "Acme")
        self.b = applied(self.db, "AI Engineer", "Beta")

    def say(self, text):
        out = handle(text, self.db, chat="7")
        for p in (out if isinstance(out, list) else [out]):
            assert_telegram_html(self, p)
        return out if isinstance(out, str) else "\n".join(out)

    def test_custom_column_value_and_hidden_row_survive_rewrites(self):
        tracker.add_column(self.db, "Referral")
        tracker.set_value(self.db, self.a.id, "referral", "asked a friend")
        tracker.hide(self.db, self.b.id)
        for _ in range(2):                                         # rewritten again: still there
            head, rows, _ = table(tracker.write(self.db, self.f))
            self.assertEqual(head[-2:], ["Notes", "Referral"])     # custom columns after the standard ones
            self.assertEqual([r["Company"] for r in rows], ["Acme"])
            self.assertEqual(rows[0]["Referral"], "asked a friend")
        self.assertEqual(self.db.get(self.b.id)["status"], "applied")     # hidden ≠ deleted
        self.assertEqual(len(self.db.events(self.b.id)), 1)
        self.assertEqual(tracker.show_hidden(self.db), 1)
        _, rows, _ = table(tracker.write(self.db, self.f))
        self.assertEqual(sorted(r["Company"] for r in rows), ["Acme", "Beta"])

    def test_edits_typed_into_the_file_keep_only_notes_and_custom_columns(self):
        tracker.add_column(self.db, "Referral")
        tracker.write(self.db, self.f)
        head, _, ws = table(self.f)
        col = {h: i + 1 for i, h in enumerate(head)}
        row = next(r for r in range(2, ws.max_row + 1) if ws.cell(r, col["Company"]).value == "Acme")
        ws.cell(row, col["Notes"]).value = "called HR on Monday"
        ws.cell(row, col["Referral"]).value = "Priya, team lead"
        ws.cell(row, col["Status"]).value = "offer"                 # not yours to change from the file
        ws.cell(row, col["Company"]).value = "Renamed Co"
        ws.cell(1, len(head) + 1).value = "Follow-up"               # a column you added in the file
        ws.cell(row, len(head) + 1).value = "Friday"
        ws.parent.save(self.f)
        _, rows, _ = table(tracker.write(self.db, self.f))
        acme = next(r for r in rows if r["Job id"] == self.a.id[:8])
        self.assertEqual(acme["Notes"], "called HR on Monday")
        self.assertEqual(acme["Referral"], "Priya, team lead")
        self.assertEqual(acme["Follow-up"], "Friday")
        self.assertEqual(acme["Status"], "applied")                # from the DB, not the file
        self.assertEqual(acme["Company"], "Acme")
        self.assertEqual(self.db.get(self.a.id)["notes"], "called HR on Monday")

    def test_a_chat_change_is_not_undone_by_the_old_file(self):
        tracker.add_column(self.db, "Referral")
        tracker.write(self.db, self.f)                             # file says Referral = ""
        tracker.set_value(self.db, self.a.id, "Referral", "from chat")
        _, rows, _ = table(tracker.write(self.db, self.f))
        self.assertEqual(next(r for r in rows if r["Company"] == "Acme")["Referral"], "from chat")

    def test_the_three_chat_examples_need_a_yes(self):
        jid = self.a.id[:8]
        with mock.patch("jobhunter.tracker.path", return_value=self.f):
            out = self.say("add a column Referral in the sheet")
            self.assertIn("I'll add a column <b>Referral</b>", out)
            self.assertEqual(tracker.columns(self.db), [])
            self.say("yes")
            self.assertEqual(tracker.columns(self.db), ["Referral"])
            out = self.say(f"put 'asked Rahul for referral' in Referral for {jid}")
            self.assertIn("Reply <b>yes</b>", out)
            self.say("yes")
            out = self.say(f"remove {jid} from the sheet")
            self.assertIn("the job and your application record stay", out)
            self.say("yes")
            _, rows, _ = table(self.f)
            self.assertNotIn("Acme", [r["Company"] for r in rows])
            self.say("show hidden")
            self.say("yes")
            _, rows, _ = table(self.f)
        acme = next(r for r in rows if r["Company"] == "Acme")
        self.assertEqual(acme["Referral"], "asked Rahul for referral")

    def test_put_x_in_a_column_is_sheet_set_and_note_is_add_note(self):
        tracker.add_column(self.db, "Referral")
        jid = self.a.id[:8]
        no_model = mock.patch("jobhunter.llm.chat", side_effect=AssertionError("must not need the model"))
        with mock.patch("jobhunter.tracker.path", return_value=self.f), no_model:
            for msg in (f"put 'asked Rahul for referral' in Referral for {jid}",      # quoted
                        f"put asked Rahul for referral in the Referral column for {jid}"):   # unquoted, "for" inside
                out = self.say(msg)
                self.assertIn("put “asked Rahul for referral” in <b>Referral</b>", out)
                self.say("yes")
                self.assertEqual(self.db.c.execute("SELECT value FROM sheet_custom WHERE job_id=? AND column='Referral'",
                                                   (self.a.id,)).fetchone()[0], "asked Rahul for referral")
                tracker.set_value(self.db, self.a.id, "Referral", "")
            self.assertFalse(self.db.get(self.a.id)["notes"])                        # nothing leaked into Notes
            for msg in (f"add a note 'call HR Monday' to {jid}", f"note for {jid}: call HR Monday"):
                self.assertIn("add the note “call HR Monday”", self.say(msg))
                self.say("yes")
            self.assertIn("call HR Monday", self.db.get(self.a.id)["notes"])
            self.assertIsNone(self.db.c.execute("SELECT 1 FROM sheet_custom WHERE job_id=?", (self.a.id,)).fetchone())
            self.assertIn("filled from the job record", self.say(f"put interview in Status for {jid}"))

    def test_unknown_column_and_job_are_explained(self):
        self.assertIn("no column “Referral”", self.say(f"put 'x' in Referral for {self.a.id[:8]}"))
        tracker.add_column(self.db, "Referral")
        self.assertIn("No job matches", self.say("put 'x' in Referral for ab12cd34"))


def src(kind, lo, hi, n=50, fresher=True):
    return {"name": f"AmbitionBox · {kind}", "url": f"https://www.ambitionbox.com/{kind}", "min": lo, "max": hi,
            "n": n, "fresher": fresher, "kind": kind}


class Salary(unittest.TestCase):
    def setUp(self):
        self.db = DB(HOME / "data" / f"sal-{self._testMethodName}.db")

    def est(self, comp=None, role=None, web=(), **kw):
        return salary.estimate(kw.get("r", "AI engineer"), kw.get("c", "Zoho"), "Bengaluru", "fresher", self.db,
                               lookup=(lambda *a: comp, lambda *a: role, lambda *a: list(web)))

    def test_agreeing_sources_high_confidence(self):
        r = self.est(src("company", 760000, 920000, 45), src("role", 760000, 840000, 9000))
        self.assertEqual(r["confidence"], "high")
        html = salary.as_html(r)
        self.assertIn("≈ <b>₹7.6–9.2 LPA</b> (estimate, not from the posting)", html)
        self.assertIn("confidence: <b>high</b>", html)
        self.assertIn('href="https://www.ambitionbox.com/company"', html)
        self.assertIn("checked", html)
        assert_telegram_html(self, html)

    def test_disagreement_and_missing_data_are_said(self):
        r = self.est(src("company", 500000, 600000, 30), src("role", 1100000, 1300000, 900), c="Infy")
        self.assertEqual(r["confidence"], "low")
        self.assertIn("sources disagree a lot", r["note"])
        r = self.est(None, src("role", 900000, 1000000, 900, fresher=False), c="Tiny Co")
        self.assertEqual(r["confidence"], "low")
        self.assertIn("no Tiny Co-specific data", r["note"])
        self.assertIn("no fresher (0–1 yr) data", r["note"])
        r = self.est(None, None, c="Nobody")
        self.assertNotIn("min", r)
        self.assertIn("I won't guess", salary.as_html(r))

    def test_internship_is_a_monthly_stipend(self):
        r = self.est(None, src("role", 267000, 305000, 700), r="ML intern", c="")
        self.assertIn("₹22–25k/month", salary.as_html(r))

    def test_cached_30_days(self):
        calls = []
        look = (lambda *a: calls.append(1) or src("company", 7e5, 8e5), lambda *a: None, lambda *a: [])
        salary.estimate("SWE", "Zoho", db=self.db, lookup=look)
        self.assertTrue(salary.estimate("SWE", "Zoho", db=self.db, lookup=look).get("cached"))
        self.assertEqual(len(calls), 1)
        self.db.c.execute("UPDATE salary_cache SET at=?", (time.time() - 31 * 86400,))
        salary.estimate("SWE", "Zoho", db=self.db, lookup=look)
        self.assertEqual(len(calls), 2)

    def test_stored_apart_from_pay_and_shown_on_cards(self):
        j = Job("AI Engineer", "Zoho", "Chennai", url="https://x.example/z", source="linkedin")
        self.db.upsert(j)
        r = self.est(src("company", 760000, 920000, 45), src("role", 760000, 840000, 900))
        salary.store(self.db, j.id, r)
        row = self.db.get(j.id)
        self.assertIsNone(row["pay_min"])
        self.assertIsNone(row["pay_max"])
        self.assertEqual((row["est_min"], row["est_max"]), (760000, 920000))
        self.assertIn("💰 not listed · ≈ ₹7.6–9.2 LPA est.", job_card(row))

    def test_chat_question_uses_the_estimate_and_never_the_model(self):
        with mock.patch.object(salary, "company_source", return_value=src("company", 760000, 920000, 45)), \
             mock.patch.object(salary, "role_source", return_value=src("role", 760000, 840000, 900)), \
             mock.patch("jobhunter.llm.chat") as model:
            sent = []
            out = handle("how much would a fresher AI engineer get at Zoho?", self.db, chat="9", progress=sent.append)
            model.assert_not_called()
        out = out if isinstance(out, str) else "\n".join(out)
        self.assertIn("₹7.6–9.2 LPA", out)
        self.assertIn("estimate, not from the posting", out)
        self.assertEqual(sent, ["🔎 searching…"])


if __name__ == "__main__":
    unittest.main()
