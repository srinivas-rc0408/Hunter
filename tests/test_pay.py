"""Pay is read right at the parser and shown exactly: one fixture per mistake seen on the live boards (19 Nov)."""
import datetime as dt
import json
import tempfile
import unittest

from _home import use, make_home

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter.db import DB, Job  # noqa: E402
from jobhunter.notify import pay_text  # noqa: E402
from jobhunter.score import score  # noqa: E402
from jobhunter.sources import ats_more  # noqa: E402
from jobhunter.sources.ats import scout  # noqa: E402
from jobhunter.sources.boards import row_to_job  # noqa: E402

NAUKRI = {"site": "naukri", "title": "Python Developer", "company": "Acme", "location": "Bengaluru",
          "job_url": "https://www.naukri.com/job-listings-x-1", "interval": "yearly", "currency": "INR",
          "salary_source": "direct_data"}


def row(**kw):
    return row_to_job({**NAUKRI, **kw})


def shown(job: Job) -> str:
    return pay_text(job.__dict__)


class Parsers(unittest.TestCase):
    def test_lacs_range_is_lpa(self):                       # Naukri "10-16 Lacs P.A." → 1000000 / 1600000 yearly
        j = row(min_amount=1000000, max_amount=1600000)
        self.assertEqual((j.pay_min, j.pay_max, j.pay_period, j.currency), (1000000, 1600000, "year", "INR"))
        self.assertEqual(shown(j), "₹10–16 LPA")

    def test_zero_min_is_up_to(self):                        # Naukri "0-24 Lacs P.A." was shown as "₹24 LPA"
        j = row(min_amount=0, max_amount=2400000)
        self.assertEqual((j.pay_min, j.pay_max), (None, 2400000))
        self.assertEqual(shown(j), "up to ₹24 LPA")

    def test_min_above_max_is_swapped(self):
        j = row(min_amount=1600000, max_amount=1000000)
        self.assertEqual((j.pay_min, j.pay_max), (1000000, 1600000))

    def test_no_pay_drops_unit_and_currency(self):           # "Not disclosed": no number, no unit left behind
        j = row(min_amount=None, max_amount=None)
        self.assertEqual((j.pay_min, j.pay_period, j.currency, shown(j)), (None, None, None, "not listed"))

    def test_naukri_stipend_month(self):                     # label "15,000/month" → monthly, one number
        j = row(min_amount=15000, max_amount=15000, interval="monthly", title="Data Intern")
        self.assertEqual((j.pay_period, shown(j)), ("month", "₹15,000/month"))

    def test_usd_is_not_inr(self):                           # LinkedIn "$20/hr - $100/hr"
        j = row(site="linkedin", min_amount=20, max_amount=100, interval="hourly", currency="USD")
        self.assertEqual(shown(j), "$20–100/hour (not INR)")

    def test_indeed_estimate_is_marked(self):
        from jobspy.indeed.model import IndeedCompensation
        from jobhunter.sources import boards
        boards._tag_indeed_estimates()
        import jobspy.indeed as ind
        est = IndeedCompensation.model_validate({"baseSalary": None, "currencyCode": None, "estimated": {
            "currencyCode": "INR", "baseSalary": {"unitOfWork": "YEAR", "range": {"min": 400000, "max": 650000}}}})
        listed = IndeedCompensation.model_validate({"currencyCode": "INR", "estimated": None, "baseSalary": {
            "unitOfWork": "MONTH", "range": {"min": 12000}}})
        self.assertEqual(ind.get_compensation(est).model_dump().get("salary_source"), "indeed_estimate")
        self.assertIsNone(ind.get_compensation(listed).model_dump().get("salary_source"))
        j = row(site="indeed", min_amount=400000, max_amount=650000, salary_source="indeed_estimate")
        self.assertEqual((j.pay_estimate, shown(j)), ("Indeed", "≈ ₹4–6.5 LPA (estimate by Indeed)"))
        g = row(site="glassdoor", min_amount=500000, max_amount=900000)
        self.assertEqual(shown(g), "≈ ₹5–9 LPA (estimate by Glassdoor)")

    def test_indeed_from_monthly(self):                      # "From ₹12,000 a month"
        j = row(site="indeed", min_amount=12000, max_amount=None, interval="monthly")
        self.assertEqual(shown(j), "from ₹12,000/month")

    def test_lever_month_is_not_hour(self):
        data = [{"id": "1", "text": "Data Intern", "hostedUrl": "https://jobs.lever.co/acme/1", "createdAt": 1763251200000,
                 "categories": {"location": "Bengaluru"}, "salaryRange": {"min": 20000, "max": 30000, "currency": "INR",
                                                                          "interval": "per-month-salary"}}]
        pay = scout().from_lever({"company": "Acme", "type": "lever"}, data)[0]["pay"]
        self.assertEqual((pay["period"], pay["currency"]), ("month", "INR"))
        self.assertEqual(scout().pay_from_text("$5,000 - $7,000 per month")["period"], "month")
        self.assertEqual(scout().pay_from_text("$45 to $60 per hour")["period"], "hour")
        self.assertIsNone(scout().pay_from_text("competitive pay, best in industry"))

    def test_jsonld_unit_and_competitive(self):
        def page(base):
            return ('<script type="application/ld+json">' + json.dumps({
                "@type": "JobPosting", "title": "AI Engineer", "hiringOrganization": {"name": "Acme"},
                "url": "/j/1", "baseSalary": base}) + "</script>")
        a = ats_more.parse_jsonld(page({"currency": "INR", "value": {"@type": "QuantitativeValue", "minValue": 30000,
                                                                     "maxValue": 40000, "unitText": "MONTH"}}), "https://acme.example/")
        self.assertEqual((a[0]["pay_min"], a[0]["pay_max"], a[0]["pay_period"]), (30000, 40000, "month"))
        b = ats_more.parse_jsonld(page({"currency": "INR", "value": {"minValue": "Competitive", "unitText": "YEAR"}}),
                                  "https://acme.example/")
        self.assertEqual((b[0]["pay_min"], b[0]["pay_max"], b[0]["currency"], b[0]["pay_period"]), (None, None, None, None))
        c = ats_more.parse_jsonld(page({"currency": "INR", "value": {"value": 900000, "unitText": "YEAR"}}), "https://acme.example/")
        self.assertEqual(pay_text(c[0]), "₹9 LPA")              # a single number is one number, not a range


class Display(unittest.TestCase):
    def test_rules(self):
        for j, want in [({"pay_min": 1000000, "pay_max": 1600000, "pay_period": "year", "currency": "INR"}, "₹10–16 LPA"),
                        ({"pay_min": 132000, "pay_max": 144000, "pay_period": "year", "currency": "INR"}, "₹1.3–1.4 LPA"),
                        ({"pay_min": 1100000, "pay_max": 1350000, "pay_period": "year", "currency": "INR"}, "₹11–13.5 LPA"),
                        ({"pay_min": 600000, "pay_period": "year", "currency": "INR"}, "from ₹6 LPA"),
                        ({"pay_min": 30000, "pay_max": 30000, "pay_period": "month", "currency": "INR"}, "₹30,000/month"),
                        ({"pay_min": 150000, "pay_max": 200000, "pay_period": "year", "currency": "INR",
                          "job_type": "internship", "title": "HR Intern"}, "₹12,500–16,667/month (₹1.5–2 LPA listed)"),
                        ({"pay_min": 60000, "pay_max": 70000, "pay_period": "year", "currency": "INR"},
                         "₹60,000–70,000/year as listed (looks like per month: check the posting)"),
                        ({"pay_min": 600000, "pay_period": "month", "currency": "INR"},
                         "from ₹6,00,000/month as listed (looks like per year: check the posting)"),
                        ({"pay_min": 152405, "pay_max": 179300, "pay_period": "year", "currency": "USD"}, "$152,405–179,300/year (not INR)"),
                        ({"pay_min": 90000, "pay_max": 120000, "pay_period": "year", "currency": "CAD"}, "C$90,000–120,000/year (not INR)"),
                        ({"pay_min": 12.5, "pay_period": "hour", "currency": "USD"}, "from $12.5/hour (not INR)"),
                        ({"pay_min": 500, "pay_max": 500, "pay_period": "week", "currency": "EUR"}, "€500/week (not INR)"),
                        ({"pay_min": 50000, "pay_max": 70000}, "50,000–70,000 (currency not listed)"),
                        ({"pay_min": None}, "not listed"), ({}, "not listed")]:
            self.assertEqual(pay_text(j), want, j)


class Scoring(unittest.TestCase):
    def job(self, **kw):
        return score(Job(kw.pop("title", "Software Engineer"), "Acme", "Bengaluru, Karnataka", url="https://x.example/j",
                         source="naukri", description="Python freshers", posted_at=str(dt.date.today()), **kw))

    def test_low_yearly_is_not_a_stipend(self):              # ₹1.32 LPA was read as ₹1.32 L a month and passed
        self.assertEqual(self.job(pay_min=132000, pay_max=144000, pay_period="year", currency="INR").status, "filtered")

    def test_floor_uses_the_top_of_the_range(self):
        self.assertNotEqual(self.job(pay_min=400000, pay_max=700000, pay_period="year", currency="INR").status, "filtered")
        self.assertEqual(self.job(pay_min=300000, pay_max=500000, pay_period="year", currency="INR").status, "filtered")

    def test_intern_yearly_stipend(self):                    # ₹1.5–2 LPA = ₹12,500–16,667 a month: clears ₹15k
        self.assertNotEqual(self.job(title="Data Intern", pay_min=150000, pay_max=200000, pay_period="year",
                                     currency="INR", job_type="internship").status, "filtered")
        self.assertEqual(self.job(title="Data Intern", pay_min=100000, pay_max=120000, pay_period="year",
                                  currency="INR", job_type="internship").status, "filtered")

    def test_estimates_and_wrong_units_never_drop(self):
        self.assertNotEqual(self.job(pay_min=200000, pay_max=300000, pay_period="year", currency="INR",
                                     pay_estimate="Indeed").status, "filtered")
        self.assertNotEqual(self.job(pay_min=60000, pay_max=70000, pay_period="year", currency="INR").status, "filtered")


class Merge(unittest.TestCase):
    def test_listed_replaces_estimate_and_ranges_never_mix(self):
        db = DB(tempfile.mktemp(suffix=".db"))
        base = dict(title="AI Engineer", company="Acme", location="Bengaluru", url="https://in.indeed.com/viewjob?jk=1")
        db.upsert(Job(**base, source="indeed", pay_min=400000, pay_max=650000, pay_period="year", currency="INR",
                      pay_estimate="Indeed"))
        db.upsert(Job(**base, source="naukri", pay_min=None, pay_max=1600000, pay_period="year", currency="INR"))
        j = db.get(Job(**base).id)
        self.assertEqual((j["pay_min"], j["pay_max"], j["pay_estimate"]), (None, 1600000, None))
        db.upsert(Job(**base, source="linkedin", pay_min=20, pay_max=100, pay_period="hour", currency="USD"))
        self.assertEqual(db.get(j["id"])["pay_max"], 1600000)  # first listed pay stays whole


if __name__ == "__main__":
    unittest.main()
