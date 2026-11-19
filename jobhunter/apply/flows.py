"""Site flows: LinkedIn Easy Apply, Naukri, single-page ATS forms, anything else.

Each flow fills what it can and returns an Outcome. The flow itself decides
submit-or-wait with `gate()`: submit only when every field is basic, the site
is allowed, the daily caps aren't hit and the verifier passes.
"""
from __future__ import annotations

import re
import select
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..db import DB
from ..util import console
from . import browser as B
from . import fields as F
from . import panel as PN

# 2026 LinkedIn UI: a native <dialog data-testid="dialog" open>, hashed classes, no <form>
LI_MODAL = ('.jobs-easy-apply-modal, [data-test-modal-id="easy-apply-modal"], div[role="dialog"]:has(form), '
            'dialog[data-testid="dialog"][open]')


def _in(page, root: str, sel: str):
    """`sel` inside the first element matching `root` (root may be a comma list)."""
    return page.locator(root).first.locator(sel)


@dataclass
class Outcome:
    status: str                    # applied | needs_you | skipped | failed | filled(dry)
    site: str
    auto: bool = False
    why: str = ""
    missing: list[str] = field(default_factory=list)
    learned: int = 0


def site_of(url: str) -> str:
    u = (url or "").lower()
    for key, site in (("linkedin.com", "linkedin"), ("naukri.com", "naukri"), ("greenhouse.io", "greenhouse"),
                      ("lever.co", "lever"), ("ashbyhq.com", "ashby"), ("myworkdayjobs.com", "workday"),
                      ("indeed.", "indeed"), ("internshala.com", "internshala"), (".gov.in", "govt"),
                      (".nic.in", "govt")):
        if key in u:
            return site
    return "generic"


SUBMIT_NAMES = ["Submit application", "Submit Application", "Submit", "Send application", "Apply"]


def checking() -> bool:
    """Check mode: the bot fills, you read the real form, fix anything, and press Submit.
    Off for dry runs, hidden browsers, and 'submit now' approvals (from Telegram / Review Center)."""
    mode = config.cfg("search").get("apply", {}).get("mode", "check")
    return (mode == "check" or (APPROVED and not SUBMIT_NOW)) and VISIBLE and not SUBMIT_NOW


def gate(db: DB, site: str, job: dict, res: B.FormResult, problems: list[str]) -> tuple[bool, str]:
    s = config.cfg("search").get("apply", {})
    if site in s.get("never_auto_submit_on", []):
        return False, f"{site}: you always submit this site yourself"
    if checking():
        return False, "you read the form and press Submit"
    if APPROVED:      # you reviewed the values (Review Center / Telegram) and said submit
        if res.blockers:
            return False, "; ".join(res.blockers)
        if res.missing:
            return False, "still missing: " + ", ".join((m.field["label"] or m.answer.key)[:40] for m in res.missing[:6])
        if problems:
            return False, "verify failed: " + "; ".join(problems[:4])
        return True, "approved by you in the Review Center"
    if s.get("mode", "basic_auto") != "basic_auto":
        return False, f"mode is {s.get('mode')}: waiting for your approval"
    if site not in s.get("sites_allowed_auto", []):
        return False, f"{site}: never auto-submitted"
    if db.applied_today() >= s.get("max_auto_submits_per_day", 25):
        return False, "daily auto-submit cap reached"
    cap = (s.get("max_per_site_per_day") or {}).get(site)
    if cap is not None and db.applied_today(site) >= cap:
        return False, f"{site} daily cap reached"
    if job.get("score", 0) < s.get("min_score_for_auto", 45) and not job.get("dream"):
        return False, f"score {job.get('score')} < auto minimum"
    if res.blockers:
        return False, "; ".join(res.blockers)
    if res.missing:
        return False, "needs you: " + ", ".join((m.field["label"] or m.answer.key)[:40] for m in res.missing[:6])
    if res.non_basic_filled:
        return False, "drafted answers to review: " + ", ".join(m.field["label"][:30] for m in res.non_basic_filled)
    if problems:
        return False, "verify failed: " + "; ".join(problems[:4])
    return True, "every field is a basic fact you gave"


def _stdin_ready() -> bool:
    try:
        return bool(select.select([sys.stdin], [], [], 0)[0])
    except (OSError, ValueError):
        return False


# Set by the runner: True when you approved this job (Review Center / Telegram).
APPROVED = False
SUBMIT_NOW = False     # approved with "submit now": no check panel, submit if nothing is missing
VISIBLE = True         # False when the browser is headless (nobody can check a hidden window)
TITLE = ""             # "role · company" shown on the check panel


def wait_for_you(page, res: B.FormResult | None, site: str, root: str | None = None) -> tuple[bool, int]:
    """Hand a form over to you. In 'queue' mode (default) the bot records the form for the
    Review Center and moves on; in 'wait' mode, or after you approved, it waits in the browser."""
    B.snap(page)
    if checking():
        act, saved = PN.hand_over(page, res.fills if res else [], site, TITLE, root, final=True,
                                  msg="Finish anything the bot couldn't do, then press the site's own Submit.")
        PN.say_learned(saved)
        if act == "submit":
            PN.hide(page)
            page.wait_for_timeout(2500)
        return act == "submitted" or (act == "submit" and B.succeeded(page)), len(saved)
    if config.cfg("search").get("apply", {}).get("on_needs_you", "queue") == "queue" and not APPROVED:
        return False, 0
    return _wait_in_browser(page, res, site, root)


def check_form(page, db: DB, job: dict, res: B.FormResult, site: str, resume: Path | None,
               cover: str | None) -> Outcome:
    """Check mode for one-page and multi-page forms: you read, fix and submit; the bot learns."""
    fills, saved_all, msg = list(res.fills), [], ""
    for _ in range(12):
        act, saved = PN.hand_over(page, fills, site, TITLE, final=True, msg=msg)
        saved_all += saved
        PN.say_learned(saved)
        if act == "submitted":
            return Outcome("applied", site, why="you submitted it", learned=len(saved_all))
        if act == "moved":                       # next page of a multi-page form: fill it too
            page.wait_for_timeout(1500)
            nxt = B.fill_form(page, job, resume, cover_text=cover)
            _report(nxt)
            fills, msg = nxt.fills, ""
            continue
        if act == "submit":
            PN.hide(page)
            B.snap(page)                         # the form exactly as you approved it
            if not B.click_button(page, SUBMIT_NAMES):
                msg = "I can't find this site's Submit button. Press it yourself; I'll notice."
                continue
            page.wait_for_timeout(4000)
            if B.succeeded(page) or not B.scan(page):
                return Outcome("applied", site, why="submitted after your check", learned=len(saved_all))
            errs = B.errors(page)
            msg = ("The site says: " + "; ".join(errs[:3])) if errs else \
                "Pressed Submit but saw no confirmation. Check the page, then press Submit again or Later."
            continue
        if act == "skip":
            return Outcome("skipped", site, why="you skipped it", learned=len(saved_all))
        why = {"later": "you chose Later", "timeout": "no answer in time", "close": "preview closed"}.get(
            act, "the form was closed before it was sent")
        return Outcome("needs_you", site, why=why, learned=len(saved_all),
                       missing=[m.field["label"] for m in res.missing])
    return Outcome("needs_you", site, why="too many tries", learned=len(saved_all))


def _wait_in_browser(page, res: B.FormResult | None, site: str, root: str | None = None) -> tuple[bool, int]:
    """Wait while you finish and click Submit. Learns the answers you type.
    Returns (submitted, answers_learned). Press Enter in the terminal to stop waiting."""
    limit = config.cfg("search").get("apply", {}).get("wait_for_you_sec", 600)
    console.print(f"[bold yellow]→ Your turn in the browser.[/] Finish the highlighted fields and click Submit. "
                  f"I'll notice. (Enter here = skip, {limit // 60} min max)")
    try:
        page.evaluate("""() => document.querySelectorAll('[data-jh]').forEach(e => {
            if (!e.value && e.type !== 'checkbox' && e.type !== 'radio' && e.type !== 'file')
              e.style.outline = '3px solid #f5a623'; })""")
    except Exception:
        pass
    last: dict[str, dict] = {}
    t0 = time.time()
    while time.time() - t0 < limit:
        if _stdin_ready():
            sys.stdin.readline()
            return False, 0
        try:
            if page.is_closed():
                break
            if B.succeeded(page):
                return True, _learn(res, last, site)
            for f in B.scan(page, root if root and page.locator(root).count() else None):
                last[f["jh"]] = f
        except Exception:
            # navigation in progress: the form may just have been submitted
            time.sleep(1.5)
            try:
                if B.succeeded(page):
                    return True, _learn(res, last, site)
            except Exception:
                pass
        time.sleep(1.5)
    return False, 0


def _learn(res: B.FormResult | None, last: dict[str, dict], site: str) -> int:
    """Answers you typed for fields we couldn't answer become 'learned' facts."""
    if not res:
        return 0
    n = 0
    for fl in res.fills:
        if fl.ok and fl.answer.basic:
            continue
        if fl.answer.key in ("cover_letter", "resume", "cover_letter_file", "captcha", "otp", "diversity", "expected_ctc"):
            continue
        cur = last.get(fl.field["jh"])
        v = (cur or {}).get("value")
        if v not in (None, "", False):
            F.learn(fl.field["label"], v, site)
            n += 1
    return n


def _report(res: B.FormResult) -> None:
    for fl in res.fills:
        mark = "[green]✓[/]" if fl.ok and fl.answer.basic else ("[yellow]~[/]" if fl.ok else "[red]·[/]")
        req = "*" if fl.field.get("required") else " "
        console.print(f"   {mark}{req} {fl.field['label'][:46]:46} {str(fl.chosen if fl.ok else fl.why)[:40]}")


# ── single-page forms: Greenhouse, Lever, Ashby, generic career pages ──────
def single_page(page, db: DB, job: dict, resume: Path | None, cover: str | None, site: str, dry: bool) -> Outcome:
    if site == "lever" and "/apply" not in page.url:
        B.click_button(page, ["Apply for this job", "Apply"], dry=False)
        page.wait_for_load_state("domcontentloaded")
    if site in ("greenhouse", "ashby", "generic"):
        if not B.scan(page):
            B.click_button(page, ["Apply for this job", "Apply Now", "Apply now", "Apply"], dry=False)
            page.wait_for_timeout(2000)
    res = B.fill_form(page, job, resume, cover_text=cover)
    _report(res)
    if not res.fills:
        if checking() and not dry:
            return check_form(page, db, job, res, site, resume, cover)
        return Outcome("needs_you", site, why="no application form on this page (it may be a listing that links elsewhere)")
    problems = B.verify(page, res)
    ok, why = gate(db, site, job, res, problems)
    B.snap(page)
    if checking() and not dry:
        return check_form(page, db, job, res, site, resume, cover)
    if dry:
        return Outcome("filled", site, why=f"dry run · would {'AUTO-SUBMIT' if ok else 'wait'}: {why}",
                       missing=[m.field["label"] for m in res.missing])
    if ok and B.click_button(page, SUBMIT_NAMES):
        page.wait_for_timeout(4000)
        if B.succeeded(page) or not B.scan(page):
            return Outcome("applied", site, auto=True, why=why)
        problems = B.errors(page)
        why = "clicked submit but no confirmation: " + "; ".join(problems[:3])
    submitted, learned = wait_for_you(page, res, site)
    return Outcome("applied" if submitted else "needs_you", site, why=why, learned=learned,
                   missing=[m.field["label"] for m in res.missing])


# ── LinkedIn Easy Apply ────────────────────────────────────────────────────
APPLY_NAME = re.compile(r"^\s*(easy apply|apply)\b", re.I)
NOT_APPLY = re.compile(r"filter|alert|save|similar|sign in|join", re.I)
ALREADY = re.compile(r"\bapplied (?:\d+|an?) (?:second|minute|hour|day|week|month)s? ago|application submitted|"
                     r"you applied on|application was sent", re.I)
CLOSED = re.compile(r"no longer accepting applications|this job is no longer available|job has expired", re.I)
WHAT_JS = r"""
() => [...document.querySelectorAll('main button, main a, button, a[role=button]')].filter(e => {
  const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.top < innerHeight * 1.5; })
  .map(e => ((e.getAttribute('aria-label') || e.innerText || '').replace(/\s+/g, ' ').trim()).slice(0, 50))
  .filter((t, i, a) => t && a.indexOf(t) === i).slice(0, 30)
"""


def apply_button(page, wait_s: int = 15):
    """LinkedIn's Easy Apply / Apply control, found the way a person finds it: by its name.
    (Button or link, any class name, re-checked while the page finishes loading.)"""
    t0 = time.time()
    while time.time() - t0 < wait_s:
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=APPLY_NAME)
            try:
                n = min(loc.count(), 8)
            except Exception:
                n = 0
            for i in range(n):
                b = loc.nth(i)
                try:
                    name = b.get_attribute("aria-label") or b.inner_text() or ""
                    if b.is_visible() and not NOT_APPLY.search(name):
                        return b, name
                except Exception:
                    continue
        t = B.page_text(page)
        if ALREADY.search(t) or CLOSED.search(t):
            return None, ""
        page.wait_for_timeout(800)
    return None, ""


def linkedin(page, db: DB, job: dict, resume: Path | None, cover: str | None, dry: bool) -> Outcome:
    if "login" in page.url or "authwall" in page.url or "signup" in page.url:
        return Outcome("failed", "linkedin", why="not logged in: run `hunt login linkedin` once")
    btn, label = apply_button(page)
    if btn is None:
        t = B.page_text(page)
        if ALREADY.search(t):
            return Outcome("applied", "linkedin", why="you already applied to this job on LinkedIn")
        if CLOSED.search(t):
            return Outcome("skipped", "linkedin", why="LinkedIn says it no longer accepts applications")
        if "sign in" in t[:3000] and "join now" in t[:3000]:
            return Outcome("failed", "linkedin", why="LinkedIn shows you as logged out: run `hunt login linkedin`")
        try:
            seen = page.evaluate(WHAT_JS)
        except Exception:
            seen = []
        console.print("   [dim]buttons on the page: " + " | ".join(seen) + "[/]")
        return Outcome("needs_you", "linkedin", why="couldn't find LinkedIn's Apply button (buttons seen: "
                       + ", ".join(seen[:12]) + "). Open it with `hunt browser` and press 🎯")
    label = label.lower()
    console.print(f"   LinkedIn button: {label[:60]!r}")
    if "easy apply" not in label:
        p2 = follow_external(page, btn)
        if p2 is None:
            return Outcome("needs_you", "linkedin", why="LinkedIn's Apply button should open the company's site, "
                           "but nothing opened in 25 s. Open the job and press Apply yourself.")
        site = site_of(p2.url)
        console.print(f"   company site → {site}: {p2.url[:80]}")
        return single_page(p2, db, job, resume, cover, site, dry)
    for wait_ms in (5000, 10000):            # live: the first click is sometimes lost: click once more
        try:
            if not page.locator(LI_MODAL).filter(visible=True).count():   # it may have opened just after the first wait
                btn.click(timeout=5000)
            page.wait_for_selector(LI_MODAL, timeout=wait_ms)
            break
        except Exception:
            pass
    else:
        return Outcome("needs_you", "linkedin", why="pressed Easy Apply twice but its window didn't open")
    all_fills = B.FormResult()
    if checking() and not dry:
        return _linkedin_check(page, db, job, resume, cover, all_fills)
    for step in range(15):
        page.wait_for_timeout(1200)
        _li_ready(page)
        # untick "follow company" (it's on by default)
        try:
            fc = _in(page, LI_MODAL, "input#follow-company-checkbox")
            if fc.count() and fc.is_checked():
                _in(page, LI_MODAL, "label[for='follow-company-checkbox']").click()
        except Exception:
            pass
        _li_resume(page, resume)
        res = B.fill_form(page, job, resume, root=LI_MODAL, cover_text=cover)
        all_fills.fills += res.fills
        all_fills.blockers += res.blockers
        _report(res)
        problems = B.verify(page, res, LI_MODAL)          # includes errors the page shows
        sub = _li_submit(page)
        is_last = any(sub.nth(i).is_visible() for i in range(sub.count()))
        if res.missing or problems or res.blockers or (res.non_basic_filled and not APPROVED):
            ok, why = gate(db, "linkedin", job, res, problems)
            B.snap(page)
            if dry:
                return Outcome("filled", "linkedin", why=f"dry run · step {step + 1}: would wait: {why}")
            submitted, learned = wait_for_you(page, all_fills, "linkedin", LI_MODAL)
            return Outcome("applied" if submitted else "needs_you", "linkedin", why=why, learned=learned,
                           missing=[m.field["label"] for m in res.missing])
        if is_last:
            ok, why = gate(db, "linkedin", job, all_fills, problems)
            B.snap(page)
            if dry:
                return Outcome("filled", "linkedin", why=f"dry run · would {'AUTO-SUBMIT' if ok else 'wait'}: {why}")
            if ok and B.click_button(page, ["Submit application"], root=LI_MODAL):
                page.wait_for_timeout(4000)
                if B.succeeded(page) or not _li_submit(page).count():
                    B.click_button(page, ["Dismiss", "Done"], dry=False)
                    return Outcome("applied", "linkedin", auto=True, why=why)
                why = "clicked Submit but LinkedIn showed no confirmation"
            submitted, learned = wait_for_you(page, all_fills, "linkedin", LI_MODAL)
            return Outcome("applied" if submitted else "needs_you", "linkedin", why=why, learned=learned)
        # errors are read before each Next (above), not after: the next step may open with its own
        before = _li_pages(page)
        why = ""
        if not B.click_button(page, ["Continue to next step", "Next", "Review your application", "Review"], root=LI_MODAL):
            why = "couldn't find Next"
        elif before and not _li_moved(page, before):
            why = f"pressed Next but LinkedIn stayed on page {before}"
        if why:
            B.snap(page)
            if dry:
                return Outcome("filled", "linkedin", why=f"dry run · step {step + 1}: would wait: {why}")
            submitted, learned = wait_for_you(page, all_fills, "linkedin", LI_MODAL)
            return Outcome("applied" if submitted else "needs_you", "linkedin", why=why, learned=learned)
    return Outcome("needs_you", "linkedin", why="too many steps")


LI_STEP_BTN = re.compile(r"next|continue to next step|review|submit application", re.I)


def _li_ready(page, wait_ms: int = 5000) -> bool:
    """Wait (up to ~5 s) until the Easy Apply step has loaded its Next/Review/Submit button."""
    for _ in range(wait_ms // 500):
        try:
            b = page.locator(LI_MODAL).first.get_by_role("button", name=LI_STEP_BTN)
            if any(b.nth(i).is_visible() for i in range(b.count())):
                return True
def _li_submit(page):
    """LinkedIn's Submit button(s), found by name: the 2026 Review page's button has no aria-label."""
    return page.locator(LI_MODAL).first.get_by_role("button", name="Submit application")


        except Exception:
            pass
        page.wait_for_timeout(500)
    return False


def _li_pages(page) -> str:
    """LinkedIn's "N/M pages" counter in the Easy Apply window, e.g. "3/5" ("" if it has none)."""
    try:
        m = re.search(r"\b(\d+\s*/\s*\d+)\s*pages?", page.locator(LI_MODAL).first.inner_text(timeout=2000))
    except Exception:
        return ""
    return re.sub(r"\s", "", m.group(1)) if m else ""


def _li_moved(page, before: str, wait_ms: int = 4000) -> bool:
    """True once the page counter leaves `before` (or the window closes / loses its counter)."""
    for _ in range(wait_ms // 500):
        page.wait_for_timeout(500)
        if _li_pages(page) != before:
            return True
    return False


def _li_resume(page, resume: Path | None) -> bool:
    """New Easy Apply resume step: no <input type=file> until "Upload resume" opens the file picker.
    Uploads your own resume when none is selected yet."""
    d = page.locator(LI_MODAL).first
    up = d.get_by_role("button", name=re.compile(r"^\s*upload resume", re.I))
    try:
        if not resume or not up.count() or not up.first.is_visible() or \
                d.locator("[role=radiogroup] input:checked, [role=radiogroup] [aria-checked=true]").count():
            return False
        with page.expect_file_chooser(timeout=8000) as fc:
            up.first.click()
        fc.value.set_files(str(resume))
        page.wait_for_timeout(2500)
        console.print(f"   resume uploaded: {resume.name}")
        return True
    except Exception as exc:
        console.print(f"   [yellow]resume upload failed: {exc}[/]")
        return False


def follow_external(page, btn, wait_s: int = 25):
    """Press an 'Apply on company site' button and return the page it opens: a new tab, the same tab,
    or after LinkedIn's 'Continue to apply' / 'Share your profile?' dialog."""
    ctx = page.context
    before, url0 = set(ctx.pages), page.url
    btn.click()
    t0 = time.time()
    while time.time() - t0 < wait_s:
        page.wait_for_timeout(700)
        new = [p for p in ctx.pages if p not in before]
        if new or (page.url != url0 and "linkedin.com" not in page.url):
            p2 = new[-1] if new else page
            try:
                p2.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass
            return p2
        B.click_button(page, ["Continue to apply", "Continue", "Apply anyway"], root='div[role="dialog"]')
    return None


def _linkedin_check(page, db: DB, job: dict, resume: Path | None, cover: str | None, all_fills: B.FormResult) -> Outcome:
    """Easy Apply in check mode: every step is filled, shown on the panel, and moves on only when you say."""
    seen: set[str] = set()
    saved_all: list[dict] = []
    msg = ""
    for _ in range(25):
        page.wait_for_timeout(1200)
        _li_ready(page)
        if not page.locator(LI_MODAL).count():
            return Outcome("applied" if B.succeeded(page) else "needs_you", "linkedin",
                           why="sent" if B.succeeded(page) else "the Easy Apply window was closed",
                           learned=len(saved_all))
        try:
            fc = _in(page, LI_MODAL, "input#follow-company-checkbox")
            if fc.count() and fc.is_checked():
                _in(page, LI_MODAL, "label[for='follow-company-checkbox']").click()
        except Exception:
            pass
        _li_resume(page, resume)
        res = B.fill_form(page, job, resume, root=LI_MODAL, cover_text=cover, skip_jh=seen)
        seen |= {f.field["jh"] for f in res.fills}
        all_fills.fills += res.fills
        _report(res)
        here = {f["jh"] for f in B.scan(page, LI_MODAL)}
        step = [f for f in all_fills.fills if f.field["jh"] in here]
        sub = _li_submit(page)
        is_last = any(sub.nth(i).is_visible() for i in range(sub.count()))
        errs = B.errors(page, LI_MODAL)
        msg = msg or ("LinkedIn says: " + "; ".join(errs[:3]) if errs else "")
        B.snap(page)
        act, saved = PN.hand_over(page, step, "linkedin", TITLE, root=LI_MODAL, final=is_last, msg=msg)
        saved_all += saved
        PN.say_learned(saved)
        msg = ""
        if act == "submitted":
            B.click_button(page, ["Dismiss", "Done"], dry=False)
            return Outcome("applied", "linkedin", why="you submitted it", learned=len(saved_all))
        if act == "moved":
            continue
        if act in ("next", "submit"):
            PN.hide(page)
        if act == "next":
            errs = B.errors(page, LI_MODAL)
            if errs:                     # never press Next over an error LinkedIn shows: you fix it first
                msg = "Fix first. LinkedIn says: " + "; ".join(errs[:3])
                continue
            before = _li_pages(page)
            if not B.click_button(page, ["Continue to next step", "Next", "Review your application", "Review"], root=LI_MODAL):
                msg = "I can't find Next: press LinkedIn's own Next button."
            elif before and not _li_moved(page, before):
                msg = f"Pressed Next but LinkedIn stayed on page {before}: check the window."
            page.wait_for_timeout(1000)
            errs = B.errors(page, LI_MODAL)
            if errs:
                msg = "LinkedIn says: " + "; ".join(errs[:3])
            continue
        if act == "submit":
            if B.click_button(page, ["Submit application"], root=LI_MODAL):
                page.wait_for_timeout(4000)
                if B.succeeded(page) or not _li_submit(page).count():
                    B.click_button(page, ["Dismiss", "Done"], dry=False)
                    return Outcome("applied", "linkedin", why="submitted after your check", learned=len(saved_all))
            errs = B.errors(page, LI_MODAL)
            msg = ("LinkedIn says: " + "; ".join(errs[:3])) if errs else "No confirmation yet: check the window."
            continue
        if act == "skip":
            return Outcome("skipped", "linkedin", why="you skipped it", learned=len(saved_all))
        return Outcome("needs_you", "linkedin", learned=len(saved_all),
                       why={"later": "you chose Later", "timeout": "no answer in time"}.get(act, "closed before sending"))
    return Outcome("needs_you", "linkedin", why="too many steps", learned=len(saved_all))


# ── Naukri ─────────────────────────────────────────────────────────────────
NAUKRI_BOT = ".chatbot_DrawerContentWrapper, [class*='chatbot_Drawer'], .chatbot_MessageContainer"


def naukri(page, db: DB, job: dict, resume: Path | None, cover: str | None, dry: bool) -> Outcome:
    page.wait_for_timeout(2500)
    t = B.page_text(page)
    if "already applied" in t or page.locator("#already-applied, .already-applied").count():
        return Outcome("applied", "naukri", why="already applied")
    ext = page.locator("#company-site-button, button:has-text('Apply on company site')").first
    if ext.count() and ext.is_visible():
        p2 = follow_external(page, ext)
        if p2 is None:
            return Outcome("needs_you", "naukri", why="'Apply on company site' opened nothing in 25 s")
        return single_page(p2, db, job, resume, cover, site_of(p2.url), dry)
    btn = page.locator("#apply-button, button.apply-button, button:has-text('Apply')").first
    if not btn.count():
        return Outcome("skipped", "naukri", why="no Apply button")
    if "login" in page.url or page.locator("text=Login to apply").count():
        return Outcome("failed", "naukri", why="not logged in: run `hunt login naukri` once")
    if dry:
        return Outcome("filled", "naukri", why="dry run · Naukri one-click apply uses your Naukri profile (basic)")
    if checking():
        act, _ = PN.hand_over(page, [], "naukri", TITLE, final=True,
                              msg="Naukri sends your Naukri profile and resume. Check them on Naukri, then press Submit.")
        if act == "submitted":
            return Outcome("applied", "naukri", why="you applied on Naukri")
        if act != "submit":
            return Outcome("skipped" if act == "skip" else "needs_you", "naukri", why=f"you chose {act}")
        ok, why = True, "you checked it"
    else:
        ok, why = gate(db, "naukri", job, B.FormResult(), [])
    if not ok:
        submitted, learned = wait_for_you(page, None, "naukri")
        return Outcome("applied" if submitted else "needs_you", "naukri", why=why)
    btn.click()
    page.wait_for_timeout(3500)
    # Naukri may open a recruiter questionnaire in a chat drawer
    for _ in range(12):
        if B.succeeded(page) or "applied" in (btn.inner_text() or "").lower():
            return Outcome("applied", "naukri", auto=not checking(), why="one-click apply with your Naukri profile")
        if not page.locator(NAUKRI_BOT).count():
            page.wait_for_timeout(1500)
            continue
        q = _in(page, NAUKRI_BOT, ".botMsg, [class*='botItem']").last
        question = (q.inner_text() if q.count() else "").strip()
        radios = _in(page, NAUKRI_BOT, ".ssrc__radio-btn-container label, [class*='chip'] label")
        opts = [radios.nth(i).inner_text().strip() for i in range(radios.count())]
        a = F.answer(question, "radio" if opts else "text", "", opts, job)
        if not a.basic:
            submitted, learned = wait_for_you(page, None, "naukri")
            return Outcome("applied" if submitted else "needs_you", "naukri", why=f"recruiter question: {question[:60]}")
        if opts:
            choice = F.pick_option(a.value, opts)
            if not choice:
                submitted, _ = wait_for_you(page, None, "naukri")
                return Outcome("applied" if submitted else "needs_you", "naukri", why=f"no option for: {question[:60]}")
            radios.nth(opts.index(choice)).click()
        else:
            box = _in(page, NAUKRI_BOT, "[contenteditable='true'], input[type=text]").last
            box.click()
            box.press_sequentially(str(a.value), delay=40)
        B.human_pause(0.5, 1.0)
        send = _in(page, NAUKRI_BOT, ".sendMsg, button:has-text('Save'), [class*='send']").last
        if send.count():
            send.click()
        page.wait_for_timeout(2000)
    submitted, _ = wait_for_you(page, None, "naukri")
    return Outcome("applied" if submitted else "needs_you", "naukri", why="questionnaire did not finish")


def run(page, db: DB, job: dict, resume: Path | None, cover: str | None, dry: bool) -> Outcome:
    url = job["url"]
    # prefer a link we can apply on: LinkedIn Easy Apply > company ATS > Naukri > first
    for u in job.get("urls") or []:
        if site_of(u) in ("greenhouse", "lever", "ashby"):
            url = u
            break
    site = site_of(url)
    if site == "govt":
        return Outcome("skipped", "govt", why="govt portals need OTR/OTP/fee: apply yourself from the alert")
    global TITLE
    TITLE = f"{job.get('title', '')} · {job.get('company', '')}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:
        if "Timeout" not in type(exc).__name__ or page.url in ("about:blank", ""):
            raise
        console.print("   [dim]page still loading after 45 s: working with what's there[/]")
    page.wait_for_timeout(1500)
    if site == "linkedin":
        return linkedin(page, db, job, resume, cover, dry)
    if site == "naukri":
        return naukri(page, db, job, resume, cover, dry)
    if site in ("workday", "indeed", "internshala"):
        res = B.fill_form(page, job, resume, cover_text=cover)
        _report(res)
        if dry:
            return Outcome("filled", site, why=f"dry run · {site} always waits for you")
        if checking():
            return check_form(page, db, job, res, site, resume, cover)
        submitted, learned = wait_for_you(page, res, site)
        return Outcome("applied" if submitted else "needs_you", site, why=f"{site}: you submit", learned=learned)
    return single_page(page, db, job, resume, cover, site, dry)
