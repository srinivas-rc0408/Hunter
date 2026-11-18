"""`hunt apply`: open your browser and work through the queue, best jobs first."""
from __future__ import annotations

import random
import time
from pathlib import Path

from .. import config, notify, resume_pdf, tailor
from ..db import DB
from ..util import console
from . import browser as B
from . import flows


def queue(db: DB, ids: list[str] | None, top: int, min_score: int) -> list[dict]:
    if ids:
        return [j for j in (db.get(i) for i in ids) if j]
    jobs = db.query(status=["new", "alerted", "queued"], kind="private", min_score=min_score, limit=top * 3)
    jobs.sort(key=lambda j: (not j["dream"], -j["score"]))
    return jobs[:top]


def apply(ids: list[str] | None = None, top: int = 10, min_score: int = 50, dry: bool = False,
          headless: bool | None = None, with_cover: bool = True, approve: bool = False) -> list[tuple[dict, flows.Outcome]]:
    """approve=True: you reviewed this job's values in the Review Center. Drafted answers are
    allowed, and the bot waits for you in the browser for anything still missing."""
    db = DB()
    flows.APPROVED = approve
    jobs = queue(db, ids, top, min_score)
    if not jobs:
        console.print("Nothing in the queue. Run `hunt scan` first, or lower --min-score.")
        return []
    s = config.cfg("search").get("apply", {})
    lo, hi = s.get("pause_range_sec", [35, 90])
    pw, ctx = B.launch(headless=headless)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    done: list[tuple[dict, flows.Outcome]] = []
    try:
        for n, job in enumerate(jobs, 1):
            console.rule(f"[{n}/{len(jobs)}] {job['title']} · {job['company']} · score {job['score']}")
            B.RECORD.clear()
            shots = config.data_dir() / "screens"
            shots.mkdir(exist_ok=True)
            B.SNAP_PATH = shots / f"{job['id']}-{int(time.time())}.png"
            resume, rrep = None, None
            try:
                mode = s.get("resume", "tailored")
                if mode == "own" and config.path("config", "resume.pdf").is_file():
                    resume = config.path("config", "resume.pdf")
                else:
                    rrep = resume_pdf.tailor_job(job, use_llm=True)
                    resume = Path(rrep["pdf"])
                    console.print(f"   resume: ATS {rrep['before']} → {rrep['after']} ({rrep['how']})")
            except Exception as exc:
                own = config.path("config", "resume.pdf")
                resume = own if own.is_file() else None
                console.print(f"[yellow]tailoring failed ({exc}); using {resume}[/]")
            cover = None
            if with_cover:
                cover, rep, how = tailor.cover(job)
                (config.data_dir() / "covers").mkdir(exist_ok=True)
                (config.data_dir() / "covers" / f"{job['id']}.txt").write_text(cover, encoding="utf-8")
            try:
                out = flows.run(page, db, job, resume, cover, dry)
            except Exception as exc:
                out = flows.Outcome("failed", flows.site_of(job["url"]), why=f"{type(exc).__name__}: {str(exc)[:160]}")
            if not B.SNAP_PATH.exists():
                B.snap(page)
            colour = {"applied": "green", "needs_you": "yellow", "filled": "cyan"}.get(out.status, "red")
            console.print(f"[bold {colour}]{out.status.upper()}[/] ({'auto' if out.auto else 'you'}) · {out.site} · {out.why}"
                          + (f" · learned {out.learned} answers" if out.learned else ""))
            try:
                url_now = page.url
            except Exception:
                url_now = job["url"]
            if out.status in ("needs_you", "filled"):
                from . import questions
                nq = questions.add_from_record(job["id"], list(B.RECORD), out.site)
                if nq:
                    console.print(f"[yellow]❓ {nq} new question(s) for you[/]: answer once with `hunt questions` "
                                  "(or Review Center → Questions, or Telegram /questions)")
            db.add_application(job["id"], out.site, out.status, out.auto, approve, dry, out.why, list(B.RECORD),
                               str(B.SNAP_PATH) if B.SNAP_PATH.exists() else "", str(resume or ""), rrep,
                               cover or "", url_now)
            if not dry:
                detail = f"site={out.site} auto={out.auto} approved={approve} why={out.why[:200]}"
                if out.status == "applied":
                    db.set_status(job["id"], "applied", detail)
                elif out.status == "needs_you":
                    db.set_status(job["id"], "needs_you", detail)
                    notify.telegram(f"✋ <b>Review needed</b>: {job['title']} · {job['company']}\n{out.why[:300]}\n"
                                    f"Open the Review Center: http://localhost:8765/#{job['id'][:8]}")
                elif out.status == "skipped":
                    db.set_status(job["id"], "skipped", detail)
                else:
                    db.event(job["id"], "apply_failed", detail)
            else:
                db.event(job["id"], "dry_run", out.why[:200])
            done.append((job, out))
            if n < len(jobs) and not dry and out.status == "applied":
                wait = random.uniform(lo, hi)
                console.print(f"[dim]pausing {wait:.0f}s (human pace)…[/]")
                time.sleep(wait)
            # LinkedIn sometimes opens a new tab for external applies: keep one main page
            for p in ctx.pages[1:]:
                try:
                    p.close()
                except Exception:
                    pass
    finally:
        flows.APPROVED = False
        ctx.close()
        pw.stop()
    a = sum(1 for _, o in done if o.status == "applied")
    w = sum(1 for _, o in done if o.status == "needs_you")
    console.print(f"\n[bold]Applied {a} · waiting for you {w} · other {len(done) - a - w}[/]")
    return done


def login(site: str) -> None:
    urls = {"linkedin": "https://www.linkedin.com/login", "naukri": "https://www.naukri.com/nlogin/login",
            "indeed": "https://secure.indeed.com/auth", "internshala": "https://internshala.com/login"}
    pw, ctx = B.launch(headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(urls.get(site, site))
    console.print(f"Log in to {site} in the window, then press Enter here. The session is saved in data/browser-profile.")
    try:
        input()
    finally:
        ctx.close()
        pw.stop()
