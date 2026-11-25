"""Application emails: sorted right, matched to the right job, statuses only move forward; Excel tracker."""
import unittest
from email.message import EmailMessage

from _home import make_home, use

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter import mail, tracker  # noqa: E402
from jobhunter.db import DB, Job  # noqa: E402


def raw(sender, subject, body, date="Mon, 23 Nov 2026 10:15:00 +0530", mid=None):
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"] = sender, subject, date
    m["Message-ID"] = mid or f"<{abs(hash(sender + subject))}@x>"
    m.set_content(body)
    return m.as_bytes()


class FakeIMAP:
    def __init__(self, msgs):
        self.msgs, self.labels = msgs, {}

    def select(self, *a, **k):
        return "OK", [b""]

    def create(self, *a):
        return "OK", [b""]

    def uid(self, cmd, *a):
        if cmd == "SEARCH":
            return "OK", [" ".join(str(i) for i in range(1, len(self.msgs) + 1)).encode()]
        if cmd == "FETCH":
            ids = [int(x) for x in a[0].split(",")]
            self.fetched = getattr(self, "fetched", []) + [(a[1], ids)]
            if "HEADER.FIELDS" in a[1]:
                out = []
                for i in ids:
                    head = self.msgs[i - 1].split(b"\n\n", 1)[0] + b"\n\n"
                    out += [(f"{i} (UID {i} BODY[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] {{{len(head)}}}".encode(), head), b")"]
                return "OK", out
            return "OK", [(f"{ids[0]} (UID {ids[0]} BODY[] {{1}}".encode(), self.msgs[ids[0] - 1]), b")"]
        if cmd == "STORE":
            self.labels[a[0]] = a[2]
            return "OK", [b""]

    def logout(self):
        pass


class Mail(unittest.TestCase):
    def setUp(self):
        self.db = DB()
        mail.ensure(self.db)
        self.db.c.execute("DELETE FROM mail")
        self.db.c.execute("DELETE FROM mail_state")
        self.db.c.execute("DELETE FROM jobs")
        self.db.c.execute("DELETE FROM events")
        tracker.ensure(self.db)
        for t in ("applications", "sheet_columns", "sheet_custom", "sheet_hidden", "sheet_snapshot"):
            self.db.c.execute(f"DELETE FROM {t}")             # other test modules share this DB
        self.jobs = {}
        for t, c in [("Generative AI Engineer", "Birlasoft Limited"), ("ML Intern", "Zoho Corporation"),
                     ("Data Analyst", "Zoho Corporation"), ("AI Engineer", "Astra Security")]:
            j = Job(t, c, "Bengaluru, Karnataka", url=f"https://x.example/{t}{c}", source="linkedin")
            self.db.upsert(j)
            self.db.set_status(j.id, "applied", "test")
            self.jobs[(t, c)] = j.id
        self.db.c.commit()

    def m(self, sender, subject, body):
        return mail.parse("1", raw(sender, subject, body))

    def test_sorting(self):
        cases = [
            ("Birlasoft Careers <careers@birlasoft.com>", "Thank you for applying",
             "We have received your application. If you are shortlisted, we will contact you for an interview.", "received"),
            ("talent@zoho.com", "Interview invitation - ML Intern", "Please pick a slot for your technical round.", "interview"),
            ("no-reply@hackerrank.com", "Zoho Corporation has invited you to an assessment", "Complete the coding test.", "assessment"),
            ("careers@birlasoft.com", "Update on your application",
             "Unfortunately, we have decided to move forward with other candidates.", "rejected"),
            ("hr@astra.security", "Offer letter - AI Engineer", "We are pleased to offer you the role.", "offer"),
            ("jobs-noreply@linkedin.com", "Your application was viewed by Astra Security", "", "viewed"),
        ]
        for s, sub, body, want in cases:
            self.assertEqual(mail.classify(self.m(s, sub, body)), want, sub)

    def test_job_alerts_are_not_replies(self):
        self.assertFalse(mail.is_job_mail(self.m("jobalerts-noreply@linkedin.com", "30+ new jobs for you in Bengaluru", "x"), []))
        self.assertFalse(mail.is_job_mail(self.m("friend@gmail.com", "Dinner tonight?", "see you"), ["zoho"]))
        self.assertTrue(mail.is_job_mail(self.m("careers@birlasoft.com", "Your application", "thanks"), ["birlasoft"]))

    def test_real_inbox_noise_is_ignored(self):
        """Subjects seen in the real inbox on 24 Nov: none of these is a reply to an application."""
        noise = [("invitations@linkedin.com", "You have 1 new invitation"),
                 ("notifications-noreply@linkedin.com", "Nikesh Rai reacted to this post: 50,000…"),
                 ("news@anthropic.com", "Claude cracked a 26-year-old NSA mystery"),
                 ("info@naukri.com", "Update: Your profile is a good fit"),
                 ("team@render.com", "Build, deploy, and monitor your application in one place"),
                 ("jobalerts-noreply@linkedin.com", "Data Engineer - ML at Scoutit"),
                 ("messages-noreply@linkedin.com", "You may know Abhishek Bandu Bhandare"),
                 ("jobs-listings@linkedin.com", "Machine Learning Engineer: Accenture in India and Infosys are hiring"),
                 ("info@naukri.com", "Update: A new opportunity is available for you | 1 new alert"),
                 ("newsletter@datacamp.com", "Stop applying blindly: Fix your data applications tonight")]
        for s, sub in noise:
            self.assertFalse(mail.is_job_mail(self.m(s, sub, "interview tips and more"), ["zoho", "birlasoft"]), sub)

    def test_coding_platform_promos_ignored(self):
        for s, sub in [("noreply@hackerearth.com", "Solve your first problem"),
                       ("no-reply@hackerrank.com", "Participate in your first contest")]:
            self.assertFalse(mail.is_job_mail(self.m(s, sub, "Sharpen your coding skills"), ["zoho"]), sub)
        self.assertTrue(mail.is_job_mail(self.m("no-reply@hackerrank.com", "Zoho Corporation has invited you to an assessment", ""), []))
        self.assertTrue(mail.is_job_mail(self.m("support@mettl.com", "Your assessment for Data Analyst", ""), []))
        self.assertTrue(mail.is_job_mail(self.m("noreply@codility.com", "Coding test", "Birlasoft: complete the test"), ["birlasoft"]))

    def test_real_replies_are_kept_and_sorted(self):
        m = self.m("info@naukri.com", "Update: Your application has not been accepted", "Regards, recruiter")
        self.assertTrue(mail.is_job_mail(m, []))
        self.assertEqual(mail.classify(m), "rejected")
        m = self.m("jobs-noreply@linkedin.com", "Your application was viewed by Astra Security", "")
        self.assertTrue(mail.is_job_mail(m, []))
        self.assertEqual(mail.classify(m), "viewed")
        m = self.m("jobs-noreply@linkedin.com", "Your application was sent to Zoho Corporation", "")
        self.assertTrue(mail.is_job_mail(m, []))

    def test_applications_you_made_yourself_are_recorded(self):
        msgs = [raw("jobs-noreply@linkedin.com", "SrinivaS, your application was sent to Codemonk", "AI Engineer", mid="<o1@x>"),
                raw("jobs-noreply@linkedin.com", "Your application to ML Engineer at Fractal Analytics", "", mid="<o2@x>"),
                raw("jobs-noreply@linkedin.com", "Complete your application and receive these benefits",
                    "We will invite you to an interview", mid="<o3@x>")]
        r = mail.sync(imap_factory=lambda: FakeIMAP(msgs), quiet=True)
        self.assertEqual(r, {"received": 2})                                # the ad is ignored, not an "interview"
        rows = {(x["company"], x["title"]): x["status"] for x in self.db.query(status=["applied"], limit=100)}
        self.assertEqual(rows[("Fractal Analytics", "ML Engineer")], "applied")
        self.assertIn(("Codemonk", "(role: see the LinkedIn email)"), rows)

    def test_bad_password_is_one_plain_line(self):
        import imaplib
        class Bad:
            def __init__(self, *a): pass
            def login(self, u, p): raise imaplib.IMAP4.error("[ALERT] Application-specific password required")
        import os
        os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"] = "a@b.c", "x"
        real = mail.imaplib.IMAP4_SSL
        mail.imaplib.IMAP4_SSL = Bad
        try:
            r = mail.sync(quiet=True)
        finally:
            mail.imaplib.IMAP4_SSL = real
            os.environ.pop("GMAIL_USER"); os.environ.pop("GMAIL_APP_PASSWORD")
        self.assertIn("16-letter app password", r["error"])

    def test_match_by_sender_subject_and_role(self):
        jid, how = mail.match(self.db, self.m("careers@birlasoft.com", "Thanks", "Hi Srinivas"))
        self.assertEqual(jid, self.jobs[("Generative AI Engineer", "Birlasoft Limited")])
        self.assertIn("sender", how)
        jid, _ = mail.match(self.db, self.m("no-reply@greenhouse.io", "Zoho: your application for Data Analyst", ""))
        self.assertEqual(jid, self.jobs[("Data Analyst", "Zoho Corporation")])
        jid, why = mail.match(self.db, self.m("hr@unknownco.com", "Your application", "Thanks for applying"))
        self.assertIsNone(jid)

    def test_sync_updates_status_forward_only_labels_and_sheet(self):
        msgs = [raw("talent@zoho.com", "Interview invitation - ML Intern", "Please pick a slot.", mid="<a@x>"),
                raw("careers@birlasoft.com", "Update on your application",
                    "Unfortunately we will not be moving forward.", mid="<b@x>"),
                raw("hr@astra.security", "Offer letter - AI Engineer", "We are pleased to offer you the role.", mid="<c@x>"),
                raw("hr@astra.security", "Re: your application", "Unfortunately the role is closed.",
                    date="Mon, 23 Nov 2026 09:00:00 +0530", mid="<d@x>"),
                raw("jobalerts-noreply@linkedin.com", "25 new jobs for you", "x", mid="<e@x>")]
        fake = FakeIMAP(msgs)
        sent = []
        import jobhunter.notify as N
        real, N.telegram = N.telegram, lambda t: sent.append(t) or True
        try:
            r = mail.sync(imap_factory=lambda: fake, quiet=True)
        finally:
            N.telegram = real
        self.assertEqual(r, {"interview": 1, "rejected": 2, "offer": 1})
        st = lambda k: self.db.get(self.jobs[k])["status"]
        self.assertEqual(st(("ML Intern", "Zoho Corporation")), "interview")
        self.assertEqual(st(("Generative AI Engineer", "Birlasoft Limited")), "rejected")
        self.assertEqual(st(("AI Engineer", "Astra Security")), "offer")     # a later 'rejected' mail never undoes an offer
        self.assertIn('"JobHunter/Interview"', fake.labels["1"])
        self.assertEqual(len(sent), 2)                                      # interview + offer pinged; rejections are not
        bodies = [ids for what, ids in fake.fetched if "HEADER" not in what]
        self.assertNotIn([5], bodies)                                       # a job alert's body is never downloaded
        fake.fetched = []
        again = mail.sync(imap_factory=lambda: fake, quiet=True)
        self.assertEqual(again, {})                                         # every message handled once
        self.assertEqual(fake.fetched, [])                                  # and the next run reads nothing old
        # the Excel tracker
        from openpyxl import load_workbook
        p = tracker.write(self.db, HOME / "data" / "t.xlsx")
        wb = load_workbook(p)
        self.assertEqual(wb.sheetnames, ["Dashboard", "Applications", "Email replies"])
        rows = list(wb["Applications"].iter_rows(values_only=True))
        head = list(rows[0])
        self.assertEqual(rows[0][:5], ("Applied on", "Time (IST)", "★", "Company", "Role"))
        self.assertEqual(len(rows) - 1, 4)
        zoho = dict(zip(head, next(r for r in rows if r[4] == "ML Intern")))
        self.assertEqual((zoho["Company"], zoho["Status"], zoho["Emails"]), ("Zoho Corporation", "interview", 1))
        self.assertEqual(len(list(wb["Email replies"].iter_rows())) - 1, 4)
        n = tracker.numbers(self.db)
        self.assertEqual((n["Applied"], n["companies"], n["Interviews"], n["Offers"], n["Rejected"]), (4, 3, 1, 1, 1))

    def test_confirmation_for_a_matched_job_marks_it_applied_on_the_email_date(self):
        """Birlasoft: the form waited for you (needs_you), you sent it, LinkedIn confirmed. That is an application."""
        jid = self.jobs[("Generative AI Engineer", "Birlasoft Limited")]
        self.db.c.execute("UPDATE jobs SET status='needs_you', status_at='2026-11-18T08:00:00+05:30' WHERE id=?", (jid,))
        self.db.c.execute("DELETE FROM events WHERE job_id=?", (jid,))
        self.db.c.commit()
        before = tracker.numbers(self.db)["Applied"]
        msgs = [raw("jobs-noreply@linkedin.com", "SrinivaS, your application was sent to Birlasoft",
                    "Generative AI Engineer · Birlasoft\nYour application number is BS-204511.",
                    date="Thu, 19 Nov 2026 06:29:45 +0530", mid="<bs1@x>"),
                raw("jobs-noreply@linkedin.com", "Your application to Generative AI Engineer at Birlasoft", "",
                    date="Sun, 22 Nov 2026 06:30:21 +0530", mid="<bs2@x>")]
        r = mail.sync(imap_factory=lambda: FakeIMAP(msgs), quiet=True)
        self.assertEqual(r, {"received": 2})
        j = self.db.get(jid)
        self.assertEqual(j["status"], "applied")
        self.assertEqual(self.db.applied_on(jid)[:10], "2026-11-19")          # the email's date, not today
        self.assertEqual(tracker.numbers(self.db)["Applied"], before + 1)
        self.assertEqual(self.db.app_meta(jid)["conf_id"], "BS-204511")      # read from the confirmation email
        self.assertEqual(len(self.db.c.execute("SELECT * FROM events WHERE job_id=? AND kind='applied'", (jid,)).fetchall()), 1)
        # further on already: a late confirmation never moves it back
        self.db.set_status(jid, "interview", "test")
        self.db.c.execute("DELETE FROM mail"); self.db.c.execute("DELETE FROM mail_state"); self.db.c.commit()
        mail.sync(imap_factory=lambda: FakeIMAP(msgs[:1]), quiet=True)
        self.assertEqual(self.db.get(jid)["status"], "interview")

    def test_response_rate_counts_only_real_answers(self):
        ids = list(self.jobs.values())                                     # 4 applications
        def got(jid, kind, n):
            self.db.c.execute("INSERT INTO mail VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (f"<{kind}{n}@x>", "1", "2026-11-20T10:00:00+05:30", "x@y.z", "", kind, "", kind, jid, "", ""))
            self.db.c.commit()
        for i, jid in enumerate(ids):
            got(jid, "received", i)                                        # automatic confirmations: not answers
        n = tracker.numbers(self.db)
        self.assertEqual((n["Response rate"], n["Interview rate"], n["Waiting"]), ("0%", "0%", 4))
        got(ids[0], "viewed", 9)
        got(ids[1], "assessment", 9)
        self.db.set_status(ids[2], "interview", "test")
        n = tracker.numbers(self.db)
        self.assertEqual((n["answered"], n["Response rate"]), (3, "75%"))
        self.assertEqual(n["Interview rate"], "25%")                       # (interview + offer) / applied
        self.db.set_status(ids[3], "offer", "test")
        self.assertEqual(tracker.numbers(self.db)["Interview rate"], "50%")
        from jobhunter.bot import _stats
        self.assertIn("100% (4 of 4 got a real answer", _stats(self.db))

    def test_confirmation_numbers_are_read_never_made_up(self):
        from jobhunter.util import confirmation_id as cid
        self.assertEqual(cid("Thank you! Application ID: 12345"), "12345")
        self.assertEqual(cid("Your Reference no. REF-88A2 is below"), "REF-88A2")
        self.assertEqual(cid("Your application number is APP-2026-0412."), "APP-2026-0412")
        self.assertEqual(cid("Candidate ID #C99812"), "C99812")
        self.assertEqual(cid("Requisition ID: R-203"), "R-203")
        self.assertEqual(cid("Thanks for applying. We received your application."), "")
        self.assertEqual(cid("Your application is under review"), "")       # a word, not a number

    def test_the_excel_workbook_is_a_real_report(self):
        from openpyxl import load_workbook
        from openpyxl.chart import BarChart, DoughnutChart
        import datetime as _dt
        ids = list(self.jobs.values())
        tracker.add_column(self.db, "Referral")
        tracker.set_value(self.db, ids[0], "Referral", "asked a friend")
        self.db.add_application(ids[0], "greenhouse", "applied", False, True, False, "you submitted it", [], "", "",
                                None, "", "https://x.example/thanks", "APP-77123", "https://boards.greenhouse.io/x/apply")
        old = "2026-10-01T10:00:00+05:30"                                   # 50+ days, no answer: red
        self.db.c.execute("UPDATE events SET at=? WHERE job_id=? AND kind='applied'", (old, ids[1]))
        self.db.c.commit()
        f = HOME / "data" / "report" / "Applications.xlsx"
        tracker.write(self.db, f)
        tracker.write(self.db, f)                                          # rewritten in place
        self.assertEqual(sorted(x.name for x in f.parent.iterdir()), ["Applications.xlsx"])   # never a 2nd copy
        wb = load_workbook(f)
        self.assertEqual(wb.sheetnames, ["Dashboard", "Applications", "Email replies"])
        ds, ws, er = wb["Dashboard"], wb["Applications"], wb["Email replies"]
        # Dashboard: banner, tiles, 2 native charts, next actions
        self.assertTrue(ds["B1"].value.endswith("· Job Applications"))
        self.assertTrue(ds["B2"].value.startswith("Updated "))
        tiles = {ds.cell(4, c).value: ds.cell(5, c).value for c in range(2, 18, 2)}
        self.assertEqual(list(tiles), ["Applied", "This week", "Waiting", "Interviews", "Offers", "Rejected",
                                       "Response rate", "Interview rate"])
        self.assertEqual(tiles["Applied"], 4)
        self.assertIn("B5:C6", {str(m) for m in ds.merged_cells.ranges})
        self.assertEqual(sorted(type(c).__name__ for c in ds._charts), ["BarChart", "DoughnutChart"])
        self.assertIn("Next actions", [c.value for c in ds["B"] if c.value])
        self.assertTrue(any("Follow up" in str(c.value) for c in ds["D"]))
        # Applications: an Excel Table, columns in order, dates as dates, links, colours
        head = [c.value for c in ws[1]]
        self.assertEqual(head, tracker.H + ["Referral"] + tracker.TAIL)
        self.assertEqual(head[:13], ["Applied on", "Time (IST)", "★", "Company", "Role", "Location", "Work mode", "Pay",
                                     "Score", "Applied via", "Status", "Status since", "Application ID"])
        t = ws.tables["Applications"]
        self.assertEqual(t.ref, f"A1:{ws.cell(1, len(head)).column_letter}5")
        self.assertTrue(t.tableStyleInfo.showRowStripes)
        self.assertEqual(ws.freeze_panes, "A2")
        col = {h: i for i, h in enumerate(head, 1)}
        r = next(i for i in range(2, 6) if ws.cell(i, col["Job id"]).value == ids[0][:8])
        self.assertIsInstance(ws.cell(r, col["Applied on"]).value, _dt.datetime)
        self.assertIsInstance(ws.cell(r, col["Time (IST)"]).value, _dt.time)
        self.assertEqual(ws.cell(r, col["Application ID"]).value, "APP-77123")
        self.assertEqual(ws.cell(r, col["Referral"]).value, "asked a friend")
        self.assertEqual((ws.cell(r, col["Job link"]).value, ws.cell(r, col["Application form"]).value), ("Open job", "Open form"))
        self.assertEqual(ws.cell(r, col["Application form"]).hyperlink.target, "https://boards.greenhouse.io/x/apply")
        self.assertTrue(ws.cell(r, col["Job link"]).hyperlink.target.startswith("https://x.example/"))
        late = next(i for i in range(2, 6) if ws.cell(i, col["Job id"]).value == ids[1][:8])
        self.assertGreater(ws.cell(late, col["Days waiting"]).value, 14)
        cf = {str(rng.sqref): [x.formula[0] for x in rules] for rng, rules in ws.conditional_formatting._cf_rules.items()}
        st, dw = ws.cell(1, col["Status"]).column_letter, ws.cell(1, col["Days waiting"]).column_letter
        self.assertIn(f'${st}2="interview"', cf[f"{st}2:{st}5"])
        self.assertIn(f"AND(ISNUMBER(${dw}2),${dw}2>14)", cf[f"{dw}2:{dw}5"])
        # print: landscape, one page wide, header repeated
        for sh in (ws, er):
            self.assertEqual(sh.page_setup.orientation, "landscape")
            self.assertEqual((sh.page_setup.fitToWidth, sh.sheet_properties.pageSetUpPr.fitToPage), (1, True))
        self.assertEqual(ws.print_title_rows, "$1:$1")
        # your edits in the file still come back (Notes, your columns), now that the table moved them
        ws.cell(r, col["Notes"]).value = "typed in Excel"
        wb.save(f)
        tracker.write(self.db, f)
        self.assertEqual(self.db.get(ids[0])["notes"], "typed in Excel")
        # the same ID and form link in Telegram /applied and /form_<id>
        from jobhunter.bot import handle
        from test_ops import assert_telegram_html
        out = handle("/applied", self.db)
        assert_telegram_html(self, out)
        self.assertIn('ID <code>APP-77123</code> · <a href="https://boards.greenhouse.io/x/apply">form</a>', out)
        out = handle(f"/form_{ids[0][:8]}", self.db)
        assert_telegram_html(self, out)
        self.assertIn("Application ID: <code>APP-77123</code>", out)
        self.assertIn('<a href="https://boards.greenhouse.io/x/apply">Open form</a>', out)

    def test_a_sheet_from_before_2_3_reads_back_without_ghost_columns(self):
        from openpyxl import Workbook
        f = HOME / "data" / "old" / "Applications.xlsx"
        f.parent.mkdir(exist_ok=True)
        wb = Workbook()
        ws = wb.active
        ws.title = "Applications"
        ws.append(["Applied on", "Days since applied", "Job id", "Notes"])
        ws.append([None, 3, list(self.jobs.values())[0][:8], "old note"])
        wb.save(f)
        tracker.write(self.db, f)
        self.assertEqual(tracker.columns(self.db), [])                     # no "Days since applied" column of yours
        self.assertEqual(self.db.get(list(self.jobs.values())[0])["notes"], "old note")

    def test_telegram_mail_and_sheet_commands(self):
        from jobhunter.bot import handle
        from test_ops import assert_telegram_html
        out = handle("mail", self.db)
        assert_telegram_html(self, out)
        self.assertEqual(handle("/sheet", self.db), "__SHEET__")


if __name__ == "__main__":
    unittest.main()
