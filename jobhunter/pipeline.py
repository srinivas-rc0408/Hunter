"""One scan: every source → dedupe → score → store → alert."""
from __future__ import annotations

import datetime as dt

from . import config, notify
from .db import DB, Job
from .score import is_dream, score
from .sources import ats, boards, govt, pages
from .util import Http, console, iso


def watch_careers(db: DB, errors: list[str]) -> list[Job]:
    """New links on watched career pages (dream companies without an API)."""
    out = []
    http = Http(pause=1.5)
    from .companies import watchlist
    pages_ = list(config.cfg("companies").get("watch", []) or [])
    pages_ += [{"company": w["company"], "url": w["url"], "include": [], "exclude": []}
               for w in watchlist() if w.get("type") == "page" and w.get("url")]
    for w in pages_:
        for link in pages.watch(db, http, w["url"], w.get("include", []), w.get("exclude", []), errors):
            j = Job(title=link.text[:160], company=w["company"], location="Karnataka (check)", url=link.href,
                    source="careers-page", description=link.context)
            j.dream = bool(w.get("dream")) or bool(is_dream(w["company"]))
            j.score = 55 + (15 if j.dream else 0)
            j.reasons = [f"★ new on {w['company']} careers page" if j.dream else f"new on {w['company']} careers page",
                         "location/eligibility not parsed: open the link"]
            j.flags = ["careers-page"] + (["dream"] if j.dream else [])
            out.append(j)
    return out


def save(db: DB, jobs: list[Job]) -> list[str]:
    """Score and store board/ATS jobs exactly as a scan does. Returns the ids new to us."""
    new_ids: list[str] = []
    for j in jobs:
        score(j)
        if db.upsert(j):
            db.set_score(j.id, j.score, j.reasons, j.flags, j.dream, j.status)
            new_ids.append(j.id)
        else:
            cur = db.get(j.id)
            if cur and cur["status"] in ("new", "filtered") and j.score > cur["score"]:
                db.set_score(j.id, j.score, j.reasons, j.flags, j.dream, j.status)
    return new_ids


def run(light: bool = False, notify_on: bool = True, only: set[str] | None = None,
        offline_ats: str | None = None, baseline_new: bool = False) -> dict:
    db = DB()
    s = config.cfg("search")
    errors: list[str] = []
    want = only or {"boards", "ats", "pages", "govt"}
    private: list[Job] = []
    t0 = dt.datetime.now()
    if "boards" in want:
        console.print("[bold]Job boards[/] (LinkedIn/Indeed/Naukri/Google)")
        private += boards.scan(errors, light=light)
    if "ats" in want and s.get("company_boards", {}).get("enabled", True):
        console.print("[bold]Company boards[/] (Greenhouse/Lever/Ashby/Workday)")
        private += ats.scan(errors, offline=offline_ats)
    found = len(private)
    new_ids = save(db, private)
    extra: list[Job] = []
    if "pages" in want:
        console.print("[bold]Watched career pages[/] (Birla …)")
        extra += watch_careers(db, errors)
    if "govt" in want and s.get("govt", {}).get("enabled", True):
        console.print("[bold]Government radar[/]")
        extra += govt.scan(db, errors, baseline_new=baseline_new)
    for j in extra:
        found += 1
        if db.upsert(j):
            db.set_score(j.id, j.score, j.reasons, j.flags, j.dream)
            new_ids.append(j.id)

    # ── alerts ─────────────────────────────────────────────────────────────
    sc = s.get("scoring", {})
    thr, dthr = sc.get("alert_threshold", 60), sc.get("dream_alert_threshold", 35)
    to_alert = []
    for jid in new_ids:
        j = db.get(jid)
        if not j or j["status"] != "new":
            continue
        if j["kind"] == "govt":
            if "eligibility:ineligible" not in j["flags"] and "tech" in j["flags"]:
                to_alert.append(j)
        elif j["score"] >= thr or (j["dream"] and j["score"] >= dthr):
            to_alert.append(j)
    to_alert.sort(key=lambda j: (not j["dream"], j["kind"] != "govt", -j["score"]))
    sent = 0
    if notify_on and to_alert:
        sent = notify.alert_jobs(to_alert[:25])
        for j in to_alert[:25]:
            db.set_status(j["id"], "alerted", "telegram")
    db.log_run("light" if light else "full", found, len(new_ids), sent, errors)
    secs = (dt.datetime.now() - t0).seconds
    console.print(f"\n[bold green]Done[/] in {secs}s · found {found} · new {len(new_ids)} · alerted {sent}"
                  f" · errors {len(errors)}")
    for e in errors[:12]:
        console.print(f"  [yellow]·[/] {e}")
    return {"found": found, "new": len(new_ids), "alerted": sent, "errors": errors, "at": iso()}


def digest(send: bool = True) -> str:
    db = DB()
    since = (dt.datetime.now() - dt.timedelta(days=1)).isoformat(timespec="seconds")
    jobs = db.query(status=["new", "alerted", "queued"], kind="private", min_score=40, since=since, limit=25)
    gov = db.query(kind="govt", since=since, limit=20)
    stats = {"new today": len(jobs) + len(gov), "applied total": db.count("status='applied'"),
             "waiting for you": db.count("status='needs_you'"), "interviews": db.count("status='interview'")}
    body = notify.digest_html(jobs, gov, stats)
    if send:
        if not notify.email(f"JobHunter: {stats['new today']} new · {stats['waiting for you']} waiting", body):
            head = "<b>Daily digest</b>\n" + " · ".join(f"{k} {v}" for k, v in stats.items())
            for msg in notify.pages(head, [notify.format_job(j, db) for j in (jobs + gov)[:8]]):
                notify.telegram(msg)
    return body
