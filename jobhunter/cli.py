"""`hunt`: the JobHunter command line. Run `hunt --help` or `hunt <command> --help`."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import __version__, config


def _console():
    from .util import console
    return console


# ── scan / radar ───────────────────────────────────────────────────────────
def cmd_scan(a):
    from . import pipeline
    only = set(a.only.split(",")) if a.only else None
    r = pipeline.run(light=a.light, notify_on=not a.no_notify, only=only, baseline_new=a.include_existing)
    if a.json:
        print(json.dumps(r))


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


def _ask(prompt: str, default: str = "") -> str:
    try:
        v = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"
    return v.strip() or default


def _print_listings(rows: list[dict], ka_only: bool, fit_only: bool, limit: int) -> list[dict]:
    from rich.table import Table
    shown = [r for r in rows if (not ka_only or r["karnataka"]) and (not fit_only or r["fit"])][:limit]
    t = Table(header_style="bold", show_lines=False)
    for col, w in (("#", 4), ("posted", 9), ("role", 46), ("where", 24), ("type", 10), ("via", 16), ("fit", 4),
                   ("status", 9)):
        t.add_column(col, max_width=w, overflow="fold")
    for i, r in enumerate(shown, 1):
        d = r.get("days_ago")
        when = ("🔥 today" if d == 0 else f"{d}d ago") if d is not None else "?"
        t.add_row(str(i), when, r["title"], (r.get("location") or "")[:40] + (" ✓KA" if r["karnataka"] else ""),
                  r.get("job_type") or "", ",".join(r["sources"])[:16], str(r["score"]) if r["fit"] else "—",
                  r.get("status") or "")
    _console().print(t)
    return shown


def cmd_company(a):
    """Type a company → choose → every listing with date, place, source and link → apply/watch."""
    from . import companies
    c = _console()
    q = a.query or _ask("Company name: ")
    if not q or q == "q":
        return
    sug = companies.suggest(q, online=not a.offline)
    if a.pick:
        choice = sug[min(a.pick, len(sug)) - 1]
    else:
        for i, s in enumerate(sug, 1):
            c.print(f"  [bold]{i:>2}[/]. {s['name']}  [dim]{s.get('hint') or ''} · {s['source']}[/]")
        n = _ask("Pick a number [1]: ", "1")
        if n == "q" or not n.isdigit() or not 1 <= int(n) <= len(sug):
            return
        choice = sug[int(n) - 1]
    c.print(f"[dim]Finding {choice['name']}'s careers page and hiring platform…[/]")
    info = companies.resolve(choice["name"], choice.get("domain"), choice.get("wikidata"), refresh=a.refresh)
    c.rule(f"[bold]{info['name']}[/]")
    meta = [x for x in [info.get("description"), info.get("website"), f"~{info['employees']} employees" if info.get("employees")
                        else None, f"founded {info['founded']}" if info.get("founded") else None] if x]
    if meta:
        c.print(" · ".join(meta))
    plats = ", ".join(f"{b['type']}:{b.get('token') or b.get('host')}" for b in info.get("boards", [])) or "none found"
    c.print(f"hiring platform: [bold]{plats}[/]" + (f" · careers page: {info['careers_url']}" if info.get("careers_url") else ""))
    for n_ in info.get("notes", []):
        c.print(f"[yellow]{n_}[/]")
    c.print("[dim]Collecting listings (company platform + LinkedIn/Naukri/Indeed + your database)…[/]")
    res = companies.listings(info, boards_search=not a.no_boards)
    rows = res["listings"]
    k = res["counts"]
    c.print(f"[bold]{k['total']}[/] listings · {k['karnataka']} in Karnataka/remote-India · {k['fit']} fit your rules · "
            f"{k['new_24h']} posted in the last 24 h")
    for e in res["errors"][:5]:
        c.print(f"[dim]  · {e}[/]")
    if a.json:
        print(json.dumps(res, default=str, ensure_ascii=False))
        return
    ka, fit, limit = not a.all, False, 60
    shown = _print_listings(rows, ka, fit, limit)
    help_ = ("[dim]commands: [bold]apply 1,3[/] (fill + submit basic) · [bold]preview 2[/] (fill only) · "
             "[bold]track 1-4[/] (add to queue) · [bold]show 3[/] · [bold]open 3[/] · [bold]watch[/] (include in every scan) · "
             "[bold]links[/] (referrals) · [bold]all[/]/[bold]ka[/]/[bold]fit[/] (filters) · [bold]q[/][/]")
    c.print(help_)

    def pick(spec: str) -> list[dict]:
        idx = set()
        for part in spec.replace(" ", "").split(","):
            if "-" in part:
                lo, hi = part.split("-", 1)
                if lo.isdigit() and hi.isdigit():
                    idx |= set(range(int(lo), int(hi) + 1))
            elif part.isdigit():
                idx.add(int(part))
        return [shown[i - 1] for i in sorted(idx) if 1 <= i <= len(shown)]

    while True:
        cmd = _ask("> ")
        if cmd in ("q", "quit", "exit"):
            break
        verb, _, arg = cmd.partition(" ")
        if verb in ("all", "ka", "fit"):
            ka, fit = verb == "ka", verb == "fit"
            shown = _print_listings(rows, ka, fit, limit)
        elif verb in ("apply", "preview", "track"):
            chosen = pick(arg)
            if not chosen:
                c.print("which numbers? e.g. apply 1,3 or track 2-5")
                continue
            ids = companies.track(chosen)
            c.print(f"added to your queue: {', '.join(i[:8] for i in ids)}")
            if verb != "track":
                from .apply.runner import apply
                apply(ids=ids, dry=verb == "preview", with_cover=True)
        elif verb in ("show", "open"):
            for r in pick(arg):
                if verb == "open":
                    webbrowser.open(r.get("apply_url") or r["url"])
                else:
                    c.rule(f"{r['title']} · {r['company']}")
                    c.print(f"{r.get('location')} · {r.get('job_type') or ''} · posted {r.get('posted') or '?'} · "
                            f"via {', '.join(r['sources'])}\napply: {r.get('apply_url') or r['url']}\n"
                            f"fit: {r['score'] if r['fit'] else 'outside your rules'} · {r.get('why') or ''}\n")
                    c.print((r.get("description") or "")[:1500])
        elif verb == "watch":
            c.print("added to config/watchlist.yaml: every scan now includes it" if companies.watch(info)
                    else "already watched (or no platform/careers page to watch)")
        elif verb == "links":
            for k_, v in companies.links(info).items():
                if v:
                    c.print(f"  {k_}: {v}")
        else:
            c.print(help_)


def cmd_questions(a):
    """Answer what forms asked that isn't in your files. Never guessed; asked once."""
    from .apply import questions
    c = _console()
    rows = questions.pending()
    if not rows:
        c.print("No open questions. ✅")
        return
    if a.list:
        for i, r in enumerate(rows, 1):
            c.print(f"{i:>2}. {r['label']}  [dim]({len(r['jobs'])} job(s) waiting"
                    + (f"; options: {', '.join(r['options'][:8])}" if r.get("options") else "") + ")[/]")
        return
    c.print(f"[bold]{len(rows)} question(s).[/] Answer only with true facts. Enter = skip, q = stop.\n")
    for r in rows:
        c.print(f"[bold]{r['label']}[/]  [dim]{len(r['jobs'])} job(s) waiting · seen on {', '.join(r.get('sites') or ['?'])}[/]")
        if r.get("options"):
            for i, o in enumerate(r["options"], 1):
                c.print(f"   {i}. {o}")
        v = _ask("   your answer: ")
        if v == "q":
            break
        if not v:
            continue
        if r.get("options") and v.isdigit() and 1 <= int(v) <= len(r["options"]):
            v = r["options"][int(v) - 1]
        try:
            res = questions.answer(r["key"], v)
            c.print(f"   [green]saved[/] → {res['value']!r}" + (f" · {len(res['requeued'])} job(s) back in the apply queue"
                                                              if res["requeued"] else ""))
        except (ValueError, KeyError) as exc:
            c.print(f"   [red]{exc}[/]")


def cmd_answers(a):
    from .apply.fields import load_bank
    bank = load_bank()
    for k, v in sorted(bank.items()):
        print(f"{v['label'][:60]:60} → {v['answer']}")
    print(f"\n{len(bank)} learned answers in data/answers.yaml (edit or delete lines freely)")


def cmd_classify(a):
    """Debug: how would a form label be answered?"""
    from .apply.fields import answer
    r = answer(a.label, a.type, a.placeholder)
    print(f"{r.key} → {r.value!r} ({r.basis}{', BASIC' if r.basic else ''}) {r.note}")


# ── sync with the cloud radar (GitHub Actions) ─────────────────────────────
def cmd_sync(a):
    """Pull the cloud radar's encrypted jobs.db and merge it into yours."""
    from .config import env
    from .db import DB
    home = config.home()
    key = env("STATE_KEY")
    quiet = getattr(a, "quiet", False)
    if not key:
        if not quiet:
            _console().print("[yellow]STATE_KEY missing in .env (run `hunt secrets`)[/]")
        return
    enc, tmp = config.data_dir() / "cloud.db.enc", config.data_dir() / "cloud.db"
    try:
        subprocess.run(["git", "-C", str(home), "fetch", "-q", "origin", "radar-state"], check=True, timeout=60)
        enc.write_bytes(subprocess.run(["git", "-C", str(home), "show", "origin/radar-state:jobs.db.enc"], check=True,
                                       capture_output=True, timeout=60).stdout)
        subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", "env:STATE_KEY",
                        "-in", str(enc), "-out", str(tmp)], check=True, timeout=60, capture_output=True,
                       env={**__import__("os").environ, "STATE_KEY": key})
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        if not quiet:
            _console().print(f"[yellow]no cloud state yet or wrong STATE_KEY ({exc})[/]")
        return
    added, updated = DB().merge_from(tmp)
    tmp.unlink(missing_ok=True)
    _console().print(f"synced from cloud radar: +{added} jobs, {updated} status updates")
    _mail_and_sheet(quiet=True)


def _mail_and_sheet(quiet: bool = True) -> None:
    """After every sync: read new application emails (if set up) and rewrite the Excel tracker."""
    from . import mail, tracker
    if all(mail.creds()):
        try:
            r = mail.sync(quiet=quiet)
            if r.get("error"):
                _console().print(f"[yellow]email check: {r['error']}[/]")
            elif r and not quiet:
                _console().print(r)
        except Exception as exc:
            _console().print(f"[yellow]email check failed: {type(exc).__name__}: {str(exc)[:150]}[/]")
    tracker.refresh()


def cmd_mail(a):
    from . import mail
    c = _console()
    if not all(mail.creds()):
        c.print("Set up once: Google Account → Security → 2-Step Verification → App passwords → create one "
                "named JobHunter.\nThen add to .env:\n  GMAIL_USER=you@gmail.com\n  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx")
        return
    if a.reset:
        from .db import DB
        db = DB(); mail.ensure(db); n = db.c.execute("DELETE FROM mail").rowcount
        db.c.execute("DELETE FROM mail_state"); db.c.commit()
        c.print(f"forgot {n} sorted emails: they'll be re-read and re-sorted now. To remove old Gmail labels, "
                "delete the 'JobHunter' label in Gmail (your emails stay).")
    r = mail.sync(days=a.days, label=not a.no_label, quiet=False)
    if r.get("error"):
        c.print(f"[red]✗ {r['error']}[/]")
        return
    c.print(r if r else "no new application emails")
    from .db import DB
    from .tracker import numbers
    n = numbers(DB())
    c.print(f"Applications: {n['Applied']} · waiting {n['Waiting']} · interviews {n['Interviews']} · offers {n['Offers']} · "
            f"rejected {n['Rejected']} · response rate {n['Response rate']} · interview rate {n['Interview rate']}")
    from rich.markup import escape
    import re as _re
    c.print(escape(_re.sub(r"<[^>]+>", "", mail.summary_text(DB(), 7))))


def cmd_sheet(a):
    from . import tracker
    p = tracker.write()
    _console().print(f"[green]✓[/] {p}")
    if a.png:
        for f in tracker.preview(Path(a.png).expanduser()):
            _console().print(f"[green]✓[/] {f}")
    if a.open:
        subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cmd_secrets(a):
    """Create STATE_KEY (saved to .env) and print/push the GitHub secrets the cloud radar needs."""
    import base64
    import secrets as _s

    from .config import env
    home = config.home()
    envf = home / ".env"
    if not envf.exists():
        shutil.copy(home / ".env.example", envf)
    key = env("STATE_KEY")
    if not key:
        key = _s.token_urlsafe(32)
        with envf.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# encrypts the cloud radar's state (also a GitHub secret)\nSTATE_KEY={key}\n")
        _console().print("STATE_KEY generated and saved to .env")
    priv = config.path("config", "private.yaml")
    if not priv.is_file():
        sys.exit("config/private.yaml missing: copy config/private.example.yaml and fill it")
    vals = {"STATE_KEY": key, "PRIVATE_YAML_B64": base64.b64encode(priv.read_bytes()).decode()}
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_USER", "SMTP_PASS", "DIGEST_TO"):
        if env(k):
            vals[k] = env(k)
    if a.push:
        if not shutil.which("gh"):
            sys.exit("GitHub CLI not found: `sudo pacman -S github-cli`, then `gh auth login`")
        for k, v in vals.items():
            subprocess.run(["gh", "secret", "set", k, "--body", v], cwd=str(home), check=True)
            _console().print(f"✓ secret {k}")
        _console().print("Done. Run the workflow once: `gh workflow run radar -f mode=full`")
    else:
        _console().print("Secrets to add at GitHub → repo → Settings → Secrets and variables → Actions:")
        for k in vals:
            _console().print(f"  {k}")
        _console().print("Easiest: `hunt secrets --push` (uses the GitHub CLI). Values aren't printed here on purpose.")


def cmd_rescore(a):
    """Re-score every open job with the current rules (after an update or a profile change)."""
    from .rescore import rescore
    r = rescore()
    c = _console()
    for t, co, why in r["dropped"][:40]:
        c.print(f"[red]✗[/] {t[:60]} · {co} → {why}")
    c.print(f"re-scored {r['total']} jobs; {r['changed']} scores changed, {len(r['dropped'])} dropped by the new rules")


def cmd_dream(a):
    from . import dreams
    c = _console()
    if a.action == "list" or not a.name:
        ds = dreams.names()
        c.print("★ dream companies: " + (", ".join(ds) if ds else "none") +
                "\nadd one: hunt dream add \"Company\"   remove: hunt dream remove \"Company\"")
        return
    name = " ".join(a.name)
    ok, msg = (dreams.add if a.action == "add" else dreams.remove)(name)
    c.print(("[green]✓[/] " if ok else "[yellow]·[/] ") + msg)
    if ok:
        for n in dreams.after_change(name, a.action == "add", push=not a.no_push):
            c.print("  " + n)


def cmd_llm(a):
    """Show the LLM chain; --test sends one tiny request through every key and model."""
    import time as _t

    from . import llm
    from rich.table import Table
    c = _console()
    rows = llm.status()
    t = Table(title="LLM chain (auto tries these in order)")
    for col in ("#", "provider", "model now", "fallbacks", "keys", "state"):
        t.add_column(col)
    for i, r in enumerate(rows, 1):
        t.add_row(str(i), r["prov"], r["model"] or "-", ", ".join(r["fallbacks"]) or "-",
                  str(r["keys"]) + (f" ({r['dead']} refused)" if r["dead"] else ""),
                  ("[green]ready[/] " if r["ok"] else "[red]off[/] ") + r["note"])
    c.print(t)
    if a.models:
        for p in llm._providers():
            c.print(f"[bold]{p['prov']}[/] live chat models (best first): " + ", ".join(llm.ranked(p["prov"], p["ids"])[:25]))
    if not a.test:
        c.print("[dim]hunt llm --test  checks every key with one tiny request (uses ~1 request per key)[/]")
        return
    for p in llm._providers():
        prov = p["prov"]
        model = p["models"][0]
        for k in (llm.keys(prov) or [None]):
            t0 = _t.time()
            c.print(f"[dim]… {prov} key {llm.mask(k)} ({model}), up to 30 s[/]")
            code, text = llm._post(p, model, k, "Reply with exactly: OK", "Say OK.", 0, 2000, timeout=30)
            if code in (404, 408, 410):            # retired or overloaded: find the first model that answers
                for alt in p["models"][1:]:
                    c2, t2 = llm._post(p, alt, k, "Reply with exactly: OK", "Say OK.", 0, 2000, timeout=30)
                    c.print(f"  [dim]{model}: {'timed out' if code == 408 else 'retired'}; {alt}: "
                            f"{'works' if c2 == 200 and t2 else c2}[/]")
                    if c2 == 200 and t2:
                        llm._remember(prov, alt, True)
                        code, text, model = c2, t2, alt
                        break
            dt = _t.time() - t0
            good = code == 200 and bool(text)
            why = {401: "key invalid", 403: "key refused / model not allowed", 404: "model not found",
                   410: "model retired (Gone)",
                   408: "timed out", 429: "rate-limited (wait a minute)", 0: "unreachable"}.get(code, f"HTTP {code}")
            c.print(f"{'[green]✓[/]' if good else '[red]✗[/]'} {prov} {model} key {llm.mask(k)}: "
                    + (f"{text[:20]!r} in {dt:.1f}s" if good else why))
    out = llm.chat("Reply in one short sentence.", "Name one skill an AI/ML intern needs.", max_tokens=1500)
    c.print(f"chain answer via {llm.LAST.get('prov', '-')} / {llm.LAST.get('model', '-')}: {out or 'none (templates will be used)'}")


# ── setup / health ─────────────────────────────────────────────────────────
PENDING_KEYS = ["location.pincode", "gender", "date_of_birth", "answers.expected_ctc_lpa",
                "answers.expected_stipend_month", "sc_subgroup", "marital_status"]


def cmd_doctor(a):
    from . import llm
    from .config import env, get
    c = _console()
    ok = lambda b: "[green]✓[/]" if b else "[red]✗[/]"
    prof = config.cfg("profile")
    c.rule("JobHunter doctor")
    c.print(f"{ok(sys.version_info >= (3, 10))} Python {sys.version.split()[0]}")
    for mod in ("jobspy", "playwright", "yaml", "bs4", "pdfplumber", "reportlab", "rich"):
        try:
            __import__(mod)
            c.print(f"{ok(True)} {mod}")
        except ImportError:
            c.print(f"{ok(False)} {mod}: pip install -r requirements.txt")
    missing = [k for k in PENDING_KEYS if get(prof, k) in (None, "")]
    c.print(f"{ok(not missing)} profile: " + ("complete" if not missing else "missing " + ", ".join(missing)))
    c.print(f"{ok(config.path('config', 'private.yaml').is_file())} config/private.yaml (personal facts, never committed)")
    resume_ok = config.path("config", "resume.pdf").is_file()
    filled = "TODO" not in config.path("config", "resume.yaml").read_text()
    c.print(f"{ok(resume_ok or filled)} resume: " + ("config/resume.pdf" if resume_ok else "") +
            (" + resume.yaml filled" if filled else " (resume.yaml still has TODOs: tailoring uses only filled bullets)"))
    ap = config.cfg("search").get("apply", {})
    if ap.get("resume", "own") == "own":
        from .apply.runner import my_resume
        r = my_resume()
        c.print(f"{ok(bool(r))} applications upload: " + (f"{r.name} (your own PDF)" if r else
                "nothing yet: hunt resume --use FILE, or send the PDF to the Telegram bot"))
    c.print(f"{ok(True)} apply mode: {ap.get('mode', 'check')}"
            + (" (you read each filled form and press Submit; fixes are learned)" if ap.get("mode", "check") == "check" else ""))
    c.print(f"{ok(bool(env('TELEGRAM_BOT_TOKEN') and env('TELEGRAM_CHAT_ID')))} Telegram alerts")
    c.print(f"{ok(bool(env('SMTP_USER') and env('SMTP_PASS')))} email digest")
    rows = llm.status()
    c.print(f"{ok(llm.available())} LLM (optional; tried in order, templates if none): " + (" → ".join(
        f"{r['prov']} " + (f"[green]{r['model']}[/]" + (f" ({r['keys']} keys)" if r['keys'] > 1 else "")
                           if r["ok"] else f"[dim]{r['note']}[/]") for r in rows) or "LLM_PROVIDER=none"))
    c.print(f"{ok(bool(env('STATE_KEY')))} STATE_KEY (cloud state + backups). Keep a copy off this laptop")
    c.print(f"{ok(bool(shutil.which('gh')))} GitHub CLI (gh) for secrets and workflow control")
    if shutil.which("systemctl"):
        for u in ("jobhunter-review.service", "jobhunter-bot.service", "jobhunter-sync.timer", "jobhunter-scan.timer",
                  "jobhunter-morning.timer", "jobhunter-backup.timer"):
            r = subprocess.run(["systemctl", "--user", "is-active", u], capture_output=True, text=True)
            c.print(f"{ok(r.stdout.strip() == 'active')} {u} ({r.stdout.strip() or 'missing'})"
                    + ("" if r.stdout.strip() == "active" else ": run scripts/install-services.sh"))
    from .morning import cloud_heartbeat
    hb, hrs = cloud_heartbeat()
    c.print(f"{ok(hrs is not None and hrs <= 1.5)} {hb}")
    from .ops import backup_dir
    bk = sorted(backup_dir().glob("jobhunter-*.tar.gz.enc"))
    c.print(f"{ok(bool(bk))} backups: {len(bk)} in {backup_dir()}" + (f" (newest {bk[-1].name})" if bk else
                                                                      ": run `hunt backup`"))
    prof_dir = config.path(config.cfg("search").get("apply", {}).get("browser_profile_dir", "data/browser-profile"))
    c.print(f"{ok(prof_dir.exists())} browser profile (run `hunt login linkedin` and `hunt login naukri`)")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            b.close()
        c.print(f"{ok(True)} Chromium for Playwright")
    except Exception as exc:
        c.print(f"{ok(False)} Chromium: run `python -m playwright install chromium` ({str(exc)[:60]})")
    if a.test_alert:
        from .notify import telegram
        c.print(f"{ok(telegram('✅ JobHunter is connected. Alerts will arrive here.'))} test Telegram message")


def cmd_telegram_id(a):
    import requests

    from .config import env
    tok = env("TELEGRAM_BOT_TOKEN")
    if not tok:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env first (from @BotFather).")
    r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=20).json()
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name") for u in r.get("result", []) if "message" in u}
    if not chats:
        sys.exit("Send any message to your bot in Telegram first, then run this again.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


def cmd_advantages(a):
    """Your advantages as a checklist (SC, Karnataka, Kannada, GATE)."""
    c = _console()
    prof = config.cfg("profile")
    c.rule("Your advantages: use every one")
    items = config.cfg("advantages").get("advantages") or config.cfg("govt").get("advantages", [])
    if not items:
        sys.exit("config/advantages.yaml not found")
    for i, x in enumerate(items, 1):
        c.print(f"[bold]{i:>2}. {x['title']}[/]")
        c.print(f"    {x['detail']}")
        c.print(f"    [green]→ {x['action']}[/]")
        c.print(f"    [dim]{x['status']}[/]")
    todo = []
    if not prof.get("sc_subgroup"):
        todo.append("set sc_subgroup (A/B/C) in config/private.yaml")
    if not (prof.get("education") or [{}])[0].get("percent_equivalent"):
        todo.append("get REVA's CGPA→% conversion certificate, then set percent_equivalent")
    if todo:
        c.print("\n[bold yellow]Open items:[/] " + "; ".join(todo))


def cmd_calendar(a):
    c = _console()
    today = dt.date.today()
    for e in sorted(config.cfg("govt").get("calendar", []), key=lambda e: str(e["date"])):
        d = e["date"] if isinstance(e["date"], dt.date) else dt.date.fromisoformat(str(e["date"]))
        if d < today - dt.timedelta(days=7):
            continue
        days = (d - today).days
        c.print(f"{d}  [{'red' if days < 15 else 'white'}]{days:>4}d[/]  {e['event']}  [dim]({e['status']})[/]")


def cmd_export_hunter(a):
    """Write hunter/profile.json + hunter/pipeline.json so the hunter-* skills share this data."""
    from .db import DB
    prof = config.cfg("profile")
    hd = config.home() / "hunter"
    hd.mkdir(exist_ok=True)
    edu = next((e for e in prof["education"] if e.get("level") == "bachelors"), {})
    e10 = next((e for e in prof["education"] if e.get("level") == "class10"), {})
    e12 = next((e for e in prof["education"] if e.get("level") == "class12"), {})
    ans = prof.get("answers", {})
    hp = {
        "name": prof["name"], "first_name": prof["first_name"], "email": prof["email"], "phone": prof["phone"],
        "location": f"{prof['location']['city']}, {prof['location']['state']}",
        "target_roles": prof["targets"]["roles"], "seniority": "entry", "years_experience": 0,
        "skills": [s for g in prof["skills"].values() for s in g],
        "locations": ["Bengaluru, KA", "Mysuru, KA", "Mangaluru, KA"], "remote": prof["targets"].get("remote_ok", True),
        "relocate": False, "exclude_titles": prof["targets"]["exclude_title_words"],
        "dream_companies": prof.get("dream_companies", []), "links": {
            "linkedin": prof["links"].get("linkedin"), "github": prof["links"].get("github"),
            "website": prof["links"].get("portfolio")},
        "work_authorization": {"country": "IN", "authorized": True, "needs_sponsorship": False},
        "salary_floor": prof["targets"].get("min_ctc_inr_year"), "currency": "INR",
        "resume_path": "config/resume.pdf",
        "current": {"company": None, "title": "B.Tech student", "since": "2023-08"},
        "education": [{"school": edu.get("institution"), "degree": f"{edu.get('degree')} {edu.get('branch')}",
                       "year": edu.get("end"), "cgpa": edu.get("cgpa"), "tenth_percent": e10.get("percent"),
                       "twelfth_percent": e12.get("percent"), "backlogs": edu.get("backlogs")}],
        "answers": {"start_date": ans.get("earliest_joining"), "notice_period": ans.get("notice_period"),
                    "relocation": ans.get("willing_to_relocate"), "onsite": ans.get("work_from_office"),
                    "how_did_you_hear": ans.get("how_did_you_hear"), "previously_employed": ans.get("previously_employed_here"),
                    "current_ctc": ans.get("current_ctc_text"), "salary_expectation": ans.get("expected_ctc"),
                    "why_this_company": None, "why_leaving": None, "referral": None, "pronouns": None},
        "self_identification": {"_note": "Voluntary; never answered for you.", "gender": None,
                                "race_ethnicity": None, "veteran_status": None, "disability_status": None},
    }
    (hd / "profile.json").write_text(json.dumps(hp, indent=2, ensure_ascii=False), encoding="utf-8")
    stage = {"applied": "applied", "interview": "interview", "offer": "offer", "rejected": "rejected",
             "needs_you": "saved", "alerted": "saved", "new": "saved", "queued": "saved", "filled": "saved"}
    db = DB()
    roles = []
    for j in db.query(status=list(stage), kind="private", min_score=a.min, limit=500):
        applied = next((e["at"][:10] for e in db.events(j["id"]) if e["kind"] == "applied"), None)
        st = stage[j["status"]]
        if st in ("applied", "interview", "offer") and not applied:
            applied = (j.get("status_at") or j["found_at"])[:10]
        roles.append({"id": j["id"], "company": j["company"], "title": j["title"], "url": j["url"], "stage": st,
                      "applied": applied, "next_step": None, "next_date": None,
                      "notes": f"{j['location']}. Fit {j['score']} from jobhunter."})
    pf = hd / "pipeline.json"
    old = json.loads(pf.read_text()) if pf.is_file() else {"roles": [], "contacts": [], "threads": []}
    keep = {r["id"]: r for r in old.get("roles", [])}
    for r in roles:
        keep[r["id"]] = {**keep.get(r["id"], {}), **{k: v for k, v in r.items() if v is not None or k not in keep.get(r["id"], {})}}
    old["roles"] = list(keep.values())
    pf.write_text(json.dumps(old, indent=2, ensure_ascii=False), encoding="utf-8")
    _console().print(f"hunter/profile.json + hunter/pipeline.json ({len(old['roles'])} roles) written")


def cmd_setup(a):
    """First-run helper: copies .env, checks Chromium, prints what's left."""
    c = _console()
    home = config.home()
    env = home / ".env"
    if not env.exists():
        shutil.copy(home / ".env.example", env)
        c.print("created .env from .env.example: add your Telegram token there")
    if a.browser:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    cmd_doctor(argparse.Namespace(test_alert=False))
    c.print("\nNext: `hunt login linkedin` · `hunt login naukri` · `hunt scan` · `hunt apply --dry-run`")


def main(argv=None):
    p = argparse.ArgumentParser(prog="hunt", description="JobHunter: find jobs early, apply fast, track everything.")
    p.add_argument("--version", action="version", version=f"jobhunter {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="scan every source, score, alert new matches")
    s.add_argument("--light", action="store_true", help="quick scan (subset of boards/locations)")
    s.add_argument("--only", help="comma list: boards,ats,intern,pages,govt")
    s.add_argument("--no-notify", action="store_true")
    s.add_argument("--include-existing", action="store_true", help="first run: treat notices already on pages as new")
    s.add_argument("--json", action="store_true")
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
    a.fn(a)


if __name__ == "__main__":
    main()
