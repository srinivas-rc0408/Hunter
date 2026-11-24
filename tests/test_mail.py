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
            return "OK", [(b"1", self.msgs[int(a[0]) - 1])]
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

    def test_real_replies_are_kept_and_sorted(self):
        m = self.m("info@naukri.com", "Update: Your application has not been accepted", "Regards, recruiter")
        self.assertTrue(mail.is_job_mail(m, []))
        self.assertEqual(mail.classify(m), "rejected")
        m = self.m("jobs-noreply@linkedin.com", "Your application was viewed by Astra Security", "")
        self.assertTrue(mail.is_job_mail(m, []))
        self.assertEqual(mail.classify(m), "viewed")
        m = self.m("jobs-noreply@linkedin.com", "Your application was sent to Zoho Corporation", "")
        self.assertTrue(mail.is_job_mail(m, []))

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
        again = mail.sync(imap_factory=lambda: fake, quiet=True)
        self.assertEqual(again, {})                                         # every message handled once
        # the Excel tracker
        from openpyxl import load_workbook
        p = tracker.write(self.db, HOME / "data" / "t.xlsx")
        wb = load_workbook(p)
        self.assertEqual(wb.sheetnames, ["Summary", "Applications", "Email replies"])
        rows = list(wb["Applications"].iter_rows(values_only=True))
        self.assertEqual(rows[0][:5], ("Applied on", "Time (IST)", "★", "Company", "Role"))
        self.assertEqual(len(rows) - 1, 4)
        zoho = next(r for r in rows if r[4] == "ML Intern")
        self.assertEqual((zoho[3], zoho[9], zoho[12]), ("Zoho Corporation", "interview", 1))
        self.assertTrue(wb["Applications"].cell(row=2, column=16).hyperlink)
        self.assertEqual(len(list(wb["Email replies"].iter_rows())) - 1, 4)
        summ = dict(r[:2] for r in wb["Summary"].iter_rows(values_only=True) if r[0])
        self.assertEqual(summ["Applications (total)"], 4)
        self.assertEqual(summ["Companies applied to"], 3)

    def test_telegram_mail_and_sheet_commands(self):
        from jobhunter.bot import handle
        from test_ops import assert_telegram_html
        out = handle("mail", self.db)
        assert_telegram_html(self, out)
        self.assertEqual(handle("/sheet", self.db), "__SHEET__")


if __name__ == "__main__":
    unittest.main()
