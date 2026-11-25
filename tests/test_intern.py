"""Internship/fresher sources (Internshala, Unstop, Wellfound) on saved public pages, plus the robots.txt matcher."""
import datetime as dt
import json
import unittest
from pathlib import Path

from _home import make_home, use

HOME = make_home()
FIX = Path(__file__).parent / "fixtures" / "intern"
TODAY = dt.date(2026, 11, 25)


def setUpModule():
    use(HOME)


from jobhunter.sources import intern  # noqa: E402
from jobhunter.util import parse_robots, robots_match  # noqa: E402


class Internshala(unittest.TestCase):
    def setUp(self):
        self.cards = intern.internshala_cards((FIX / "internshala-cards.html").read_text(), "internship", TODAY)

    def test_cards(self):
        self.assertEqual(len(self.cards), 3)
        c = self.cards[0]
        self.assertEqual((c["company"], c["places"], c["duration"]), ("SmartEd Innovations", ["Bangalore"], "6 Months"))
        self.assertEqual(c["posted"], "2026-11-04")          # "3 weeks ago" from the card, not today
        self.assertEqual(self.cards[2]["posted"], "2026-11-25")

    def test_detail_gives_start_and_apply_by(self):
        d = intern.internshala_detail((FIX / "internshala-detail.html").read_text())
        self.assertEqual((d["start"], d["apply_by"], d["duration"]), ("Immediately", "30 Nov' 26", "6 Months"))
        j = intern.card_to_job(self.cards[0], d)
        self.assertEqual((j.title, j.location, j.job_type), ("Product Development Intern", "Bangalore, India", "internship"))
        self.assertEqual((j.pay_min, j.pay_max, j.pay_period, j.currency), (12000, 15000, "month", "INR"))
        self.assertEqual(j.deadline, "2026-11-30")
        self.assertEqual(j.details["start_date"], "Immediately")

    def test_pay(self):
        self.assertEqual(intern.pay("₹ 30,000 lump sum", "3 Months")[:3], (10000, None, "month"))
        self.assertEqual(intern.pay("₹ 30,000 lump sum")[:3], (None, None, None))    # no duration: never guessed
        self.assertEqual(intern.pay("Unpaid")[3], "unpaid")

    def test_ago(self):
        self.assertEqual(intern.ago("2 days ago", TODAY), "2026-11-23")
        self.assertIsNone(intern.ago("Be an early applicant", TODAY))


class Unstop(unittest.TestCase):
    def test_items(self):
        js = intern.unstop_items(json.loads((FIX / "unstop.json").read_text()))
        self.assertEqual(len(js), 2)
        self.assertTrue(all(j.job_type == "internship" and j.source == "unstop" for j in js))
        paid = next(j for j in js if j.pay_min)
        self.assertEqual((paid.pay_min, paid.pay_period, paid.location), (15000, "month", "Remote, India"))
        self.assertTrue(all(j.deadline and j.posted_at for j in js))


class Wellfound(unittest.TestCase):
    def test_items(self):
        js = intern.wellfound_items((FIX / "wellfound.html").read_text())
        self.assertEqual(len(js), 25)
        j = js[0]
        self.assertEqual((j.company, j.location, j.pay_min, j.pay_max, j.pay_period),
                         ("Simplismart", "Bengaluru, India", 2000000, 3000000, "year"))
        self.assertTrue(all(x.posted_at for x in js))
        self.assertTrue(all("India" in x.location for x in js))

    def test_foreign_city_dropped(self):
        html = (FIX / "wellfound.html").read_text().replace('"locationNames":["Bengaluru"]', '"locationNames":["New York"]', 1)
        self.assertEqual(len(intern.wellfound_items(html)), 24)

    def test_pay_only_rupees(self):
        self.assertEqual(intern._wf_pay("$100k – $150k", False), (None, None, None, None))
        self.assertEqual(intern._wf_pay("₹15K – ₹20K", True), (15000, 20000, "month", "INR"))


class Robots(unittest.TestCase):
    TXT = "User-agent: *\nDisallow: /internships/\nAllow: /internships/detail/\nDisallow: /*?q=\nAllow: /api/public/*\n"

    def test_longest_rule_wins(self):
        r = parse_robots(self.TXT)
        self.assertFalse(robots_match(r, "/internships/ml/"))
        self.assertTrue(robots_match(r, "/internships/detail/abc"))
        self.assertFalse(robots_match(r, "/jobs?q=ml"))
        self.assertTrue(robots_match(r, "/api/public/opportunity/search-result?x=1"))
        self.assertTrue(robots_match(r, "/anything"))

    def test_named_group_beats_star(self):
        r = parse_robots("User-agent: *\nDisallow: /\n\nUser-agent: jobhunter\nAllow: /\n")
        self.assertTrue(robots_match(r, "/x"))


if __name__ == "__main__":
    unittest.main()
