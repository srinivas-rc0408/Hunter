"""`hunt`: the JobHunter command line. Run `hunt --help` or `hunt <command> --help`."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import webbrowser
from pathlib import Path

from . import __version__


def _console():
    from .util import console
    return console


# ── scan / radar ───────────────────────────────────────────────────────────
def cmd_scan(a):
    from . import pipeline
    only = set(a.only.split(",")) if a.only else None
    r = pipeline.run(light=a.light, notify_on=not a.no_notify, only=only, baseline_new=a.include_existing, dry=a.dry_run)
    if a.json:
        print(json.dumps(r))
    elif a.dry_run:
        from rich.table import Table
        t = Table(title="Dry run: jobs per source (nothing saved, nothing sent)")
        for c in ("source", "found", "new"):
            t.add_column(c, justify="left" if c == "source" else "right")
        for s, v in r["by_source"].items():
            t.add_row(s, str(v["found"]), str(v["new"]))
        t.add_row("[bold]total[/]", f"[bold]{r['found']}[/]", f"[bold]{r['new']}[/]")
        _console().print(t)


def cmd_govt(a):
    from . import pipeline
    pipeline.run(only={"govt"}, notify_on=not a.no_notify, baseline_new=a.include_existing)
    cmd_list(argparse.Namespace(status=None, kind="govt", min=0, limit=a.limit, all=True))


def cmd_digest(a):
    from . import pipeline
    pipeline.digest(send=not a.print)
    _console().print("digest sent" if not a.print else "printed")


# ── browse ─────────────────────────────────────────────────────────────────
def cmd_list(a):
    from rich.table import Table

    from .db import DB
    db = DB()
    st = a.status.split(",") if a.status else (None if a.all else ["new", "alerted", "queued", "needs_you"])
    rows = db.query(status=st, kind=a.kind, min_score=a.min, limit=a.limit)
    t = Table(show_lines=False, header_style="bold")
    for c in ("id", "score", "role", "company", "where", "status", "date"):
        t.add_column(c, overflow="fold", max_width={"role": 44, "company": 24, "where": 22}.get(c))
    for j in rows:
        t.add_row(j["id"][:8], str(j["score"]), ("★ " if j["dream"] else "") + ("🏛 " if j["kind"] == "govt" else "") + j["title"],
                  j["company"], j["location"] or "", j["status"], j.get("deadline") or j.get("posted_at") or j["found_at"][:10])
    _console().print(t)
    _console().print(f"[dim]{len(rows)} shown · `hunt show <id>` for details · `hunt apply <id>` to apply[/]")


def cmd_show(a):
    from .db import DB
    from .tailor import gap
    db = DB()
    j = db.get(a.id)
    if not j:
        sys.exit(f"no job {a.id}")
    c = _console()
    c.rule(f"{j['title']} · {j['company']}")
    c.print(f"[bold]{j['location']}[/] · score {j['score']} · {j['status']} · {', '.join(j['sources'])}")
    c.print(f"apply: {j['url']}")
    for u in j["urls"][1:]:
        c.print(f"  also: {u}")
    if j.get("deadline"):
        c.print(f"[bold red]last date {j['deadline']}[/]")
    for r in j["reasons"]:
        c.print(f"  • {r}")
    if j["kind"] == "private" and j.get("description"):
        g = gap(j["description"])
        c.print(f"\nkeyword match {g['match']}% · have: {', '.join(g['have'][:12])}")
        if g["missing"]:
            c.print(f"[yellow]missing (only add if true): {', '.join(g['missing'][:12])}[/]")
    c.print("\n" + (j.get("description") or "")[: a.chars])
    for e in db.events(j["id"]):
        c.print(f"[dim]{e['at']} {e['kind']} {e['detail']}[/]")


def cmd_mark(a):
    from .db import DB
    db = DB()
    j = db.get(a.id)
    if not j:
        sys.exit(f"no job {a.id}")
    db.set_status(j["id"], a.status, a.note or "manual")
    if a.note:
        db.note(j["id"], f"{dt.date.today()}: {a.note}")
    _console().print(f"{j['title']} · {j['company']} → {a.status}")


def cmd_dashboard(a):
    from .dashboard import build
    f = build()
    _console().print(f"dashboard: {f}")
    if a.open:
        webbrowser.open(Path(f).as_uri())


# ── apply ──────────────────────────────────────────────────────────────────
def cmd_apply(a):
    from .apply.runner import apply
    if a.sync:
        from .cli_sync import cmd_sync
        cmd_sync(argparse.Namespace(quiet=True))
    if a.auto:
        from . import config as _c
        _c.cfg("search").setdefault("apply", {})["mode"] = "basic_auto"
    apply(ids=a.ids or None, top=a.top, min_score=a.min, dry=a.dry_run, headless=a.headless or None,
          with_cover=not a.no_cover, approve=a.approve or a.submit, submit_now=a.submit)


def cmd_browser(a):
    from .apply.assist import browse
    browse(a.url)


def cmd_login(a):
    from .apply.runner import login
    login(a.site)


def cmd_cover(a):
    from .db import DB
    from .tailor import cover
    j = DB().get(a.id)
    if not j:
        sys.exit(f"no job {a.id}")
    text, rep, how = cover(j)
    print(text)
    _console().print(f"\n[dim]{how} · ready={rep['ready']} · " +
                     "; ".join(f"{c['check']}:{c['status']}" for c in rep["checks"]) + "[/]")


def cmd_resume(a):
    from . import resume_pdf
    if a.use:
        from .resumes import use
        ok, msg = use(Path(a.use).expanduser())
        _console().print(("[green]✓[/] " if ok else "[red]✗[/] ") + msg)
        return
    if a.show:
        from .apply.runner import my_resume
        r = my_resume()
        _console().print(f"applications upload: {r}" if r else "no config/resume.pdf yet: hunt resume --use FILE")
        return
    out = resume_pdf.build(Path(a.out) if a.out else resume_pdf.pdf_path(None))
    resume_pdf.write_base()
    _console().print(f"one-page master resume: {out}\nhunter-resume base: hunter/resumes/base.md")


def cmd_tailor(a):
    from . import resume_pdf
    from .db import DB
    db = DB()
    j = db.get(a.id)
    if not j:
        sys.exit(f"no job {a.id}")
    r = resume_pdf.tailor_job(j, use_llm=not a.no_llm)
    db.event(j["id"], "tailored", json.dumps({k: r[k] for k in ("before", "after", "how", "clean")}))
    c = _console()
    c.print(f"[bold]{j['title']} · {j['company']}[/]")
    c.print(f"ATS (hunter-resume formula): {r['before']} → [bold]{r['after']}[/] · {r['how']} · truth check "
            f"{'[green]CLEAN[/]' if r['clean'] else '[red]FAILED[/]'}")
    if r["reword"]:
        c.print("JD says it differently: " + "; ".join(r["reword"]))
    if r["missing"]:
        c.print(f"[yellow]JD asks, your resume doesn't show (add to resume.yaml facts ONLY if true): "
                f"{', '.join(r['missing'])}[/]")
    c.print(f"PDF: {r['pdf']}\nmarkdown: {r['md']}")


def cmd_review(a):
    from .review import serve
    serve(port=a.port, open_browser=a.open)


def cmd_morning(a):
    from .morning import run
    run(send=a.send, heartbeat=not a.offline)


def cmd_health(a):
    from . import health
    if a.unit_failed:                                   # systemd OnFailure= (jobhunter-alert@.service)
        return health.unit_failed(a.unit_failed)
    if a.watch:                                         # jobhunter-health.timer, every 30 min
        for m in health.watch():
            print("sent:", m.splitlines()[0])
        from .followup import suggest                   # 7 days, no reply: a follow-up draft for you to send
        print(f"follow-up suggestions sent: {suggest()}")
        return
    if a.weekly:                                        # jobhunter-weekly.timer, Sunday 21:00
        from .db import DB
        from .notify import telegram
        from .ops import tidy
        db = DB()
        try:
            print(f"tidy: {tidy(db)} old screen file(s) deleted, jobs.db vacuumed")
        except Exception as e:                          # a busy db must not cost the report
            print(f"tidy skipped: {e}")
        msg = health.weekly(db)
        print(msg if not a.send else ("sent" if telegram(msg) else "NOT sent (Telegram not set?)"))
        return
    c = _console()
    for ok, what, fix in health.checks():
        c.print(f"{'[green]✓[/]' if ok else '[red]✗[/]'} {what}" + ("" if ok else f"\n    fix: [bold]{fix}[/]"))


def cmd_bot(a):
    from .bot import run
    run()


def cmd_backup(a):
    from .ops import backup
    backup(keep=a.keep)


def cmd_restore(a):
    from .ops import restore
    restore(a.file)


def cmd_update(a):
    from .ops import update
    update(run_tests=a.test)


def main(argv=None):
    from .cli_company import cmd_company, cmd_questions, cmd_answers, cmd_classify
    from .cli_sync import cmd_sync, cmd_mail, cmd_sheet, cmd_secrets, cmd_rescore, cmd_dream, cmd_llm
    from .cli_setup import cmd_doctor, cmd_telegram_id, cmd_advantages, cmd_calendar, cmd_export_hunter, cmd_setup
    p = argparse.ArgumentParser(prog="hunt", description="JobHunter: find jobs early, apply fast, track everything.")
    p.add_argument("--version", action="version", version=f"jobhunter {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="scan every source, score, alert new matches")
    s.add_argument("--light", action="store_true", help="quick scan (subset of boards/locations)")
    s.add_argument("--only", help="comma list: boards,ats,intern,pages,govt")
    s.add_argument("--no-notify", action="store_true")
    s.add_argument("--include-existing", action="store_true", help="first run: treat notices already on pages as new")
    s.add_argument("--json", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="scan into a throwaway copy of the DB: shows what's found, saves and sends nothing")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("govt", help="government radar only, then list notices")
    s.add_argument("--no-notify", action="store_true")
    s.add_argument("--include-existing", action="store_true")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_govt)

    s = sub.add_parser("list", help="list jobs")
    s.add_argument("--status", help="comma list, e.g. new,alerted,needs_you")
    s.add_argument("--kind", choices=["private", "govt"])
    s.add_argument("--min", type=int, default=0)
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--all", action="store_true", help="any status")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="one job in full, with keyword gap")
    s.add_argument("id")
    s.add_argument("--chars", type=int, default=2500)
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("apply", help="open the browser and apply (best first)")
    s.add_argument("ids", nargs="*", help="job ids (8 chars is enough); default: the queue")
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--min", type=int, default=50, help="minimum score for the queue")
    s.add_argument("--dry-run", action="store_true", help="fill only, never submit, don't wait")
    s.add_argument("--headless", action="store_true")
    s.add_argument("--no-cover", action="store_true", help="skip cover-letter drafting")
    s.add_argument("--sync", action="store_true", help="pull the cloud radar's jobs first")
    s.add_argument("--approve", action="store_true",
                   help="you reviewed these jobs: allow drafted answers; the check panel still shows before sending")
    s.add_argument("--submit", action="store_true",
                   help="you reviewed these jobs (Review Center / Telegram): submit without the panel if nothing is missing")
    s.add_argument("--auto", action="store_true",
                   help="no check panel: auto-submit only all-basic forms, queue the rest (old basic_auto mode)")
    s.set_defaults(fn=cmd_apply)

    s = sub.add_parser("browser", help="the bot's browser: go to any form, press 🎯 Fill this form, check, submit")
    s.add_argument("url", nargs="?", help="page to open first (default: LinkedIn recommended jobs)")
    s.set_defaults(fn=cmd_browser)

    s = sub.add_parser("login", help="log in once to a site in the bot's browser")
    s.add_argument("site", choices=["linkedin", "naukri", "indeed", "internshala"])
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("mark", help="set a job's status (applied, interview, offer, rejected, skipped…)")
    s.add_argument("id")
    s.add_argument("status")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_mark)

    s = sub.add_parser("cover", help="draft + verify a cover letter for a job")
    s.add_argument("id")
    s.set_defaults(fn=cmd_cover)

    s = sub.add_parser("resume", help="your resume: --use FILE sets the PDF every application uploads")
    s.add_argument("--use", metavar="PDF", help="use this PDF for every application (old one kept in data/resume-history)")
    s.add_argument("--show", action="store_true", help="which file applications upload")
    s.add_argument("--out", help="(no --use) build the one-page master PDF here")
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("tailor", help="tailor your resume to a job (hunter-resume ATS score + truth check)")
    s.add_argument("id")
    s.add_argument("--no-llm", action="store_true", help="reorder only, no rewording")
    s.set_defaults(fn=cmd_tailor)

    s = sub.add_parser("review", help="Review Center at http://localhost:8765 (what the bot filled; approve & submit)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("dashboard", help="write data/dashboard.html")
    s.add_argument("--open", action="store_true")
    s.set_defaults(fn=cmd_dashboard)

    s = sub.add_parser("digest", help="send the daily email/Telegram digest")
    s.add_argument("--print", action="store_true", help="build without sending")
    s.set_defaults(fn=cmd_digest)

    s = sub.add_parser("sync", help="merge jobs found by the GitHub Actions radar")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("mail", help="sort application emails in Gmail (offer/interview/test/rejected/…) and update statuses")
    s.add_argument("--days", type=int, default=30, help="how far back to read (default 30)")
    s.add_argument("--no-label", action="store_true", help="don't add JobHunter/… labels in Gmail")
    s.add_argument("--reset", action="store_true", help="forget what was sorted and sort again")
    s.set_defaults(fn=cmd_mail)

    s = sub.add_parser("sheet", help="write the Excel tracker (~/Documents/JobHunter/Applications.xlsx)")
    s.add_argument("--open", action="store_true", help="open it after writing")
    s.add_argument("--png", metavar="DIR", help="also save a PNG of each sheet there (LibreOffice headless)")
    s.set_defaults(fn=cmd_sheet)

    s = sub.add_parser("advantages", help="your SC / Karnataka / GATE advantages as a checklist")
    s.set_defaults(fn=cmd_advantages)

    s = sub.add_parser("secrets", help="make STATE_KEY and set GitHub secrets for the cloud radar")
    s.add_argument("--push", action="store_true", help="set them with the GitHub CLI (gh)")
    s.set_defaults(fn=cmd_secrets)

    s = sub.add_parser("calendar", help="govt exam/notification calendar")
    s.set_defaults(fn=cmd_calendar)

    s = sub.add_parser("answers", help="show answers learned from your typing")
    s.set_defaults(fn=cmd_answers)

    s = sub.add_parser("classify", help="debug: how a form label gets answered")
    s.add_argument("label")
    s.add_argument("--type", default="text")
    s.add_argument("--placeholder", default="")
    s.set_defaults(fn=cmd_classify)

    s = sub.add_parser("export-hunter", help="write hunter/profile.json + pipeline.json for the hunter skills")
    s.add_argument("--min", type=int, default=40)
    s.set_defaults(fn=cmd_export_hunter)

    s = sub.add_parser("company", help="search a company → pick → all its listings (date, place, link) → apply/watch")
    s.add_argument("query", nargs="?")
    s.add_argument("--pick", type=int, help="take suggestion N without asking")
    s.add_argument("--all", action="store_true", help="show listings outside Karnataka too")
    s.add_argument("--no-boards", action="store_true", help="skip LinkedIn/Naukri/Indeed (faster)")
    s.add_argument("--refresh", action="store_true", help="re-discover the careers page/platform")
    s.add_argument("--offline", action="store_true", help="no Wikidata suggestions")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_company)

    s = sub.add_parser("questions", help="answer what application forms asked that isn't in your files")
    s.add_argument("--list", action="store_true")
    s.set_defaults(fn=cmd_questions)

    s = sub.add_parser("morning", help="morning brief: cloud health, best new jobs, approvals, govt deadlines, GATE")
    s.add_argument("--send", action="store_true", help="also send it to Telegram")
    s.add_argument("--offline", action="store_true", help="skip the cloud heartbeat check")
    s.set_defaults(fn=cmd_morning)

    s = sub.add_parser("bot", help="Telegram command bot (/top /govt /review /skip …): runs as a service")
    s.set_defaults(fn=cmd_bot)

    s = sub.add_parser("backup", help="encrypted backup of everything git doesn't hold → ~/jobhunter-backups")
    s.add_argument("--keep", type=int, default=8)
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("restore", help="restore an encrypted backup (needs STATE_KEY)")
    s.add_argument("file")
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("update", help="git pull + reinstall + refresh Chromium")
    s.add_argument("--test", action="store_true", help="also run the test suite")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("health", help="every check in plain words with the fix command (/health on Telegram)")
    s.add_argument("--watch", action="store_true", help="alert new problems on Telegram once, and their recovery")
    s.add_argument("--unit-failed", metavar="UNIT", help="called by systemd when a service fails")
    s.add_argument("--weekly", action="store_true", help="the Sunday report (print; --send to Telegram)")
    s.add_argument("--send", action="store_true")
    s.set_defaults(fn=cmd_health)

    s = sub.add_parser("doctor", help="check setup")
    s.add_argument("--test-alert", action="store_true")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("dream", help="dream companies: hunt dream add \"Zoho\" · remove · list")
    s.add_argument("action", choices=["add", "remove", "list"], nargs="?", default="list")
    s.add_argument("name", nargs="*")
    s.add_argument("--no-push", action="store_true", help="don't git push the change")
    s.set_defaults(fn=cmd_dream)

    s = sub.add_parser("rescore", help="re-score open jobs with the current rules")
    s.set_defaults(fn=cmd_rescore)

    s = sub.add_parser("llm", help="LLM chain: NVIDIA → Ollama → Gemini → Groq (--test checks every key)")
    s.add_argument("--test", action="store_true", help="send one tiny request per key + one through the chain")
    s.add_argument("--models", action="store_true", help="list the live chat models of each provider")
    s.set_defaults(fn=cmd_llm)

    s = sub.add_parser("telegram-id", help="print your Telegram chat id")
    s.set_defaults(fn=cmd_telegram_id)

    s = sub.add_parser("setup", help="first-run setup")
    s.add_argument("--browser", action="store_true", help="also install Playwright Chromium")
    s.set_defaults(fn=cmd_setup)

    a = p.parse_args(argv)
    import logging
    import os
    logging.basicConfig(level=(os.environ.get("JH_LOG") or "WARNING").upper(), format="%(levelname)s %(name)s: %(message)s")
    a.fn(a)


if __name__ == "__main__":
    main()
