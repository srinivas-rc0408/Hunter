"""The check panel: you read the filled form on the real website, fix anything, then submit.

After the bot fills a form it shows a panel on the page itself:
  • every field, the value the bot typed, and where it came from (your details / learned
    from you / AI draft, read it / EMPTY, type it), colour-coded on the page too;
  • click a row to jump to that field;
  • change anything right on the page: the row turns blue ("you changed this");
  • buttons: Submit application (or Next step), Skip this job, Later.
When you press Submit (on the panel or on the site), the bot reads every field once more.
Each value you changed or typed is saved as YOUR answer for that question, and it wins
over the bot's own choice on every future form. That's how a mistake is never repeated.

Never learned: file uploads, cover letters, long essays (job-specific), captcha/OTP,
and caste/diversity questions (your category is never typed into private forms).
"""
from __future__ import annotations

import time

from .. import config
from ..util import console
from . import browser as B
from . import fields as F

NEVER_LEARN = {"resume", "cover_letter", "cover_letter_file", "captcha", "otp", "diversity", "marketing",
               "category", "caste"}
ESSAY_CHARS = 300

TAG = {"profile": ("your details", "#1a7f37"), "consent": ("consent", "#1a7f37"),
       "learned": ("learned from you", "#6b4bd8"), "drafted": ("AI draft: read it", "#b26a00"),
       "none": ("EMPTY: type it", "#c62828"), "opt": ("optional, empty", "#777777")}

PANEL_JS = r"""
(d) => {
""" + B.DEEP_JS + r"""
  const old = document.getElementById('jh-panel-host'); if (old) old.remove();
  window.__jhAction = null;
  const host = document.createElement('div'); host.id = 'jh-panel-host';
  host.style.cssText = 'position:fixed;top:12px;left:12px;z-index:2147483647;';
  const sh = host.attachShadow({mode: 'open'});
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const counts = {}; d.rows.forEach(r => counts[r.tag] = (counts[r.tag] || 0) + 1);
  const chip = (t, n) => n ? `<span class="chip" style="border-color:${d.tags[t][1]};color:${d.tags[t][1]}">${n} ${esc(d.tags[t][0])}</span>` : '';
  sh.innerHTML = `<style>
    .p{width:min(360px,44vw);min-width:250px;max-height:calc(100vh - 24px);display:flex;flex-direction:column;background:#fff;color:#1a1a1a;
       border:2px solid #1f5fbf;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.35);font:13px/1.4 system-ui,sans-serif}
    .h{padding:10px 12px;border-bottom:1px solid #e5e5e5}.t{font-weight:700;font-size:14px}.m{color:#555;font-size:12px}
    .chip{display:inline-block;border:1px solid;border-radius:999px;padding:0 7px;margin:3px 3px 0 0;font-size:11px;font-weight:600}
    .rows{overflow:auto;padding:4px 0;flex:1}.r{padding:6px 12px;border-bottom:1px solid #f0f0f0;cursor:pointer}
    .r:hover{background:#f4f7ff}.l{font-size:11.5px;color:#555}.v{font-weight:600;word-break:break-word}
    .tg{font-size:10.5px;font-weight:700}.ch .v{color:#1f5fbf}.f{padding:10px 12px;border-top:1px solid #e5e5e5;display:flex;gap:6px;flex-wrap:wrap}
    button{font:inherit;border-radius:8px;border:1px solid #ccc;background:#fff;padding:7px 10px;cursor:pointer}
    button.go{background:#1a7f37;border-color:#1a7f37;color:#fff;font-weight:700;flex:1}.msg{padding:6px 12px;font-size:12px;color:#b26a00}
    .x{float:right;border:none;background:none;font-size:16px;padding:0 4px}.min .rows,.min .f,.min .msg,.min .hint{display:none}
  </style>
  <div class="p" id="p"><div class="h"><button class="x" id="min" title="minimise">–</button>
    <div class="t">JobHunter · check this form <span class="m">(drag me)</span></div><div class="m">${esc(d.title)}</div>
    <div>${chip('profile', counts.profile)}${chip('learned', counts.learned)}${chip('drafted', counts.drafted)}${chip('none', counts.none)}${chip('opt', counts.opt)}</div>
    <div class="m hint" style="margin-top:4px">${esc(d.hint)}</div></div>
    <div class="msg" id="msg">${esc(d.msg || '')}</div>
    <div class="rows" id="rows">${d.rows.map((r, i) => `<div class="r" data-i="${i}" data-jh="${esc(r.jh)}">
       <div class="l">${esc(r.label)}${r.required ? ' *' : ''}</div><div class="v" id="v${i}">${esc(r.value === '' ? '—' : r.value)}</div>
       <div class="tg" id="g${i}" style="color:${d.tags[r.tag][1]}">${esc(d.tags[r.tag][0])}</div></div>`).join('') ||
       '<div class="r m">No fields to fill here. Check the page, then use the buttons below.</div>'}</div>
    <div class="f">${d.dry ? '<button class="go" data-a="close">Close preview (nothing sent)</button>' :
       `<button class="go" data-a="${d.final ? 'submit' : 'next'}">${d.final ? 'Submit application ✓' : 'Looks right · Next step →'}</button>`}
       <button data-a="skip">Skip job</button><button data-a="later">Later</button></div></div>`;
  document.documentElement.appendChild(host);
  const byJh = {}; d.rows.forEach((r, i) => byJh[r.jh] = i);
  const elOf = jh => jh.startsWith('g:') ? deepOne(`input[type=radio][name="${CSS.escape(jh.slice(2))}"]`)
                                         : deepOne(`[data-jh="${CSS.escape(jh)}"]`);
  d.rows.forEach(r => { const e = elOf(r.jh); if (!e) return;
    const box = (e.type === 'radio' || e.type === 'checkbox') ? (e.closest('fieldset, [role=radiogroup], label') || e) : e;
    box.style.outline = `3px solid ${d.tags[r.tag][1]}`; box.style.outlineOffset = '2px'; });
  sh.getElementById('rows').addEventListener('click', ev => { const row = ev.target.closest('.r'); if (!row || !row.dataset.jh) return;
    const e = elOf(row.dataset.jh); if (!e) return; e.scrollIntoView({block: 'center', behavior: 'smooth'});
    try { e.focus({preventScroll: true}); } catch (_) {}
    const o = e.style.boxShadow; e.style.boxShadow = '0 0 0 6px rgba(31,95,191,.45)'; setTimeout(() => e.style.boxShadow = o, 1200); });
  sh.querySelectorAll('button[data-a]').forEach(b => b.addEventListener('click', () => {
    window.__jhAction = b.dataset.a; sh.getElementById('msg').textContent = 'Working…'; }));
  sh.getElementById('min').addEventListener('click', () => sh.getElementById('p').classList.toggle('min'));
  const hd = sh.querySelector('.h'); hd.style.cursor = 'move';          // drag the panel by its header
  hd.addEventListener('mousedown', e => { if (e.target.tagName === 'BUTTON') return;
    const sx = e.clientX - host.offsetLeft, sy = e.clientY - host.offsetTop;
    const mv = m => { host.style.left = Math.max(0, m.clientX - sx) + 'px'; host.style.top = Math.max(0, m.clientY - sy) + 'px'; };
    const up = () => { document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); };
    document.addEventListener('mousemove', mv); document.addEventListener('mouseup', up); e.preventDefault(); });
  const changed = ev => { const t = (ev.composedPath && ev.composedPath()[0]) || ev.target; if (!t || !t.getAttribute) return;
    let jh = t.getAttribute('data-jh'); if (t.type === 'radio') jh = 'g:' + t.name;
    if (!(jh in byJh)) return; const i = byJh[jh];
    let v = t.type === 'checkbox' ? (t.checked ? '✓ ticked' : 'unticked') : t.tagName === 'SELECT' ? t.options[t.selectedIndex].text
          : t.type === 'radio' ? ((t.closest('label') || {}).innerText || t.value) : (t.value ?? t.innerText);
    sh.getElementById('v' + i).textContent = String(v).slice(0, 200) || '—';
    sh.getElementById('g' + i).textContent = 'you changed this · will be remembered'; sh.getElementById('g' + i).style.color = '#1f5fbf';
    sh.querySelector(`.r[data-i="${i}"]`).classList.add('ch'); };
  const hook = r => { if (r.__jhHooked) return; r.__jhHooked = true;   // 'change' doesn't leave a shadow root: hook each
    r.addEventListener('input', ev => window.__jhChanged && window.__jhChanged(ev), true);
    r.addEventListener('change', ev => window.__jhChanged && window.__jhChanged(ev), true); };
  hook(document); deepAll(document, '*').forEach(e => { if (e.shadowRoot && e !== host) hook(e.shadowRoot); });
  window.__jhChanged = changed;
  window.__jhMsg = m => { const e = sh.getElementById('msg'); if (e) e.textContent = m; };
  return true;
}
"""


def _shown(v) -> str:
    if v is True:
        return "✓ ticked"
    if v is False:
        return "unticked"
    return "" if v is None else str(v)[:200]


def rows_for(fills: list) -> list[dict]:
    out = []
    for fl in fills:
        a = fl.answer
        if fl.ok:
            tag = "profile" if a.basis in ("profile", "consent") else a.basis if a.basis in TAG else "drafted"
            if a.key in ("resume", "cover_letter_file"):
                tag = "profile"
        else:
            tag = "none" if fl.field.get("required") else "opt"
        val = "drafted letter (read it)" if a.key == "cover_letter" and fl.ok else _shown(fl.chosen if fl.ok else None)
        out.append({"jh": fl.field["jh"], "label": (fl.field.get("label") or a.key)[:120], "value": val,
                    "tag": tag, "required": bool(fl.field.get("required"))})
    return out


def show(page, fills: list, title: str, final: bool, dry: bool = False, msg: str = "") -> bool:
    hint = ("Read every field on the page. Fix anything wrong right there: your changes are remembered. "
            + ("Then press Submit." if final else "Then press Next step.")) if not dry else \
           "Preview only: nothing will be sent. Coloured boxes on the page show what the bot filled."
    try:
        return bool(page.evaluate(PANEL_JS, {"rows": rows_for(fills), "title": title, "final": final, "dry": dry,
                                             "hint": hint, "msg": msg, "tags": TAG}))
    except Exception:
        return False


def hide(page) -> None:
    """Take the panel (and the 🎯 button) off the page so they can't cover the site's own buttons."""
    try:
        page.evaluate("""() => { const h = document.getElementById('jh-panel-host'); if (h) h.remove();
                                 const f = document.getElementById('jh-fab'); if (f) f.style.display = 'none'; }""")
    except Exception:
        pass


def message(page, text: str) -> None:
    try:
        page.evaluate("m => window.__jhMsg && window.__jhMsg(m)", text)
    except Exception:
        pass


def _norm(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    return " ".join(str(v or "").split()).lower()


def changes(fills: list, now: dict[str, dict]) -> list[dict]:
    """What you changed or typed, compared with what the bot filled."""
    out = []
    for fl in fills:
        cur = now.get(fl.field["jh"])
        if cur is None:
            continue
        after = cur.get("value")
        if after in (None, "") or fl.field.get("type") == "file":
            continue
        before = fl.chosen if fl.ok else None
        if _norm(before) == _norm(after):
            continue
        out.append({"label": fl.field.get("label") or "", "key": fl.answer.key, "before": before, "after": after,
                    "type": fl.field.get("type"), "jh": fl.field["jh"]})
    return out


def learn(chs: list[dict], site: str) -> list[dict]:
    """Save your changes as your answers (they override the bot next time). Returns what was saved."""
    saved = []
    for c in chs:
        if c["key"] in NEVER_LEARN or not c["label"].strip():
            continue
        if isinstance(c["after"], str) and len(c["after"]) > ESSAY_CHARS:
            continue                                   # an essay is about this one job: not reused
        F.learn(c["label"], c["after"], site, override=True)
        saved.append(c)
        for r in B.RECORD:                             # the Review Center shows what was really sent
            if r.get("jh") == c["jh"]:
                r.update(value=c["after"], basis="you", basic=True,
                         note=f"you changed it from {_shown(c['before']) or 'empty'}")
    return saved


def _form_here(page, root: str | None) -> bool:
    try:
        return bool(page.locator(root).count()) if root else bool(B.scan(page))
    except Exception:
        return False


def hand_over(page, fills: list, site: str, title: str, root: str | None = None, final: bool = True,
              limit: int | None = None, msg: str = "") -> tuple[str, list[dict]]:
    """Show the panel and wait for you. Returns (action, learned):
    action = submit | next (you pressed the panel button: the caller clicks the site's button),
             moved (you pressed the site's own Next: a new page/step of the form is showing),
             submitted (you pressed the site's own Submit and it went through),
             skip | later | close | closed | timeout."""
    limit = limit or config.cfg("search").get("apply", {}).get("wait_for_you_sec", 900)
    show(page, fills, title, final, msg=msg)
    mine = {f.field["jh"] for f in fills}
    console.print(f"[bold yellow]→ Check the form in the browser[/] ({len(fills)} fields). Fix anything on the page, "
                  f"then press {'Submit' if final else 'Next step'} on the JobHunter panel. ({limit // 60} min max)")
    last: dict[str, dict] = {}
    t0, misses = time.time(), 0
    while time.time() - t0 < limit:
        try:
            if page.is_closed():
                return "closed", []
            present = set()
            for f in B.scan(page, root if root and page.locator(root).count() else None):
                if f["jh"] in mine or not mine:
                    last[f["jh"]] = f
                present.add(f["jh"])
            act = page.evaluate("() => window.__jhAction || null")
            if mine and present and not (mine & present) and not B.succeeded(page):
                return "moved", learn(changes(fills, last), site)      # you went to the next step yourself
            if B.succeeded(page):
                return "submitted", learn(changes(fills, last), site)
            if act in ("submit", "next"):
                page.evaluate("() => { window.__jhAction = null; }")
                return act, learn(changes(fills, last), site)
            if act in ("skip", "later", "close"):
                return act, []
            if not page.evaluate("() => !!document.getElementById('jh-panel-host')"):
                if _form_here(page, root):
                    show(page, fills, title, final, msg=msg)   # the site re-drew the page: put the panel back
                else:
                    misses += 1
                    if misses >= 3:                    # form gone: you submitted it on the site, or closed it
                        time.sleep(2)
                        return ("submitted" if B.succeeded(page) else "closed"), learn(changes(fills, last), site)
        except Exception:
            time.sleep(1.5)                            # page navigating (maybe your submit)
            try:
                if B.succeeded(page):
                    return "submitted", learn(changes(fills, last), site)
            except Exception:
                pass
        time.sleep(1)
    return "timeout", []


def say_learned(saved: list[dict]) -> None:
    for c in saved:
        console.print(f"   [blue]✎ learned[/] {c['label'][:50]} → {str(c['after'])[:60]!r} "
                      f"(bot had {(_shown(c['before']) or 'nothing')!r})")


