"""Check mode in real Chromium: panel on the page, you fix fields, press Submit, the bot learns.
Plus: browser lock/profile fixes, field corrections, dream companies, resume upload, Telegram commands."""
import json
import os
import unittest
from pathlib import Path

from _home import use, ROOT, make_home, write_pdf

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter import config  # noqa: E402
from jobhunter.apply import browser as B  # noqa: E402
from jobhunter.apply import fields as F  # noqa: E402
from jobhunter.apply import flows  # noqa: E402
from jobhunter.db import DB, Job  # noqa: E402

FX = ROOT / "tests" / "fixtures" / "forms"
JOB = {"id": "chk12345", "title": "ML Intern", "company": "Acme", "score": 80, "dream": False, "url": ""}

# A pretend user: whenever a new panel appears, optionally edit fields, then press a panel button.
USER_JS = r"""
(plan) => { window.__simN = 0; setInterval(() => {
  const h = document.getElementById('jh-panel-host'); if (!h || h.dataset.sim) return; h.dataset.sim = '1';
  const step = plan[Math.min(window.__simN++, plan.length - 1)];
  setTimeout(() => {
    for (const [sel, val] of (step.set || [])) { const e = document.querySelector(sel);
      if (e.type === 'radio') { e.checked = true; e.dispatchEvent(new Event('change', {bubbles: true})); }
      else { e.value = val; e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true})); } }
    setTimeout(() => {
      if (step.site) document.querySelector(step.site).click();
      else h.shadowRoot.querySelector(`button[data-a="${step.press}"]`).click();
    }, 1300);
  }, 300); }, 200); }
"""


class CheckMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.resume = write_pdf(HOME / "config" / "resume.pdf")
        cls.db = DB()
        flows._report = lambda res: None

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        config.reload()
        config.cfg("search")["apply"]["mode"] = "check"
        config.cfg("search")["apply"]["wait_for_you_sec"] = 60
        flows.VISIBLE, flows.APPROVED, flows.SUBMIT_NOW, flows.TITLE = True, False, False, "ML Intern · Acme"
        B.RECORD.clear()
        F._bank_path().unlink(missing_ok=True)

    def tearDown(self):
        config.reload()
        F._bank_path().unlink(missing_ok=True)

    def page(self, name, plan):
        p = self.browser.new_page()
        p.goto((FX / f"{name}.html").as_uri())
        p.evaluate(USER_JS, plan)
        return p

    def test_panel_lists_fields_then_you_fix_and_submit_and_it_learns(self):
        p = self.page("basic_ats", [{"set": [["#cg", "8.05"], ["input[name=reloc][value=n]", ""]], "press": "submit"}])
        o = flows.single_page(p, self.db, JOB, self.resume, None, "greenhouse", dry=False)
        self.assertEqual(o.status, "applied", o.why)
        self.assertFalse(o.auto)                                   # you pressed Submit, not the bot on its own
        self.assertEqual(o.learned, 2)
        self.assertIn("thank you", p.inner_text("body").lower())
        bank = F.load_bank()
        cg = next(v for v in bank.values() if "cgpa" in v["label"].lower())
        self.assertEqual((cg["answer"], cg["override"]), ("8.05", True))
        # next form: your correction wins over the bot's own answer (CGPA 8.0 from the profile)
        a = F.answer(cg["label"], "text")
        self.assertEqual((a.value, a.basis), ("8.05", "learned"))
        rec = next(r for r in B.RECORD if "cgpa" in (r["label"] or "").lower())
        self.assertEqual((rec["value"], rec["basis"]), ("8.05", "you"))

    def test_nothing_is_sent_until_you_press_submit(self):
        p = self.page("basic_ats", [{"press": "later"}])
        o = flows.single_page(p, self.db, JOB, self.resume, None, "greenhouse", dry=False)
        self.assertEqual(o.status, "needs_you")
        self.assertNotIn("thank you", p.inner_text("body").lower())
        self.assertEqual(p.input_value("#first_name"), "Asha")      # still filled, waiting for you

    def test_skip(self):
        p = self.page("basic_ats", [{"press": "skip"}])
        self.assertEqual(flows.single_page(p, self.db, JOB, self.resume, None, "greenhouse", dry=False).status, "skipped")

    def test_you_press_the_sites_own_submit(self):
        p = self.page("basic_ats", [{"set": [["#cg", "8.10"]], "site": "button[type=submit]"}])
        o = flows.single_page(p, self.db, JOB, self.resume, None, "greenhouse", dry=False)
        self.assertEqual(o.status, "applied", o.why)
        self.assertEqual(o.learned, 1)

    def test_panel_shows_every_field_with_its_source(self):
        p = self.browser.new_page()
        p.goto((FX / "needs_you.html").as_uri())
        res = B.fill_form(p, JOB, self.resume)
        from jobhunter.apply import panel
        self.assertTrue(panel.show(p, res.fills, "t", final=True))
        rows = p.evaluate("() => [...document.getElementById('jh-panel-host').shadowRoot.querySelectorAll('.r .tg')].map(e => e.textContent)")
        self.assertEqual(len(rows), len(res.fills))
        self.assertIn("EMPTY: type it", rows)                      # expected CTC etc. are never guessed
        self.assertIn("your details", rows)

    def test_linkedin_every_step_waits_for_you(self):
        p = self.page("linkedin_modal", [{"press": "next"}, {"press": "next"}, {"press": "submit"}])
        o = flows.linkedin(p, self.db, JOB, self.resume, None, dry=False)
        self.assertEqual(o.status, "applied", o.why)
        self.assertIn("application was sent", p.inner_text("body").lower())

    def test_dry_run_never_waits(self):
        p = self.browser.new_page()
        p.goto((FX / "basic_ats.html").as_uri())
        o = flows.single_page(p, self.db, JOB, self.resume, None, "greenhouse", dry=True)
        self.assertEqual(o.status, "filled")

    def test_submit_now_skips_the_panel(self):
        flows.APPROVED = flows.SUBMIT_NOW = True
        p = self.browser.new_page()
        p.goto((FX / "basic_ats.html").as_uri())
        o = flows.single_page(p, self.db, JOB, self.resume, None, "greenhouse", dry=False)
        self.assertEqual(o.status, "applied", o.why)

    def test_never_learns_caste_files_or_essays(self):
        from jobhunter.apply import panel
        saved = panel.learn([
            {"label": "Caste category", "key": "diversity", "before": None, "after": "X", "jh": "1", "type": "text"},
            {"label": "Resume", "key": "resume", "before": None, "after": "a.pdf", "jh": "2", "type": "file"},
            {"label": "Why us?", "key": "unknown", "before": None, "after": "x" * 400, "jh": "3", "type": "textarea"},
            {"label": "Notice period", "key": "notice", "before": "Immediate", "after": "15 days", "jh": "4", "type": "text"}],
            "greenhouse")
        self.assertEqual([c["label"] for c in saved], ["Notice period"])


class BrowserStart(unittest.TestCase):
    def test_lock_of_a_dead_browser_is_cleared(self):
        d = HOME / "data" / "prof-test"
        d.mkdir(parents=True, exist_ok=True)
        os.symlink("myhost-999999", d / "SingletonLock")
        self.assertIsNone(B._profile_lock(d))
        self.assertFalse(os.path.lexists(d / "SingletonLock"))

    def test_lock_of_a_running_browser_is_reported(self):
        d = HOME / "data" / "prof-test2"
        d.mkdir(parents=True, exist_ok=True)
        os.symlink(f"myhost-{os.getpid()}", d / "SingletonLock")
        self.assertEqual(B._profile_lock(d), os.getpid())

    def test_profile_does_not_restore_old_tabs(self):
        d = HOME / "data" / "prof-test3"
        (d / "Default").mkdir(parents=True, exist_ok=True)
        (d / "Default" / "Preferences").write_text(json.dumps({"profile": {"exit_type": "Crashed"}}))
        B._calm_profile(d)
        p = json.loads((d / "Default" / "Preferences").read_text())
        self.assertEqual((p["profile"]["exit_type"], p["session"]["restore_on_startup"]), ("Normal", 5))


class Fixes(unittest.TestCase):
    def setUp(self):
        F._bank_path().unlink(missing_ok=True)
        self.db = DB()
        j = Job("Data Intern", "Fixco", "Bengaluru, Karnataka", url="https://x.example/f", source="linkedin")
        self.db.upsert(j)
        self.jid = j.id
        self.db.add_application(j.id, "greenhouse", "needs_you", False, False, True, "preview", [
            {"label": "Notice period", "key": "notice", "type": "text", "value": "Immediate", "basis": "profile", "required": True},
            {"label": "Willing to relocate?", "key": "relocate", "type": "radio", "value": "Yes", "basis": "profile",
             "options": ["Yes", "No"], "required": True}], "", "", None, "", "")

    def tearDown(self):
        F._bank_path().unlink(missing_ok=True)

    def test_fix_saves_and_learns(self):
        from jobhunter.apply.fixes import fix
        ok, msg = fix(self.db, self.jid[:8], 1, "15 days")
        self.assertTrue(ok, msg)
        self.assertEqual(self.db.applications(self.jid)[0]["fields"][0]["value"], "15 days")
        self.assertEqual(F.answer("Notice period").value, "15 days")

    def test_fix_checks_options_and_numbers(self):
        from jobhunter.apply.fixes import fix
        self.assertFalse(fix(self.db, self.jid, 2, "maybe")[0])
        self.assertTrue(fix(self.db, self.jid, 2, "no")[0])
        self.assertEqual(self.db.applications(self.jid)[0]["fields"][1]["value"], "No")
        self.assertFalse(fix(self.db, self.jid, 9, "x")[0])

    def test_telegram_form_and_fix(self):
        from jobhunter.bot import handle
        from test_ops import assert_telegram_html
        out = handle(f"/form_{self.jid[:8]}", self.db)
        assert_telegram_html(self, out)
        self.assertIn("1. Notice period", out)
        out = handle(f"/fix_{self.jid[:8]} 1 30 days", self.db)
        assert_telegram_html(self, out)
        self.assertIn("30 days", out)
        out = handle(f"fix {self.jid[:8]} 1 45 days", self.db)
        self.assertIn("45 days", out)


class Dreams(unittest.TestCase):
    def setUp(self):
        self.f = config.path("config", "profile.yaml")
        self.orig = self.f.read_text()

    def tearDown(self):
        self.f.write_text(self.orig)
        config.reload()

    def test_add_and_remove(self):
        from jobhunter import dreams
        from jobhunter.score import is_dream
        ok, msg = dreams.add("Zoho")
        self.assertTrue(ok, msg)
        self.assertIn("Zoho", dreams.names())
        self.assertEqual(is_dream("Zoho Corporation"), "Zoho")
        self.assertFalse(dreams.add("zoho")[0])                    # no duplicates
        self.assertTrue(dreams.remove("Zoho")[0])
        self.assertNotIn("Zoho", dreams.names())
        self.assertIn("# Add more of yours below", self.f.read_text())   # comments kept

    def test_remove_with_aliases_block(self):
        from jobhunter import dreams
        self.assertTrue(dreams.remove("Aditya Birla Group")[0])
        prof = config.cfg("profile")
        self.assertNotIn("Aditya Birla Group", prof["dream_companies"])
        self.assertNotIn("Aditya Birla Group", prof.get("dream_aliases") or {})
        self.assertIn("Birlasoft", prof["dream_aliases"])

    def test_telegram_dream(self):
        from jobhunter import bot, dreams
        from test_ops import assert_telegram_html
        real = dreams.after_change
        dreams.after_change = lambda *a, **k: ["re-scored 0 open jobs"]
        try:
            out = bot.handle("dream Sarvam AI", DB())
            assert_telegram_html(self, out)
            self.assertIn("Sarvam AI is now a dream company", out)
            self.assertIn("Sarvam AI", bot.handle("/dreams", DB()))
            self.assertIn("removed", bot.handle("/undream Sarvam AI", DB()))
        finally:
            dreams.after_change = real


class Resume(unittest.TestCase):
    def _pdf(self, pages=2, text=True):
        from reportlab.pdfgen import canvas
        f = HOME / "data" / f"r{pages}{text}.pdf"
        c = canvas.Canvas(str(f))
        for _ in range(pages):
            if text:
                for i in range(30):
                    c.drawString(72, 760 - i * 20, f"Line {i} Python FastAPI LangGraph RAG projects and internship")
            c.showPage()
        c.save()
        return f

    def test_use_checks_and_keeps_history(self):
        from jobhunter.apply.runner import my_resume
        from jobhunter.resumes import use
        self.assertFalse(use(data=b"hello")[0])
        self.assertFalse(use(self._pdf(5))[0])                     # 5 pages: too long
        self.assertFalse(use(self._pdf(1, text=False))[0])          # image-only: ATS can't read it
        ok, msg = use(self._pdf(2))
        self.assertTrue(ok, msg)
        self.assertEqual(my_resume().name, "Asha_K_R_Resume.pdf")  # clean upload name from your profile
        ok, _ = use(self._pdf(1))
        self.assertTrue(ok)
        self.assertTrue(list((config.data_dir() / "resume-history").glob("resume-*.pdf")))


if __name__ == "__main__":
    unittest.main()
