"""Step through a live LinkedIn Easy Apply window with the bot's own helpers. Never presses Submit.

    .venv/bin/python scripts/dev/li_probe.py <job id> [max steps]

Prints each step's controls, what the bot filled, errors, then presses Next. Page dumps (they hold
your personal data) go to data/screens/, which is gitignored.
"""
import sys

from jobhunter import config
from jobhunter.apply import browser as B, flows
from jobhunter.apply.runner import my_resume
from jobhunter.db import DB

CTRL = r"""root => { const d = document.querySelector(root); if (!d) return null;
  return {head: (d.innerText||'').split('\n').slice(0,4).join(' | '),
    ctl: [...d.querySelectorAll('button,input,select,textarea')].map(e => e.tagName + ':' + (e.type||'') + ':' +
      ((e.getAttribute('aria-label') || e.innerText || e.labels?.[0]?.innerText || e.name || '').replace(/\s+/g,' ').trim().slice(0,50))
      + (e.tagName==='BUTTON' && e.disabled ? ' [disabled]' : ''))}; }"""
NEXT = ["Continue to next step", "Next", "Review your application", "Review"]

job = DB().get(sys.argv[1])
steps = int(sys.argv[2]) if len(sys.argv) > 2 else 6
out = config.data_dir() / "screens"
out.mkdir(exist_ok=True)
pw, ctx = B.launch(headless=False)
page = B.work_page(ctx)
try:
    page.goto(job["url"], wait_until="domcontentloaded")
    btn, name = flows.apply_button(page)
    print("button:", name)
    btn.click()
    for t in range(15):
        page.wait_for_timeout(1000)
        if page.locator(flows.LI_MODAL).count():
            print(f"window after {t + 1} s")
            break
    else:
        sys.exit("no Easy Apply window in 15 s")
    for step in range(steps):
        page.wait_for_timeout(1500)
        print(f"--- step {step}", page.evaluate(CTRL, flows.LI_MODAL))
        (out / f"probe-{job['id'][:8]}-{step}.html").write_text(page.content(), encoding="utf-8")
        flows._li_resume(page, my_resume())
        res = B.fill_form(page, job, my_resume(), root=flows.LI_MODAL)
        print("   filled:", [(f.field["label"][:40], f.chosen, f.answer.basis) for f in res.fills])
        print("   errors:", B.errors(page, flows.LI_MODAL))
        if B.click_button(page, ["Submit application"], root=flows.LI_MODAL, dry=True):
            print("   reached Submit: stopping")
            break
        print("   clicked:", B.click_button(page, NEXT, root=flows.LI_MODAL))
        page.wait_for_timeout(1500)
        print("   errors after:", B.errors(page, flows.LI_MODAL))
finally:
    ctx.close()
    pw.stop()
