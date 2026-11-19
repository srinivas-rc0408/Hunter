"""Govt notices: every card value comes from the notification (3 real, scrubbed notices), else "see notification"."""
import datetime as dt
import unittest
from pathlib import Path

from _home import make_home, use

HOME = make_home()
FIX = Path(__file__).parent / "fixtures" / "govt"


def setUpModule():
    use(HOME)


from jobhunter.db import DB, Job  # noqa: E402
from jobhunter.notify import govt_card, govt_open  # noqa: E402
from jobhunter.sources import notice  # noqa: E402
from test_ops import assert_telegram_html  # noqa: E402

ASHA = {"date_of_birth": "2003-05-01", "category": "SC"}       # test person, not real facts


def read(name, kind):
    return notice.read((FIX / name).read_text(), "", kind)


class Notices(unittest.TestCase):
    def test_kea_state_scanned_kannada(self):
        d = read("kea-fda-sda-2026-ocr.txt", "state")
        self.assertEqual(d["pay"], "₹44,425–83,700, ₹34,100–67,700")
        self.assertEqual((d["vacancies"], d["sc_vacancies"]), (6, 2))
        self.assertEqual((d["age_limit"], d["age_limit_sc"]), (40, 45))
        self.assertEqual(d["fee_sc"], "₹500")
        self.assertEqual((d["released"], d["last_date"], d["age_cutoff"]), ("2026-05-19", "2026-06-03", "2026-06-03"))
        self.assertEqual(d["apply_url"], "https://cetonline.karnataka.gov.in/kea/")

    def test_beml_psu_with_karnataka_posting(self):
        d = read("beml-kp-s-20-2026.txt", "psu")
        self.assertEqual(d["pay"], "₹16,900–60,650")
        self.assertEqual((d["vacancies"], d["sc_vacancies"]), (44, 7))
        self.assertIn("Bangalore", d["karnataka_places"])
        self.assertEqual(d["fee_sc"], "₹0 (exempt)")
        self.assertEqual((d["released"], d["last_date"]), ("2026-11-13", "2026-12-06"))
        self.assertEqual(d["apply_url"], "https://www.bemlindia.in")

    def test_drdo_central(self):
        d = read("drdo-rac-157.txt", "central")
        self.assertIn("Level 13", d["pay"])
        self.assertEqual(d["vacancies"], 33)
        self.assertEqual((d["released"], d["last_date"], d["age_cutoff"]), ("2026-05-30", "2026-06-19", "2026-06-19"))
        self.assertEqual(d["apply_url"], "https://rac.gov.in")
        self.assertEqual(d["karnataka_places"], [])

    def test_a_date_counted_from_the_last_date_is_not_the_last_date(self):
        t = ("Ex-servicemen whose assignment is due to be completed within one year from the\n"
             "last date of receipt of application i.e., 20.07.2027) otherwise than by way of dismissal")
        self.assertIsNone(notice.read(t)["last_date"])
        self.assertEqual(notice.read("Last date of receipt of application: 21.07.2026")["last_date"], "2026-07-21")

    def test_nothing_is_guessed(self):
        d = notice.read("Recruitment of Clerks. Apply online.", "Clerk", "central")
        for k in ("pay", "vacancies", "sc_vacancies", "age_limit", "fee_sc", "released", "last_date", "exam_date"):
            self.assertIsNone(d[k], k)


def govt_job(title, details, deadline, verdict="eligible", kind="psu", location="All India", reasons=None):
    j = Job(title, "BEML <Ltd> & Co", location, url="https://x.example/n?a=1&b=2", source="govt:beml",
            kind="govt", deadline=deadline, details=details)
    d = vars(j) | {"id": j.id, "status": "new", "score": 80, "flags": [f"govt:{kind}", f"eligibility:{verdict}"],
                   "reasons": reasons or ["✓ age 23 ≤ 35 (SC +5)"]}
    return d


class Cards(unittest.TestCase):
    TODAY = dt.date(2026, 12, 4)

    def test_card_has_every_line_from_the_notice(self):
        d = read("beml-kp-s-20-2026.txt", "psu") | {"pdf": "https://www.bemlindia.in/a.pdf?x=1&y=2"}
        c = govt_card(govt_job("Officer <Grade> & II", d, "2026-12-06"), ASHA, self.TODAY)
        assert_telegram_html(self, c)
        for want in ("🔴 <b>closes in 2 days</b>", "🏛 <b>", "📍 All India", "💰 ₹16,900–60,650", "👥 44 posts · SC 7",
                     "✅ Eligible: age 23", "🎂 age limit", "you: 23 on the cut-off 6 Dec 2026", "💸 fee for SC: ₹0 (exempt)",
                     "🗓 released 13 Nov 2026 · last date 6 Dec 2026 (2 days left)", "📄 <a href=", "🔗 <a href="):
            self.assertIn(want, c)

    def test_unknown_values_say_see_notification(self):
        c = govt_card(govt_job("Clerk", {}, None, "check", reasons=["? GATE paper not stated"]), ASHA, self.TODAY)
        assert_telegram_html(self, c)
        self.assertIn("💰 see notification", c)
        self.assertIn("👥 see notification", c)
        self.assertIn("🟡 Check: GATE paper not stated", c)
        self.assertIn("last date see notification", c)
        self.assertNotIn("🔴", c)

    def test_sort_hide_and_scope(self):
        jobs = [govt_job("late check", {}, "2026-12-20", "check"),
                govt_job("expired", {}, "2026-12-01"),
                govt_job("ka eligible", {"karnataka_places": ["Bangalore"]}, "2026-12-30"),
                govt_job("soon eligible", {}, "2026-12-10"),
                govt_job("state", {}, "2026-12-15", kind="state")]
        self.assertEqual([j["title"] for j in govt_open(jobs, self.TODAY)],
                         ["soon eligible", "state", "ka eligible", "late check"])
        self.assertEqual([j["title"] for j in govt_open(jobs, self.TODAY, "karnataka")], ["state", "ka eligible"])
        self.assertNotIn("state", [j["title"] for j in govt_open(jobs, self.TODAY, "central")])
        self.assertNotIn("late check", [j["title"] for j in govt_open(jobs, self.TODAY, eligible_only=True)])


class Commands(unittest.TestCase):
    def test_govt_commands_are_valid_html(self):
        from jobhunter.bot import handle
        db = DB(HOME / "data" / "govt.db")
        today = dt.date.today()
        for i, (k, v) in enumerate([("psu", "eligible"), ("state", "check"), ("central", "ineligible")]):
            j = Job(f"Post {i} <A&B>", "Org & Co", "Karnataka" if k == "state" else "All India", url=f"https://g.example/{i}",
                    source="govt:x", kind="govt", deadline=str(today + dt.timedelta(days=i + 1)),
                    details={"pay": "Level 10", "vacancies": 5})
            db.upsert(j)
            db.set_score(j.id, 70, ["✓ fine"], [f"govt:{k}", f"eligibility:{v}"], False)
        for cmd in ("/govt", "govt all", "/govt karnataka", "/govt central", "/eligible", "/deadlines", "deadlines 2"):
            out = handle(cmd, db)
            assert_telegram_html(self, out)
        self.assertIn("Post 0", handle("/eligible", db))
        self.assertNotIn("Post 2", handle("/govt", db))           # not eligible: only in /govt all
        self.assertIn("Post 2", handle("/govt all", db))
        self.assertIn("Post 1", handle("/govt karnataka", db))
        self.assertNotIn("Post 1", handle("/govt central", db))
        self.assertIn("🔴", handle("/deadlines", db))


class Links(unittest.TestCase):
    def test_menu_links_are_not_notices(self):
        from jobhunter.sources.govt import _notice_like
        from jobhunter.sources.pages import Link
        inc = ["recruitment", "advertisement", "vacancies", "engineer"]
        for t, want in (("About Us", False), ("Vacancies", False), ("Advt No.15-2026-FINAL_0.pdf", True),
                        ("Recruitment of Project Engineer on contract", True), ("Walk-in 12.12.2026", True)):
            self.assertEqual(_notice_like(Link("p", "https://x.example/" + t.replace(" ", "_"), t, "Recruitment"), inc), want, t)

    def test_title_from_the_row_when_the_link_says_english(self):
        from jobhunter.sources.govt import notice_title
        from jobhunter.sources.pages import Link
        row = "Recruitment of Specialist Cadre Officers Advt CRPD/SCO/2026-27/12 English (1 MB) Hindi (1 MB)"
        self.assertEqual(notice_title(Link("p", "h", "English (1 MB)", row)),
                         "Recruitment of Specialist Cadre Officers Advt CRPD/SCO/2026-27/12")
        self.assertEqual(notice_title(Link("p", "h", "APPLY ONLINE (19.11.2026 TO 03.12.2026)",
                                           "Recruitment of Apprentices APPLY ONLINE (19.11.2026 TO 03.12.2026)")),
                         "Recruitment of Apprentices")
        self.assertEqual(notice_title(Link("p", "h", "Advt No.15-2026.pdf", "row")), "Advt No.15-2026.pdf")


class Locking(unittest.TestCase):
    def test_seen_link_holds_no_write_lock(self):
        """A scan re-visiting a board must not lock the DB for the bot while it downloads PDFs."""
        a, b = DB(HOME / "data" / "lock.db"), DB(HOME / "data" / "lock.db")
        self.assertTrue(a.link_is_new("p", "h", "t"))
        self.assertFalse(a.link_is_new("p", "h", "t"))
        self.assertFalse(a.c.in_transaction)
        b.c.execute("INSERT INTO runs VALUES ('x','x',0,0,0,'[]')")
        b.c.commit()


if __name__ == "__main__":
    unittest.main()
