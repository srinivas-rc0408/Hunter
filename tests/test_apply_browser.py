"""Real Chromium against local copies of the form shapes the bot meets.

basic_ats.html       Greenhouse-style page, every required field a basic fact → AUTO-SUBMIT
needs_you.html       expected CTC + essay + per-skill years → fills, WAITS for you
linkedin_modal.html  3-step Easy Apply modal (select, radio, number, follow-company) → AUTO-SUBMIT
"""
import unittest
from pathlib import Path

from _home import use, ROOT, make_home, write_pdf

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter.apply import flows  # noqa: E402
from jobhunter.db import DB  # noqa: E402

FX = ROOT / "tests" / "fixtures" / "forms"
JOB = {"id": "test1234", "title": "ML Intern", "company": "Acme", "score": 80, "dream": False, "url": ""}


class Browser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.resume = write_pdf(HOME / "config" / "resume.pdf")
        cls.db = DB()
        cls.waited = []
        flows.wait_for_you = lambda page, res, site, root=None: (cls.waited.append(site), (False, 0))[1]
        flows._report = lambda res: None

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def page(self, name):
        p = self.browser.new_page()
        p.goto((FX / f"{name}.html").as_uri())
        return p

    def test_basic_form_auto_submits(self):
        p = self.page("basic_ats")
        o = flows.single_page(p, self.db, JOB, self.resume, None, "greenhouse", dry=False)
        self.assertEqual((o.status, o.auto), ("applied", True), o.why)
        self.assertIn("thank you", p.inner_text("body").lower())

    def test_basic_form_fills_correctly(self):
        p = self.page("basic_ats")
        o = flows.single_page(p, self.db, JOB, self.resume, None, "greenhouse", dry=True)
        self.assertIn("AUTO-SUBMIT", o.why)
        v = lambda sel: p.input_value(sel)
        self.assertEqual(v("#first_name"), "Asha")
        self.assertEqual(v("#dob"), "21/07/2004")
        self.assertEqual(v("#fn"), "Ramesh K")
        self.assertEqual(v("#mn"), "Lakshmi")
        self.assertEqual(p.locator("#deg").evaluate("e => e.options[e.selectedIndex].text"), "B.E./B.Tech")
        self.assertTrue(p.is_checked("input[name=reloc][value=y]"))
        self.assertTrue(p.is_checked("input[name=bk][value=n]"))       # backlogs 0 → "No"
        self.assertTrue(p.is_checked("#gdpr"))
        self.assertFalse(p.is_checked("#news"))                          # marketing left unticked
        self.assertEqual(v("#why"), "")                                  # optional essay left empty
        self.assertEqual(p.locator("#resume").evaluate("e => e.files[0].name"), "resume.pdf")

    def test_non_basic_form_waits(self):
        before = len(self.waited)
        p = self.page("needs_you")
        o = flows.single_page(p, self.db, JOB, self.resume, None, "greenhouse", dry=False)
        self.assertEqual(o.status, "needs_you")
        self.assertEqual(len(self.waited), before + 1)
        self.assertNotIn("received", p.inner_text("body").lower())     # never submitted
        self.assertEqual(p.input_value("#name"), "Asha K R")         # but filled what it could
        for m in ("Expected CTC", "Why do you want", "Kubernetes"):
            self.assertTrue(any(m in x for x in o.missing), o.missing)

    def test_linkedin_easy_apply_multistep(self):
        p = self.page("linkedin_modal")
        o = flows.linkedin(p, self.db, JOB, self.resume, None, dry=False)
        self.assertEqual((o.status, o.auto), ("applied", True), o.why)
        self.assertIn("application was sent", p.inner_text("body").lower())

    def test_linkedin_apply_on_company_site_link_in_new_tab(self):
        p = self.page("linkedin_external")
        o = flows.linkedin(p, self.db, JOB, self.resume, None, dry=False)
        self.assertEqual(o.status, "applied", o.why)
        tab = p.context.pages[-1]
        self.assertIn("thank you", tab.inner_text("body").lower())

    def test_linkedin_new_ui_dialog_without_form(self):
        p = self.page("linkedin_sdui")          # real 2026 Easy Apply markup: <dialog>, no <form>
        o = flows.linkedin(p, self.db, JOB, self.resume, None, dry=True)
        self.assertNotIn("didn't open", o.why)
        self.assertEqual(p.get_attribute("#ea", "data-tel"), "9000000001")   # step 1 filled before Next
        self.assertIn("would", o.why)                    # reached the last step, not "too many steps"
        self.assertIn(self.resume.name, p.inner_text("dialog"))   # resume went in through the file picker

    def _li_step(self, n):
        p = self.browser.new_page()
        p.goto((FX / "linkedin_sdui_steps.html").as_uri() + f"#{n}")
        p.click("button[aria-label='Easy Apply to this job']")
        return p

    def test_linkedin_2026_radios_and_limits_read_like_a_person(self):
        from jobhunter.apply import browser as B
        p = self._li_step(3)                    # live: label "Yes", options "radio-group-_r_2c_"
        f = {x["label"]: x for x in B.scan(p, flows.LI_MODAL)}
        g = f["Are you comfortable working in an onsite setting?*"]
        self.assertEqual([o["text"] for o in g["options"]], ["Yes", "No"])
        self.assertTrue(g["required"])
        self.assertEqual(f["What is your Notice Period?"]["maxlength"], 20)   # only in "0 of 20 characters"

    def test_linkedin_2026_resume_step(self):
        from jobhunter.apply import browser as B
        p = self._li_step(2)                    # live: "radio-group-_r_28_", toast read as an error
        [g] = B.scan(p, flows.LI_MODAL)
        self.assertEqual((g["label"], g["value"]), ("Resume*", "Asha_K_R_Resume.pdf"))
        self.assertEqual(B.errors(p, flows.LI_MODAL), [])
        res = B.fill_form(p, JOB, self.resume, root=flows.LI_MODAL)
        self.assertTrue(res.fills[0].ok, res.fills[0].why)      # the chosen resume stays, no file-input crash

    def test_linkedin_2026_questions_step_passes_next(self):
        from jobhunter.apply import browser as B
        p = self._li_step(3)
        B.fill_form(p, JOB, self.resume, root=flows.LI_MODAL)
        self.assertTrue(p.is_checked("#_r_2j_"))                # onsite: Yes, from profile
        self.assertLessEqual(len(p.input_value("#_r_2m_")), 20)  # a profile answer that fits, never cut
        p.click("#next")
        self.assertTrue(p.is_visible("text=Review your application"))

    def test_redraw_mid_step_never_answers_an_unknown_question(self):
        from jobhunter.apply import browser as B
        p = self.page("linkedin_redraw")         # live: commute (no answer) ended up "Yes" after a re-render
        p.click("button[aria-label='Easy Apply to this job']")
        B.fill_form(p, JOB, self.resume, root=flows.LI_MODAL)
        self.assertEqual(p.evaluate("answers()"), {"onsite": "Yes"})
        p.evaluate("""() => { const r = document.querySelector('input[name="radio-group-commute"][value="Yes"]');
                             r.checked = true; }""")   # a stray tick, however it got there
        again = {f.field["label"]: f for f in B.fill_form(p, JOB, self.resume, root=flows.LI_MODAL).fills}
        c = again["Are you comfortable commuting to this job's location?*"]
        self.assertFalse(c.ok, "a pre-ticked radio was taken as your answer")
        self.assertEqual(c.answer.basis, "none")

    def test_linkedin_plain_text_errors_count(self):
        from jobhunter.apply import browser as B
        p = self.browser.new_page()                # live: bare <p>, no role/class/id
        p.set_content('<dialog data-testid="dialog" open><p>Select or upload a resume in DOC, DOCX, or PDF format</p>'
                      '<p>A resume is required</p><span>This field is required</span><p>Invalid input</p>'
                      '<p>0 of 20 characters</p></dialog>')
        self.assertEqual(sorted(B.errors(p, flows.LI_MODAL)),
                         ["A resume is required", "Invalid input", "This field is required"])

    def test_check_mode_never_presses_next_over_errors(self):
        from jobhunter.apply import panel as PN
        p = self.browser.new_page()
        p.set_content('<main><button aria-label="Easy Apply to this job" onclick="document.querySelector(\'dialog\')'
                      '.showModal()">Easy Apply</button></main><dialog data-testid="dialog"><p>A resume is required</p>'
                      '<button aria-label="Continue to next step" onclick="window.nexts=(window.nexts||0)+1">Next</button>'
                      '</dialog>')
        acts, msgs = iter(["next", "later"]), []
        old = flows.checking, PN.hand_over
        flows.checking = lambda: True
        PN.hand_over = lambda *a, msg="", **k: (msgs.append(msg), (next(acts), []))[1]
        try:
            flows.linkedin(p, self.db, JOB, None, None, dry=False)
        finally:
            flows.checking, PN.hand_over = old
        self.assertIsNone(p.evaluate("window.nexts"), "pressed Next while LinkedIn showed an error")
        self.assertTrue(all("A resume is required" in m for m in msgs), msgs)

    def test_stuck_page_counter_stops_instead_of_looping(self):
        p = self.browser.new_page()                # live: Next did nothing, "3/5 pages" stayed → "too many steps"
        p.set_content('<main><button aria-label="Easy Apply to this job" onclick="document.querySelector(\'dialog\')'
                      '.showModal()">Easy Apply</button></main><dialog data-testid="dialog"><span>3/5 pages</span>'
                      '<button aria-label="Continue to next step" onclick="window.nexts=(window.nexts||0)+1">Next</button>'
                      '</dialog>')
        o = flows.linkedin(p, self.db, JOB, self.resume, None, dry=True)
        self.assertEqual(p.evaluate("window.nexts"), 1, o.why)
        self.assertIn("3/5", o.why)

    def test_typing_reads_back_and_retries_once(self):
        from jobhunter.apply import browser as B
        p = self.browser.new_page()                # live: React ate keys mid-typing → "Ipommpediate"
        p.set_content('<input id="once"><input id="always"><script>let n = 0;'
                      'once.oninput = () => { if (n++ === 2) once.value += "mm"; };'
                      'always.oninput = () => { if (!always.value.endsWith("x")) always.value += "x"; };</script>')
        self.assertTrue(B._type(p, p.locator("#once"), "Immediately"))
        self.assertEqual(p.input_value("#once"), "Immediately")
        self.assertFalse(B._type(p, p.locator("#always"), "Immediately"))

    def test_numeric_notice_field_uses_days_or_asks(self):
        from jobhunter import config
        from jobhunter.apply import browser as B
        html = ('<dialog data-testid="dialog" open><label for="n">Notice period?</label>'
                '<input id="n" type="text" inputmode="numeric"></dialog>')   # live: a number field, type="text"
        p = self.browser.new_page()
        p.set_content(html)
        days = str(config.cfg("profile")["answers"]["notice_period_days"])
        B.fill_form(p, JOB, None, root=flows.LI_MODAL)
        self.assertEqual(p.input_value("#n"), days)
        config.cfg("profile")["answers"].pop("notice_period_days")
        try:
            p.set_content(html)
            res = B.fill_form(p, JOB, None, root=flows.LI_MODAL)
        finally:
            config.reload()
        self.assertEqual(p.input_value("#n"), "", "guessed a notice period")
        self.assertEqual(res.fills[0].answer.basis, "none")

    def test_easy_apply_click_lost_is_clicked_once_more(self):
        p = self.browser.new_page()                # live (NURY): first click did nothing, no window in 13 s
        p.set_content('<main><button aria-label="Easy Apply to this job" onclick="if (window.c = (window.c||0)+1, c > 1)'
                      ' document.querySelector(\'dialog\').showModal()">Easy Apply</button></main><dialog data-testid='
                      '"dialog"><button aria-label="Submit application">Submit application</button></dialog>')
        o = flows.linkedin(p, self.db, JOB, self.resume, None, dry=True)
        self.assertEqual(p.evaluate("window.c"), 2, o.why)
        self.assertIn("would", o.why)

    def test_slow_next_is_waited_for(self):
        p = self.browser.new_page()                # live (NURY): window open, buttons still loading
        p.set_content('<main><button aria-label="Easy Apply to this job" onclick="document.querySelector(\'dialog\')'
                      '.showModal(); setTimeout(() => document.querySelector(\'dialog\').innerHTML = \'<button aria-label='
                      '&quot;Submit application&quot;>Submit application</button>\', 3500)">Easy Apply</button></main>'
                      '<dialog data-testid="dialog"><p>Loading…</p></dialog>')
        o = flows.linkedin(p, self.db, JOB, self.resume, None, dry=True)
        self.assertNotIn("couldn't find Next", o.why)
        self.assertIn("would", o.why)

    def test_options_redrawn_mid_step_are_read_again(self):
        from jobhunter.apply import browser as B
        p = self.browser.new_page()                # live (Wedding Co 3/5): onsite read mid-redraw → "no option matches"
        p.set_content('<dialog data-testid="dialog" open><fieldset id="g"><legend>Are you comfortable working in an '
                      'onsite setting?</legend><input type="radio" name="o" id="y"><label for="y"></label>'
                      '<input type="radio" name="o" id="n"><label for="n"></label></fieldset></dialog><script>'
                      'setTimeout(() => g.innerHTML = g.innerHTML.replace(\'for="y"></label>\', \'for="y">Yes</label>\')'
                      '.replace(\'for="n"></label>\', \'for="n">No</label>\'), 400);</script>')
        res = B.fill_form(p, JOB, None, root=flows.LI_MODAL)
        self.assertTrue(res.fills[0].ok, res.fills[0].why)
        self.assertTrue(p.is_checked("#y"))

    def test_missing_window_never_fills_the_page_behind_it(self):
        from jobhunter.apply import browser as B
        p = self.page("basic_ats")              # live: Easy Apply didn't open → bot typed into LinkedIn's Search
        res = B.fill_form(p, JOB, self.resume, root=flows.LI_MODAL)
        self.assertEqual(res.fills, [])
        self.assertEqual(p.input_value("#first_name"), "")

    def test_dry_run_never_waits_when_next_is_missing(self):
        p = self.browser.new_page()
        p.set_content('<main><button aria-label="Easy Apply to this job" onclick="document.querySelector(\'dialog\')'
                      '.showModal()">Easy Apply</button></main><dialog data-testid="dialog"><label for="c">City</label>'
                      '<input id="c"></dialog>')
        n = len(self.waited)
        o = flows.linkedin(p, self.db, JOB, self.resume, None, dry=True)
        self.assertEqual(len(self.waited), n, "a dry run waited for you")
        self.assertEqual(o.status, "filled", o.why)

    def test_linkedin_already_applied(self):
        o = flows.linkedin(self.page("linkedin_applied"), self.db, JOB, self.resume, None, dry=False)
        self.assertEqual(o.status, "applied")
        self.assertIn("already applied", o.why)

    def test_fill_only_mode_never_submits(self):
        from jobhunter import config
        config.cfg("search")["apply"]["mode"] = "fill_only"
        try:
            p = self.page("basic_ats")
            o = flows.single_page(p, self.db, JOB, self.resume, None, "greenhouse", dry=False)
            self.assertEqual(o.status, "needs_you")
            self.assertNotIn("thank you", p.inner_text("body").lower())
        finally:
            config.reload()

    def test_workday_never_auto(self):
        from jobhunter.apply.browser import FormResult
        ok, why = flows.gate(self.db, "workday", JOB, FormResult(), [])
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
