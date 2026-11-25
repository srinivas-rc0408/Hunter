"""`hunt browser`: the bot's own browser, for applying anywhere by hand.

Browse to any job or application form (LinkedIn, Naukri, a company careers site, a Google Form).
Every page has a green **🎯 Fill this form** button (bottom-left). Press it and the bot:
  1. fills every field it knows (your details, answers you taught it, your resume PDF);
  2. shows the check panel: every field, its value and its source;
  3. waits while you read and fix, then you press Submit;
  4. learns every change you made, and records the application (dashboard + Telegram ✅).
Uses the same logged-in profile as `hunt apply`, so LinkedIn and Naukri stay signed in.
"""
from __future__ import annotations

import re

from .. import config, notify
from ..db import DB, Job
from ..util import console
from . import browser as B
from . import flows
from .runner import _Lock, my_resume

FAB_JS = r"""
(() => {
  if (window.top !== window) return;
  const add = () => {
    if (document.getElementById('jh-fab') || !document.body) return;
    const b = document.createElement('button'); b.id = 'jh-fab'; b.type = 'button';
    b.textContent = '🎯 Fill this form';
    b.title = 'JobHunter fills every field it knows, then shows you what it typed before anything is sent';
    b.style.cssText = 'position:fixed;left:16px;bottom:16px;z-index:2147483646;padding:10px 14px;border-radius:999px;' +
      'border:0;background:#1a7f37;color:#fff;font:600 14px system-ui,sans-serif;box-shadow:0 6px 20px rgba(0,0,0,.3);cursor:pointer';
    b.addEventListener('click', () => { b.textContent = '⏳ Filling…'; b.disabled = true;
      try { window.__jhFill(); } catch (e) { b.textContent = '🎯 Fill this form'; b.disabled = false; } });
    document.body.appendChild(b);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add); else add();
  setInterval(add, 2000);          // single-page apps redraw the body
})();
"""

INFO_JS = r"""
() => { const og = n => (document.querySelector(`meta[property="og:${n}"]`) || {}).content || '';
  const pick = s => { const e = document.querySelector(s); return e ? (e.innerText || '').trim() : ''; };
  return {h1: pick('h1'), og_title: og('title'), title: document.title, site: og('site_name'),
          company: pick('.job-details-jobs-unified-top-card__company-name, .jobs-unified-top-card__company-name, ' +
                        '.jd-header-comp-name a, .comp-name, [data-testid="company-name"], .company-name'),
          host: location.hostname, url: location.href}; }
"""

START = "https://www.linkedin.com/jobs/collections/recommended/"


def _company_from_host(host: str) -> str:
    parts = [p for p in host.lower().split(".") if p not in ("www", "careers", "jobs", "apply", "boards", "in", "co", "com",
                                                            "org", "net", "io", "ai")]
    return (parts[-1] if parts else host).replace("-", " ").title()


def job_for_page(db: DB, page) -> dict:
    """The job this page is about: an existing tracker entry for this URL, or a new one."""
    info = page.evaluate(INFO_JS)
    url = info["url"].split("#")[0]
    row = db.c.execute("SELECT id FROM jobs WHERE url=? OR url=?", (url, url.split("?")[0])).fetchone()
    if row:
        return db.get(row[0])
    title = (info["h1"] or info["og_title"] or info["title"] or "Application").strip()
    title = re.split(r"\s+[|\-–]\s+", title)[0][:120]
    company = (info["company"] or info["site"] or _company_from_host(info["host"])).strip()[:80]
    j = Job(title, company, "", url=url, source="browser")
    db.upsert(j)
    db.event(j.id, "opened_in_bot_browser", url)
    return db.get(j.id)


def assist(db: DB, page) -> None:
    job = job_for_page(db, page)
    flows.TITLE = f"{job['title']} · {job['company']}"
    flows.VISIBLE, flows.APPROVED, flows.SUBMIT_NOW = True, False, False
    B.RECORD.clear()
    B.FORM_URL = page.url
    shots = config.data_dir() / "screens"
    shots.mkdir(exist_ok=True)
    B.SNAP_PATH = shots / f"{job['id']}-browser.png"
    resume = my_resume()
    site = flows.site_of(page.url)
    console.rule(f"🎯 {flows.TITLE}")
    if site == "linkedin" and page.locator(flows.LI_MODAL).count():
        out = flows._linkedin_check(page, db, job, resume, None, B.FormResult())
    else:
        res = B.fill_form(page, job, resume)
        flows._report(res)
        out = flows.check_form(page, db, job, res, site, resume, None)
    console.print(f"[bold]{out.status.upper()}[/] · {out.why}" + (f" · learned {out.learned}" if out.learned else ""))
    db.add_application(job["id"], site, out.status, False, True, False, out.why, list(B.RECORD),
                       str(B.SNAP_PATH) if B.SNAP_PATH.exists() else "", str(resume or ""), None, "", page.url,
                       B.confirmation(page) if out.status == "applied" else "",
                       (job.get("details") or {}).get("apply_url") or B.FORM_URL)
    if out.status == "applied":
        db.set_status(job["id"], "applied", "bot browser")
        notify.telegram(f"✅ <b>Applied</b>: {notify._esc(job['title'])} · {notify._esc(job['company'])} (bot browser)")
    elif out.status == "skipped":
        db.set_status(job["id"], "skipped", "bot browser")
    else:
        db.set_status(job["id"], "needs_you", "bot browser: " + out.why[:120])
    try:
        page.evaluate("() => { const b = document.getElementById('jh-fab'); if (b) { b.disabled = false; b.style.display = ''; b.textContent = '🎯 Fill this form'; } }")
    except Exception:
        pass


def browse(url: str | None = None) -> None:
    try:
        lock = _Lock().__enter__()
    except B.BrowserBusy as exc:
        console.print(f"[red]Can't start:[/] {exc}")
        return
    db = DB()
    try:
        pw, ctx = B.launch(headless=False)
    except B.BrowserBusy as exc:
        console.print(f"[red]Can't start:[/] {exc}")
        lock.__exit__()
        return
    asked: list = []
    try:
        ctx.expose_binding("__jhFill", lambda src: asked.append(src["page"]))
        ctx.add_init_script(FAB_JS)
        page = B.work_page(ctx)
        page.goto(url or START, wait_until="domcontentloaded", timeout=45000)
        console.print("[bold green]Bot browser open.[/] Go to any job or application form and press "
                      "[bold]🎯 Fill this form[/] (bottom-left). You check, you press Submit. Close the window when done.")
        while True:
            alive = [p for p in ctx.pages if not p.is_closed()]
            if not alive:
                break
            if asked:
                p = asked.pop(0)
                try:
                    assist(db, p)
                except Exception as exc:
                    console.print(f"[red]couldn't fill that page:[/] {type(exc).__name__}: {str(exc)[:160]}")
            else:
                alive[-1].wait_for_timeout(400)
    except Exception as exc:
        if "closed" not in str(exc).lower():
            console.print(f"[yellow]browser stopped: {type(exc).__name__}[/]")
    finally:
        for closer in (ctx.close, pw.stop):
            try:
                closer()
            except Exception:
                pass
        lock.__exit__()
    console.print("Bot browser closed. Everything you sent is in the dashboard: http://localhost:8765")
