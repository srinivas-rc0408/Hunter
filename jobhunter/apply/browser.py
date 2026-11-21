"""Playwright helpers: open your logged-in browser, read any form, fill it, verify.

The form reader is site-agnostic. It finds every visible input/select/textarea,
works out its label the way a person would (label[for], wrapping label,
aria-labelledby, aria-label, fieldset legend, nearby text, placeholder) and
tags it with data-jh so it can be filled and read back reliably.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from . import fields as F

# Sites (LinkedIn's Easy Apply since 2025) can draw forms inside open shadow roots, where
# document.querySelector can't see them. These helpers search the page AND every open shadow root.
DEEP_JS = r"""
  const deepAll = (root, sel) => { const out = [...root.querySelectorAll(sel)];
    const walk = n => { for (const e of n.querySelectorAll('*')) if (e.shadowRoot) {
      out.push(...e.shadowRoot.querySelectorAll(sel)); walk(e.shadowRoot); } };
    walk(root); return out; };
  const deepOne = sel => { try { return deepAll(document, sel)[0] || null; } catch (_) { return null; } };
"""

SCAN_JS = r"""
(rootSel) => {
""" + DEEP_JS + r"""
  const root = rootSel ? deepOne(rootSel) : document;      // asked-for window missing: touch nothing
  if (!root) return [];
  const shown = el => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const vis = el => {
    if (shown(el)) return true;
    if (el.type === 'radio' || el.type === 'checkbox') {      // styled inputs: the label is what shows
      const l = el.closest('label') || (el.id && el.getRootNode().querySelector && el.getRootNode().querySelector(`label[for="${CSS.escape(el.id)}"]`));
      return shown(l);
    }
    if (el.type === 'file') return shown(el.parentElement) || shown(el.closest('div, label, form'));
    return false;
  };
  const txt = n => (n ? (n.innerText || n.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const R = el => { const r = el.getRootNode(); return r && r.querySelector ? r : document; };
  const byId = (el, i) => (R(el).getElementById ? R(el).getElementById(i) : null) || document.getElementById(i);
  const labelOf = el => {
    let t = '';
    if (el.id) { const l = R(el).querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = txt(l); }
    if (!t && el.getAttribute('aria-labelledby'))
      t = el.getAttribute('aria-labelledby').split(/\s+/).map(i => txt(byId(el, i))).join(' ');
    const al = el.getAttribute('aria-label') || '';    // LinkedIn 2026 may put the QUESTION in a radio's aria-label
    const q = s => s.replace(/[\s*]+/g, ' ').trim();
    if (!t && al && !(el.type === 'radio' && q(al) === q(groupLabel(el)))) t = al;
    if (!t) { const l = el.closest('label'); if (l) t = txt(l); }
    if (!t && (el.type === 'radio' || el.type === 'checkbox')) {     // LinkedIn 2026: empty <label for>, text beside it
      const w = el.closest('[role="radio"], [role="checkbox"]'); if (w) t = txt(w) || w.getAttribute('aria-label') || '';
    }
    if (!t && el.type === 'radio')     // the option's text beside it: the nearest box holding only this input
      for (let c = el.parentElement, i = 0; c && i < 5 && !t && c.querySelectorAll('input').length === 1; c = c.parentElement, i++)
        if (txt(c).length < 120) t = txt(c);
    if (!t) {
      let c = el.parentElement;
      for (let i = 0; i < 5 && c && !t; i++) {
        const cand = c.querySelector('label, legend, [class*="label"], [class*="question"], [class*="title"]');
        if (cand && !cand.contains(el) && txt(cand).length < 300) t = txt(cand);
        c = c.parentElement;
      }
    }
    return (t || el.placeholder || el.name || '').slice(0, 300);
  };
  const groupLabel = el => {
    const fs = el.closest('fieldset, [role="radiogroup"], [role="group"]');
    if (fs) {
      const lg = fs.querySelector('legend'); if (lg && txt(lg)) return txt(lg);
      if (fs.getAttribute('aria-label')) return fs.getAttribute('aria-label');
      const lb = fs.getAttribute('aria-labelledby');
      if (lb) return lb.split(/\s+/).map(i => txt(byId(el, i))).join(' ');
      for (let n = fs, i = 0; i < 3 && n; i++, n = n.parentElement) {   // LinkedIn 2026: <p>question*</p><fieldset>
        const prev = n.previousElementSibling;
        if (prev && txt(prev)) { const q = prev.matches('p, label, legend, h3, h4') ? prev : prev.querySelector('p, label, legend, h3, h4');
                                 return txt(q || prev).slice(0, 300); }
      }
    }
    let c = el.parentElement;
    for (let i = 0; i < 6 && c; i++) {
      const radios = c.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`);
      if (radios.length > 1) {
        const cands = [...c.querySelectorAll('legend, label, p, span, div, h3, h4, h5')].filter(x =>
          !x.querySelector('input') && !(x.closest('label') && x.closest('label').querySelector('input')) && txt(x));
        if (cands.length) return txt(cands[0]).slice(0, 300);
      }
      c = c.parentElement;
    }
    return el.name || '';
  };
  const out = []; const groups = {}; window.__jhN = window.__jhN || 0;
  const els = deepAll(root, 'input, select, textarea, [contenteditable="true"]');
  for (const el of els) {
    const type = (el.tagName === 'SELECT') ? 'select' : (el.tagName === 'TEXTAREA') ? 'textarea'
               : el.isContentEditable && el.tagName !== 'INPUT' ? 'textarea' : (el.type || 'text').toLowerCase();
    if (['hidden','submit','button','image','reset','search'].includes(type)) continue;
    if (el.disabled || el.readOnly && type !== 'file') continue;
    if (!vis(el)) continue;
    const jh = el.getAttribute('data-jh') || String(window.__jhN++);
    el.setAttribute('data-jh', jh);
    const required = !!(el.required || el.getAttribute('aria-required') === 'true');
    if (type === 'radio') {
      const g = el.name || jh;
      if (!groups[g]) { groups[g] = {jh: 'g:' + g, tag: 'radio', type: 'radio', name: g, label: groupLabel(el),
                        required: false, options: [], value: null, placeholder: ''}; out.push(groups[g]); }
      const o = {jh, text: labelOf(el) || el.value, value: el.value, checked: el.checked};
      groups[g].options.push(o); groups[g].required = groups[g].required || required || /\*\s*$/.test(groups[g].label);
      if (el.checked) groups[g].value = o.text;
      continue;
    }
    const f = {jh, tag: el.tagName.toLowerCase(), type, name: el.name || '', label: labelOf(el),
               required, placeholder: el.placeholder || '', value: null, options: [],
               combo: el.getAttribute('role') === 'combobox' || !!el.getAttribute('aria-autocomplete') ||
                      /select__input|autocomplete|typeahead/i.test(el.className || ''),
               maxlength: el.maxLength > 0 ? el.maxLength : null};
    const help = (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean).map(i => txt(byId(el, i))).join(' ');
    if (!f.maxlength) { const m = help.match(/\d+\s*(?:of|\/)\s*(\d+)\s*char/i); if (m) f.maxlength = +m[1]; }
    // LinkedIn number questions are type="text": tell them by inputmode, a "numeric" id, or the helper text
    if (type === 'text' && (/numeric|decimal/.test(el.inputMode) || /numeric/i.test(el.id + ' ' + el.name) ||
                            /whole number|decimal number|enter a number/i.test(help))) f.type = 'number';
    if (type === 'select') { f.options = [...el.options].map(o => ({text: o.text.trim(), value: o.value}));
                             f.value = el.selectedIndex > 0 ? el.options[el.selectedIndex].text.trim() : null; }
    else if (type === 'checkbox') f.value = el.checked;
    else if (type === 'file') f.value = el.files && el.files.length ? el.files[0].name : null;
    else f.value = el.isContentEditable && el.tagName !== 'INPUT' ? txt(el) : el.value;
    if (/\*\s*$/.test(f.label)) f.required = true;
    out.push(f);
  }
  return out;
}
"""

ERRORS_JS = r"""
(rootSel) => {
""" + DEEP_JS + r"""
  const root = rootSel ? deepOne(rootSel) : document;      // asked-for window missing: touch nothing
  if (!root) return [];
  const msgs = [];
  const ERR_TEXT = /^(this field is required|invalid input|.{1,40} is required|please (enter|make|select|provide) .{1,60}|enter a (valid|whole|decimal) .{1,40})\.?$/i;
  deepAll(root, '[aria-invalid="true"], .artdeco-inline-feedback--error, [role="alert"], .error, .field-error, .invalid-feedback, [class*="error-message"]')
    .forEach(e => { const r = e.getBoundingClientRect(); const t = (e.innerText || '').trim();
                    if (r.width > 0 && r.height > 0 && t && t.length < 200 && !/\bsuccess/i.test(t)) msgs.push(t); });
  // LinkedIn 2026: errors are bare <p>/<span> with no role or class ("A resume is required")
  deepAll(root, 'p, span, div').forEach(e => { if (e.children.length) return;
    const t = (e.innerText || '').trim(); const r = e.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && ERR_TEXT.test(t)) msgs.push(t); });
  return [...new Set(msgs)].slice(0, 10);
}
"""

SUCCESS_WORDS = ("application submitted", "application was sent", "your application was sent", "thank you for applying",
                 "thanks for applying", "application has been received", "we have received your application",
                 "we've received your application", "successfully applied", "you have successfully applied",
                 "application received", "applied successfully", "thank you for your application",
                 "thanks for your application", "your application has been submitted")
BLOCK_SELECTORS = ['iframe[src*="recaptcha"]', 'iframe[src*="hcaptcha"]', 'iframe[src*="challenges.cloudflare"]',
                   'iframe[title*="captcha" i]', '#captcha', '.g-recaptcha', '.h-captcha']


@dataclass
class Fill:
    field: dict
    answer: F.Answer
    chosen: Any = None
    ok: bool = False
    why: str = ""


@dataclass
class FormResult:
    fills: list[Fill] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[Fill]:
        """Required fields we could not fill with a basic fact."""
        out = []
        for f in self.fills:
            req = f.field.get("required")
            if f.answer.key in ("captcha", "otp"):
                out.append(f)
            elif req and not (f.ok and f.answer.basic):
                if f.answer.key in ("resume", "cover_letter_file") and f.ok:
                    continue
                out.append(f)
        return out

    @property
    def non_basic_filled(self) -> list[Fill]:
        return [f for f in self.fills if f.ok and not f.answer.basic and f.answer.key not in ("resume", "cover_letter_file")]

    def all_basic(self) -> bool:
        return not self.blockers and not self.missing and not self.non_basic_filled


class BrowserBusy(RuntimeError):
    """The bot's browser profile is already open in another window."""


def _profile_lock(prof_dir: Path) -> int | None:
    """PID holding the Chromium profile, or None. Removes a lock left by a crashed browser."""
    import os
    lock = prof_dir / "SingletonLock"
    try:
        target = os.readlink(lock)
    except OSError:
        return None
    try:
        pid = int(target.rsplit("-", 1)[-1])
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        for n in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                (prof_dir / n).unlink()
            except OSError:
                pass
        return None


def _calm_profile(prof_dir: Path) -> None:
    """Start clean: no "restore pages?" bubble, no restored tabs that close our work tab."""
    import json
    pref = prof_dir / "Default" / "Preferences"
    try:
        d = json.loads(pref.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    d.setdefault("profile", {}).update(exit_type="Normal", exited_cleanly=True)
    d.setdefault("session", {})["restore_on_startup"] = 5          # 5 = open the New Tab page
    try:
        pref.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def launch(headless: bool | None = None):
    """Persistent Chromium profile: you log in to LinkedIn/Naukri once, it stays logged in."""
    from playwright.sync_api import sync_playwright
    s = config.cfg("search").get("apply", {})
    prof_dir = config.path(s.get("browser_profile_dir", "data/browser-profile"))
    prof_dir.mkdir(parents=True, exist_ok=True)
    pid = _profile_lock(prof_dir)
    if pid:
        raise BrowserBusy(f"the bot's browser is already open (process {pid}): close that window "
                          "(or wait for the other `hunt` run to finish), then try again")
    _calm_profile(prof_dir)
    pw = sync_playwright().start()
    try:
        ctx = pw.chromium.launch_persistent_context(
            str(prof_dir), headless=s.get("headless", False) if headless is None else headless,
            no_viewport=not (s.get("headless", False) if headless is None else headless),   # page = real window size
            viewport=None if not (s.get("headless", False) if headless is None else headless) else {"width": 1280, "height": 860},
            locale="en-IN", timezone_id="Asia/Kolkata",
            args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                  "--disable-session-crashed-bubble", "--hide-crash-restore-bubble"])
    except Exception:
        pw.stop()
        raise
    return pw, ctx


def work_page(ctx):
    """One fresh tab to work in; extra start-up tabs are closed so none can steal or close it."""
    page = ctx.new_page()
    for p in list(ctx.pages):
        if p is not page:
            try:
                p.close()
            except Exception:
                pass
    return page


def human_pause(a: float = 0.3, b: float = 1.1) -> None:
    time.sleep(random.uniform(a, b))


def scan(page, root: str | None = None) -> list[dict]:
    return page.evaluate(SCAN_JS, root)


def errors(page, root: str | None = None) -> list[str]:
    try:
        return page.evaluate(ERRORS_JS, root)
    except Exception:
        return []


def blocked(page) -> list[str]:
    out = []
    for sel in BLOCK_SELECTORS:
        try:
            if page.locator(sel).count():
                out.append("captcha on page")
                break
        except Exception:
            pass
    return out


TEXT_JS = r"""
() => {
""" + DEEP_JS + r"""
  let t = document.body ? document.body.innerText : '';
  for (const e of deepAll(document, '*')) if (e.shadowRoot) t += '\n' + [...e.shadowRoot.children].map(c => c.innerText || c.textContent || '').join('\n');
  return t;
}
"""


def page_text(page) -> str:
    """Visible text, including forms drawn inside shadow roots."""
    try:
        return (page.evaluate(TEXT_JS) or "").lower()
    except Exception:
        try:
            return page.inner_text("body", timeout=3000).lower()
        except Exception:
            return ""


def succeeded(page) -> bool:
    t = page_text(page)
    return any(w in t for w in SUCCESS_WORDS)


def _type(page, loc, value: str, read_back: bool = True) -> bool:
    """Clear, type, read back; one retry if the field doesn't hold exactly `value`. False if it still doesn't."""
    lo, hi = config.cfg("search").get("apply", {}).get("typing_delay_ms", [30, 90])
    for _ in range(2):
        loc.click()
        loc.fill("")
        loc.press_sequentially(str(value), delay=random.randint(lo, hi))
        if not read_back or loc.input_value().strip() == str(value).strip():
            return True
    return False


# Every field the bot touches for the current job, for the Review Center record.
RECORD: list[dict] = []


SNAP_PATH: Path | None = None        # set per job by the runner


def snap(page) -> None:
    """Full-page screenshot of the filled form (before submit / hand-over)."""
    if SNAP_PATH is None:
        return
    try:
        page.screenshot(path=str(SNAP_PATH), full_page=True)
    except Exception:
        pass


def record(fill: "Fill", page_url: str) -> None:
    f, a = fill.field, fill.answer
    RECORD.append({"jh": f.get("jh"), "label": f.get("label"), "type": f.get("type"), "required": bool(f.get("required")),
                   "value": fill.chosen if fill.ok else None, "basis": a.basis if fill.ok else "none",
                   "basic": bool(fill.ok and (a.basic or a.key in ("resume", "marketing"))),
                   "key": a.key, "note": fill.why or a.note, "url": page_url,
                   "options": [o.get("text") for o in f.get("options", []) if o.get("text")][:40]})


def fill_form(page, job: dict | None, resume_path: Path | None, root: str | None = None,
              cover_text: str | None = None, skip_jh: set[str] | None = None) -> FormResult:
    """Fill every field we can. Leaves unknowns alone. Never clicks submit."""
    res = FormResult(blockers=blocked(page))
    skip_jh = skip_jh or set()
    for i, f0 in enumerate(scan(page, root)):
        if f0["jh"] in skip_jh:
            continue
        # Sites (LinkedIn/React) redraw while we fill and reuse inputs for other questions: find the field
        # again by its question before touching it, never trust an id from an earlier scan.
        f = f0 if i == 0 else _same_field(scan(page, root), f0)
        opts = [o["text"] for o in (f or f0).get("options", [])]
        a = F.answer(f0["label"], f0["type"], f0.get("placeholder", ""), opts, job)
        fill = Fill(f or f0, a)
        res.fills.append(fill)
        if f is None:
            fill.why = "the form redrew and this question moved: left for you"
            record(fill, page.url)
            continue
        try:
            _fill_one(page, f, a, fill, resume_path, cover_text)
            for _ in range(10):
                if fill.ok or not fill.why.startswith("no option matches"):
                    break
                # read mid-redraw (option text blank or the question's own): wait, find it again, retry, up to 4 s
                page.wait_for_timeout(400)
                f = _same_field(scan(page, root), f0)
                if f:
                    a = F.answer(f0["label"], f0["type"], f0.get("placeholder", ""),
                                 [o["text"] for o in f.get("options", [])], job)
                    fill.field, fill.answer, fill.why = f, a, ""
                    _fill_one(page, f, a, fill, resume_path, cover_text)
        except Exception as exc:  # one bad field must not stop the form
            fill.ok, fill.why = False, f"{type(exc).__name__}: {str(exc)[:80]}"
        record(fill, page.url)
        human_pause(0.15, 0.6)
    return res


def _same_field(now: list[dict], f: dict) -> dict | None:
    """`f` as the page shows it now: same question and type (same id preferred). None if it's gone."""
    same = [x for x in now if x["label"] == f["label"] and x["type"] == f["type"]]
    return next((x for x in same if x["jh"] == f["jh"]), same[0] if same else None)


def _choose(a: F.Answer, options: list[str]) -> str | None:
    for v in (a.value, *a.alts):
        c = F.pick_option(v, options)
        if c:
            return c
    return None


def _fill_one(page, f: dict, a: F.Answer, fill: Fill, resume_path: Path | None, cover_text: str | None) -> None:
    t = f["type"]
    sel = f'[data-jh="{f["jh"]}"]'
    already = f.get("value")
    if a.key == "resume" and t == "radio":      # LinkedIn's "pick a resume" cards: keep the chosen one
        pick = already or next((o["text"] for o in f["options"] if resume_path and o["text"] == resume_path.name), None)
        if pick and not already:
            page.locator(f'[data-jh="{next(o["jh"] for o in f["options"] if o["text"] == pick)}"]').check(force=True)
        fill.ok, fill.chosen, fill.why = bool(pick), pick, "" if pick else "pick your resume"
        if pick:
            fill.answer = F.Answer(a.key, pick, "profile", "your resume")
        return
    if a.key in ("resume", "cover_letter_file"):
        if a.key == "resume" and resume_path:
            page.set_input_files(sel, str(resume_path))
            fill.ok, fill.chosen = True, resume_path.name
            fill.answer = F.Answer(a.key, resume_path.name, "profile", "your resume")
        else:
            fill.why = "no file"
        return
    if a.key == "cover_letter":
        if cover_text and f.get("required"):
            page.locator(sel).fill(cover_text)
            fill.ok, fill.chosen, fill.why = True, "drafted letter", "drafted: you review"
        else:
            fill.why = "optional free-text left empty" if not f.get("required") else "needs your words"
        return
    if a.key == "marketing":
        fill.ok, fill.chosen = True, "left unticked"
        return
    if a.value is None or (a.value == "" and a.key not in ("middle_name", "referred")):
        if already not in (None, "", False) and t not in ("radio", "checkbox", "select") and a.key in SITE_PREFILL_OK:
            # site pre-filled a typed field (e.g. LinkedIn email/phone from your account). That's your own data.
            # Anything else already in a box (a stale value, another field's text) is NOT: basis "none", a question.
            fill.ok, fill.chosen = True, already
            fill.answer = F.Answer(a.key, already, "profile", "pre-filled by the site from your account")
        else:
            fill.why = a.note or "no answer in profile"
        return
    if t == "radio":
        choice = _choose(a, [o["text"] for o in f["options"]])
        if not choice:
            fill.why = f"no option matches {a.value!r}"
            return
        o = next(o for o in f["options"] if o["text"] == choice)
        loc = page.locator(f'[data-jh="{o["jh"]}"]')
        try:
            loc.check(timeout=3000)
        except Exception:
            page.locator(f'label[for="{loc.get_attribute("id")}"]').click()
        fill.ok, fill.chosen = True, choice
        return
    if t == "select":
        choice = _choose(a, [o["text"] for o in f["options"]])
        if not choice:
            if already not in (None, "") and a.key in SITE_PREFILL_OK:
                # e.g. LinkedIn's email dropdown lists the addresses of your own account
                fill.ok, fill.chosen = True, already
                fill.answer = F.Answer(a.key, already, "profile", "pre-filled by the site from your account")
                return
            fill.why = f"no option matches {a.value!r}" + (f" (site shows {already!r}: check it)" if already else "")
            return
        page.select_option(sel, label=choice)
        fill.ok, fill.chosen = True, choice
        return
    if t == "checkbox":
        loc = page.locator(sel)
        want = bool(a.value) if not isinstance(a.value, str) else F.pick_option(a.value, ["Yes", "No"]) == "Yes"
        if want and not loc.is_checked():
            try:
                loc.check(timeout=3000)
            except Exception:
                loc.evaluate("e => e.click()")
        fill.ok, fill.chosen = True, want
        return
    value = str(a.value)
    if f.get("maxlength") and len(value) > f["maxlength"]:
        fits = next((str(x) for x in a.alts if x and len(str(x)) <= f["maxlength"]), None)
        if fits:                                     # your own shorter wording of the same answer
            value = fits
        elif a.key == "phone":
            value = value[-f["maxlength"]:]
        else:
            fill.why = "too long for field"
            return
    if already and str(already).strip() == value:
        fill.ok, fill.chosen = True, value
        return
    loc = page.locator(sel)
    if f.get("combo"):
        _type(page, loc, value, read_back=False)      # the site rewrites it to the picked option
        human_pause(0.7, 1.2)
        opt = page.locator('[role="option"], .select__option, li[id*="option"], .basic-typeahead__selectable').first
        if opt.count():
            opt.click()
        else:
            loc.press("ArrowDown")
            loc.press("Enter")
    elif t == "textarea" and f["tag"] != "textarea":     # contenteditable
        loc.click()
        loc.press_sequentially(value, delay=20)
    elif not _type(page, loc, value):
        fill.why = f"typed {value!r} twice but the field shows {loc.input_value()[:30]!r}"
        return
    fill.ok, fill.chosen = True, value


SITE_PREFILL_OK = {"email", "email_confirm", "phone", "country_code"}

REJECTED_JS = r"""e => { if (e.getAttribute('aria-invalid') === 'true') return true;
  const r = e.getRootNode(); const t = (e.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean)
    .map(i => (r.getElementById ? r.getElementById(i) : document.getElementById(i))?.innerText || '').join(' ');
  return /invalid input|enter a (valid|whole|decimal)/i.test(t); }"""


def retry_rejected(page, res: FormResult) -> bool:
    """A typed value the site rejected after Next (e.g. LinkedIn "Invalid input" on "Immediately") is retyped
    with your next wording of the same answer from the profile. True if anything was retyped."""
    changed = False
    for fl in res.fills:
        if not fl.ok or fl.field["type"] not in ("text", "number", "textarea") or fl.field.get("combo"):
            continue
        loc = page.locator(f'[data-jh="{fl.field["jh"]}"]')
        try:
            if not loc.count() or not loc.evaluate(REJECTED_JS):
                continue
        except Exception:
            continue
        words = [str(x) for x in (fl.answer.value, *fl.answer.alts) if x not in (None, "")]
        nxt = next((w for w in words[words.index(str(fl.chosen)) + 1:] if w != str(fl.chosen)), None) \
            if str(fl.chosen) in words else None
        if nxt and _type(page, loc, nxt):
            fl.chosen, changed = nxt, True
            for r in RECORD:                                   # "what was sent" shows the retyped value
                if r["jh"] == fl.field["jh"]:
                    r["value"] = nxt
    return changed


def verify(page, res: FormResult, root: str | None = None) -> list[str]:
    """Second read: every field we filled still holds the value; no errors showing."""
    problems = []
    now = {f["jh"]: f for f in scan(page, root)}
    for fl in res.fills:
        if not fl.ok or fl.answer.key in ("marketing",):
            continue
        cur = now.get(fl.field["jh"])
        if cur is None:
            continue
        v = cur.get("value")
        if fl.field["type"] in ("text", "email", "tel", "number", "url", "textarea") and not fl.field.get("combo"):
            if str(v or "").strip() != str(fl.chosen or "").strip():
                problems.append(f"{fl.field['label'][:40]}: reads back {str(v)[:30]!r}")
        elif fl.field["type"] == "select" and v != fl.chosen:
            problems.append(f"{fl.field['label'][:40]}: select shows {v!r}")
        elif fl.field["type"] == "radio" and not v:
            problems.append(f"{fl.field['label'][:40]}: no radio checked")
    problems += [f"page error: {e}" for e in errors(page, root)]
    return problems


def click_button(page, names: list[str], root: str | None = None, dry: bool = False) -> str | None:
    """Click the first visible button whose text/aria-label matches; returns which."""
    scope = page.locator(root).first if root else page
    for name in names:
        for loc in (scope.get_by_role("button", name=name, exact=False),
                    scope.locator(f'input[type=submit][value*="{name}" i]'),
                    scope.locator(f'button[aria-label*="{name}" i]')):
            try:
                n = loc.count()
            except Exception:
                n = 0
            for i in range(n):
                b = loc.nth(i)
                try:
                    if b.is_visible() and b.is_enabled():
                        if not dry:
                            human_pause(0.5, 1.3)
                            b.click()
                        return name
                except Exception:
                    continue
    return None
