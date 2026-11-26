"""LinkedIn page helpers for flows.py: the Easy Apply modal, its Apply / Next / Submit buttons, step changes,
the resume step, and following an external apply link. No submit decisions here (flows.gate owns those);
flows.py re-exports every name."""
from __future__ import annotations

import re
import time
from pathlib import Path

from ..util import console, log
from . import browser as B


# 2026 LinkedIn UI: a native <dialog data-testid="dialog" open>, hashed classes, no <form>
LI_MODAL = ('.jobs-easy-apply-modal, [data-test-modal-id="easy-apply-modal"], div[role="dialog"]:has(form), '
            'dialog[data-testid="dialog"][open]')


def _in(page, root: str, sel: str):
    """`sel` inside the first element matching `root` (root may be a comma list)."""
    return page.locator(root).first.locator(sel)


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


LI_STEP_BTN = re.compile(r"next|continue to next step|review|submit application", re.I)


def _li_submit(page):
    """LinkedIn's Submit button(s), found by name: the 2026 Review page's button has no aria-label."""
    return page.locator(LI_MODAL).first.get_by_role("button", name="Submit application")


def _li_ready(page, wait_ms: int = 5000) -> bool:
    """Wait (up to ~5 s) until the Easy Apply step has loaded its Next/Review/Submit button."""
    for _ in range(wait_ms // 500):
        try:
            b = page.locator(LI_MODAL).first.get_by_role("button", name=LI_STEP_BTN)
            if any(b.nth(i).is_visible() for i in range(b.count())):
                return True
        except Exception as err:
            log.debug("flows: polling for the next step button: %s", err)
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
            except Exception as err:
                log.debug("flows: new tab still loading: %s", err)
            return p2
        B.click_button(page, ["Continue to apply", "Continue", "Apply anyway"], root='div[role="dialog"]')
    return None
