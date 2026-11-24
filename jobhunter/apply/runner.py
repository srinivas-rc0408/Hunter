"""`hunt apply`: open your browser and work through the queue, best jobs first."""
from __future__ import annotations

import datetime as dt
import os
import random
import re
import shutil
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


def my_resume() -> Path | None:
    """Your own resume PDF (config/resume.pdf), sent under a clean name: First_Last_Resume.pdf."""
    own = config.path("config", "resume.pdf")
    if not own.is_file():
        return None
    name = re.sub(r"[^A-Za-z0-9]+", "_", str(config.cfg("profile").get("name") or "My")).strip("_") + "_Resume.pdf"
    up = config.data_dir() / "upload"
    up.mkdir(exist_ok=True)
    out = up / name
    if not out.exists() or out.stat().st_mtime < own.stat().st_mtime or out.read_bytes() != own.read_bytes():
        shutil.copyfile(own, out)
    return out


class _Lock:
    """One apply run at a time: they share one browser profile."""
    def __enter__(self):
        import fcntl
        self.f = open(config.data_dir() / "apply.lock", "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            raise B.BrowserBusy("another apply run is using the bot's browser right now "
                                "(started from the terminal, Review Center or Telegram). Wait for it to finish.")
        return self

    def __exit__(self, *a):
        try:
            self.f.close()
        except OSError:
            pass


def _fresh(pw, ctx, page, headless):
    """A working tab: the same one if it's alive, a new tab, or a relaunched browser."""
    try:
        if not page.is_closed():
            page.evaluate("1")
            return pw, ctx, page
    except Exception:
        pass
    try:
        return pw, ctx, B.work_page(ctx)
    except Exception:
        try:
            ctx.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
        console.print("[yellow]browser closed: opening it again[/]")
        pw, ctx = B.launch(headless=headless)
        return pw, ctx, B.work_page(ctx)


def apply(ids: list[str] | None = None, top: int = 10, min_score: int = 50, dry: bool = False,
          headless: bool | None = None, with_cover: bool = True, approve: bool = False,
          submit_now: bool = False) -> list[tuple[dict, flows.Outcome]]:
    """approve=True: you reviewed this job's values (Review Center / Telegram).
    submit_now=True: submit without the check panel if nothing is missing."""
    try:
        with _Lock():
            return _apply(ids, top, min_score, dry, headless, with_cover, approve, submit_now)
    except B.BrowserBusy as exc:
        console.print(f"[red]Can't start:[/] {exc}")
        if os.environ.get("JH_NOTIFY"):
            notify.telegram(f"⚠️ Can't start on the laptop: {notify._esc(str(exc))}")
        return []


SAME_CAUSE, MAX_TRIES = 3, 5


def applied_before(db: DB, job: dict, days: int = 30) -> str | None:
    """When you already applied: to this job ever, or to the same title at the same company in the last
    `days` days (the same role reposted, or found on another board). None when you haven't."""
    from ..companies import same_company
    from ..util import norm
    on = db.applied_on(job["id"])
    if on or job.get("status") in ("applied", "interview", "offer", "rejected"):
        return on or job.get("status_at") or "an earlier day"
    r = db.c.execute("SELECT MIN(at) FROM applications WHERE job_id=? AND outcome='applied' AND dry=0", (job["id"],)).fetchone()
    if r and r[0]:
        return r[0]
    since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    title = norm(job["title"])
    for j in db.c.execute("SELECT j.id, j.title, j.company, MIN(e.at) FROM jobs j JOIN events e ON e.job_id = j.id "
                          "WHERE e.kind = 'applied' AND e.at >= ? AND j.id != ? GROUP BY j.id", (since, job["id"])):
        if norm(j[1]) == title and same_company(j[2], job["company"]):
            return j[3]
    return None


def cap_reached(db: DB, site: str) -> str | None:
    """The daily caps (all sites, and per site) as plain words, or None. flows.gate() enforces them too."""
    s = config.cfg("search").get("apply", {})
    total = s.get("max_auto_submits_per_day", 25)
    if db.applied_today() >= total:
        return f"daily cap reached ({total} applications today)"
    cap = (s.get("max_per_site_per_day") or {}).get(site)
    if cap is not None and db.applied_today(site) >= cap:
        return f"{site.title()} daily cap reached ({cap} today)"
    return None


def after_failure(db: DB, job: dict, why: str) -> bool:
    """Count this job's failed tries. 3 with the same cause or 5 in all: take it out of the queue,
    show the exact error on the dashboard (To review) and on Telegram. True when it gave up."""
    cause = lambda w: re.sub(r"\d+", "#", w or "")
    fails = [r[0] for r in db.c.execute("SELECT why FROM applications WHERE job_id=? AND outcome='failed' AND dry=0",
                                        (job["id"],))]
    same = sum(cause(w) == cause(why) for w in fails)
    if same < SAME_CAUSE and len(fails) < MAX_TRIES:
        return False
    db.set_status(job["id"], "needs_you", f"gave up after {len(fails)} tries ({same}× this error): {why}")
    notify.telegram(f"⛔ <b>Apply failed {len(fails)}×</b>, stopped retrying: {notify._esc(job['title'])} · "
                    f"{notify._esc(job['company'])}\n<code>{notify._esc(why[:600])}</code>"
                    + (f"\n{notify.apply_link(job)} yourself, or /skip_{job['id'][:8]}" if job.get("url") else ""))
    return True


def prune_screens(db: DB, days: int = 7) -> int:
    """Delete data/screens files older than `days` (they show your name, email, phone). The screenshot
    of every application really sent is kept. Returns how many files went."""
    keep = {r[0] for r in db.c.execute("SELECT screenshot FROM applications WHERE outcome='applied' AND dry=0")}
    root, cutoff, n = config.data_dir() / "screens", time.time() - days * 86400, 0
    for f in sorted(root.rglob("*"), reverse=True):          # files before their folders
        if f.is_file() and str(f) not in keep and f.stat().st_mtime < cutoff:
            f.unlink()
            n += 1
        elif f.is_dir() and not any(f.iterdir()):
            f.rmdir()
    return n


def _apply(ids, top, min_score, dry, headless, with_cover, approve, submit_now):
    db = DB()
    flows.APPROVED, flows.SUBMIT_NOW = approve, submit_now
    jobs = queue(db, ids, top, min_score)
    if not jobs:
        console.print("Nothing in the queue. Run `hunt scan` first, or lower --min-score.")
        return []
    s = config.cfg("search").get("apply", {})
    hidden = s.get("headless", False) if headless is None else headless
    flows.VISIBLE = not hidden
    if flows.checking() and not dry:
        console.print("[bold]Check mode:[/] the bot fills each form, then a JobHunter panel on the page lists every "
                      "field. Read it, fix anything on the page, press Submit. Your fixes are remembered.")
    lo, hi = s.get("pause_range_sec", [35, 90])
    pw, ctx = B.launch(headless=hidden)
    page = B.work_page(ctx)
    done: list[tuple[dict, flows.Outcome]] = []
    capped: set[str] = set()
    try:
        for n, job in enumerate(jobs, 1):
            console.rule(f"[{n}/{len(jobs)}] {job['title']} · {job['company']} · score {job['score']}")
            on = applied_before(db, job)
            if on:
                console.print(f"   already applied on {on[:10]}: skipped (never twice)")
                db.event(job["id"], "skip_duplicate", f"already applied on {on[:10]}")
                continue
            cap = None if dry else cap_reached(db, flows.site_of(job["url"]))
            if cap:
                console.print(f"[yellow]   {cap}: continues tomorrow[/]")
                if cap not in capped:
                    capped.add(cap)
                    notify.telegram(f"⏸ {notify._esc(cap)}, continues tomorrow (human pace, never spam)")
                continue
            B.RECORD.clear()
            shots = config.data_dir() / "screens"
            shots.mkdir(exist_ok=True)
            B.SNAP_PATH = shots / f"{job['id']}-{int(time.time())}.png"
            resume, rrep = None, None
            try:
                mode = s.get("resume", "own")
                if mode == "own" and my_resume():
                    resume = my_resume()
                    console.print(f"   resume: your own PDF ({resume.name})")
                else:
                    rrep = resume_pdf.tailor_job(job, use_llm=True)
                    resume = Path(rrep["pdf"])
                    console.print(f"   resume: ATS {rrep['before']} → {rrep['after']} ({rrep['how']})")
            except Exception as exc:
                resume = my_resume()
                console.print(f"[yellow]tailoring failed ({exc}); using {resume}[/]")
            cover = None
            if with_cover:
                cover, rep, how = tailor.cover(job)
                (config.data_dir() / "covers").mkdir(exist_ok=True)
                (config.data_dir() / "covers" / f"{job['id']}.txt").write_text(cover, encoding="utf-8")
            out = None
            for attempt in (1, 2):
                pw, ctx, page = _fresh(pw, ctx, page, hidden)
                B.RECORD.clear()
                try:
                    out = flows.run(page, db, job, resume, cover, dry)
                    break
                except Exception as exc:
                    closed = "closed" in str(exc).lower() or "TargetClosed" in type(exc).__name__
                    out = flows.Outcome("failed", flows.site_of(job["url"]), why=_plain(exc))
                    if not (closed and attempt == 1):
                        break
                    console.print("[yellow]the browser tab closed: retrying this job once[/]")
            if not B.SNAP_PATH.exists():
                try:
                    B.snap(page)
                except Exception:
                    pass
            colour = {"applied": "green", "needs_you": "yellow", "filled": "cyan"}.get(out.status, "red")
            console.print(f"[bold {colour}]{out.status.upper()}[/]{' (sent automatically)' if out.auto else ''} · {out.site} · {out.why}"
                          + (f" · learned {out.learned} answers" if out.learned else ""))
            try:
                url_now = page.url
            except Exception:
                url_now = job["url"]
            if out.status in ("failed", "needs_you") and not flows.checking():
                try:                                   # what the bot saw, for fixing the flow (stays on this laptop)
                    (shots / f"{job['id']}-page.html").write_text(page.content(), encoding="utf-8")
                    console.print(f"   [dim]saved what the bot saw: {B.SNAP_PATH}[/]")
                except Exception:
                    pass
            if out.status in ("needs_you", "filled"):
                from . import questions
                nq = questions.add_from_record(job["id"], list(B.RECORD), out.site)
                if nq:
                    console.print(f"[yellow]❓ {nq} new question(s) for you[/]: answer once with `hunt questions` "
                                  "(or Review Center → Questions, or Telegram /questions)")
            db.add_application(job["id"], out.site, out.status, out.auto, approve, dry, out.why, list(B.RECORD),
                               str(B.SNAP_PATH) if B.SNAP_PATH.exists() else "", str(resume or ""), rrep,
                               cover or "", url_now)
            if out.site in ("linkedin", "naukri"):              # /health alert: "run hunt login linkedin"
                from .. import health
                out_of = bool(re.search(r"logged out|not logged in", out.why or ""))
                if out_of or out.status in ("applied", "needs_you", "filled"):
                    health.set_flag(f"{out.site}_logged_out", out_of)
            if not dry:
                detail = f"site={out.site} auto={out.auto} approved={approve} why={out.why[:200]}"
                if out.status == "applied":
                    db.set_status(job["id"], "applied", detail)
                    if not out.why.startswith("you already applied"):
                        notify.telegram(f"✅ <b>Applied</b>: {notify._esc(job['title'])} · {notify._esc(job['company'])}"
                                        + (f"\n✎ learned {out.learned} correction(s) from you" if out.learned else ""))
                elif out.status == "needs_you":
                    db.set_status(job["id"], "needs_you", detail)
                    if not flows.checking():           # you weren't at the form: tell your phone
                        notify.review_needed(job, out.why, list(B.RECORD), B.SNAP_PATH)
                elif out.status == "skipped":
                    db.set_status(job["id"], "skipped", detail)
                else:
                    db.event(job["id"], "apply_failed", detail)
                    if after_failure(db, job, out.why):
                        console.print("[red]gave up on this job after repeated failures: it's on the dashboard[/]")
            else:
                db.event(job["id"], "dry_run", out.why[:200])
            done.append((job, out))
            if n < len(jobs) and not dry and out.status == "applied" and not flows.checking():
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
        prune_screens(db)
        flows.APPROVED = flows.SUBMIT_NOW = False
        for closer in (ctx.close, pw.stop):
            try:
                closer()
            except Exception:
                pass
    a = sum(1 for _, o in done if o.status == "applied")
    w = sum(1 for _, o in done if o.status == "needs_you")
    console.print(f"\n[bold]Applied {a} · waiting for you {w} · other {len(done) - a - w}[/]")
    return done


def _plain(exc: Exception) -> str:
    """Errors in plain words."""
    t, m = type(exc).__name__, str(exc)
    if "closed" in m.lower() or "TargetClosed" in t:
        return "the browser tab closed before the page loaded (fixed by a retry; if it repeats, close other bot windows)"
    if "Timeout" in t:
        return "a step took too long: " + m.split("\n")[0][:140]
    if "net::ERR" in m:
        return "couldn't reach the site (" + m.split("net::")[1].split()[0] + ")"
    return f"{t}: {m[:160]}"


def login(site: str) -> None:
    urls = {"linkedin": "https://www.linkedin.com/login", "naukri": "https://www.naukri.com/nlogin/login",
            "indeed": "https://secure.indeed.com/auth", "internshala": "https://internshala.com/login"}
    try:
        pw, ctx = B.launch(headless=False)
    except B.BrowserBusy as exc:
        console.print(f"[red]Can't start:[/] {exc}")
        return
    page = B.work_page(ctx)
    page.goto(urls.get(site, site))
    console.print(f"Log in to {site} in the window, then press Enter here. The session is saved in data/browser-profile.")
    try:
        input()
    finally:
        ctx.close()
        pw.stop()
