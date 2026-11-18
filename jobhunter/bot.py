"""Telegram command bot: triage jobs from your phone (`hunt bot`, runs as a service).

Only answers YOUR chat (TELEGRAM_CHAT_ID); everyone else is ignored.
Commands (also tappable from alerts as /show_<id>, /skip_<id>):
  /brief            the morning brief now
  /top [n]          best open jobs (default 10)
  /govt             govt notices with deadlines
  /review           forms waiting for your approval
  /stats            pipeline numbers
  /show <id>        one job: why it matched, link, eligibility
  /skip <id>        hide it          /queue <id>   put it at the front of the apply queue
  /applied <id>     /interview <id>  /rejected <id>  /offer <id>   update status
  /scan             start a scan on the laptop now
  /help
Nothing here submits an application: applying stays in `hunt apply` / the Review Center.
"""
from __future__ import annotations

import html
import re
import subprocess
import sys
import time

import requests

from . import config
from .config import env
from .db import DB

STATUS_CMDS = {"skip": "skipped", "applied": "applied", "interview": "interview", "rejected": "rejected",
               "offer": "offer", "queue": "queued"}


def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _line(j: dict) -> str:
    return (f"{'★ ' if j.get('dream') else ''}{'🏛 ' if j.get('kind') == 'govt' else ''}<b>{j['score']}</b> "
            f"{_e(j['title'][:80])} · {_e(j['company'])}" + (f" · ⏰ {j['deadline']}" if j.get("deadline") else "")
            + f" /show_{j['id'][:8]}")


def handle(text: str, db: DB | None = None) -> str:
    """Pure command handler (unit-tested): text in, HTML reply out."""
    db = db or DB()
    t = (text or "").strip()
    m = re.match(r"^/(\w+?)(?:_([0-9a-f]{6,16}))?(?:@\w+)?(?:\s+(.*))?$", t)
    if not m:
        return "Send /help for commands."
    cmd, inline_id, arg = m.group(1).lower(), m.group(2), (m.group(3) or "").strip()
    jid = inline_id or (arg.split()[0] if arg else "")
    if cmd in ("start", "help"):
        return "<b>JobHunter bot</b>\n" + "\n".join(l.strip() for l in __doc__.splitlines()[4:17])
    if cmd == "brief":
        from .morning import as_telegram, build
        return as_telegram(build(db=db, heartbeat=False))
    if cmd == "top":
        n = int(arg) if arg.isdigit() else 10
        jobs = db.query(status=["new", "alerted", "queued"], kind="private", min_score=40, limit=n * 2)
        jobs.sort(key=lambda j: (j["status"] != "queued", not j["dream"], -j["score"]))
        return "<b>Top open jobs</b>\n" + ("\n".join(_line(j) for j in jobs[:n]) or "none: run /scan")
    if cmd == "govt":
        jobs = [j for j in db.query(kind="govt", limit=300) if j["status"] not in ("skipped", "applied")
                and "eligibility:ineligible" not in (j.get("flags") or [])]
        jobs.sort(key=lambda j: j.get("deadline") or "9999")
        return "<b>Govt notices</b>\n" + ("\n".join(_line(j) for j in jobs[:12]) or "none yet")
    if cmd == "review":
        jobs = db.query(status=["needs_you"], limit=20)
        return (f"<b>Waiting for approval: {len(jobs)}</b>\n" + "\n".join(_line(j) for j in jobs)
                + "\nApprove them at localhost:8765 on your laptop.") if jobs else "Nothing waiting. ✅"
    if cmd == "stats":
        n = {k: db.count(w) for k, w in {
            "tracked": "1=1", "open": "status IN ('new','alerted','queued')", "waiting": "status='needs_you'",
            "applied": "status='applied'", "interviews": "status='interview'", "offers": "status='offer'",
            "rejected": "status='rejected'"}.items()}
        return (f"<b>Pipeline</b>\ntracked {n['tracked']} · open {n['open']} · waiting {n['waiting']}\n"
                f"applied {n['applied']} · interviews {n['interviews']} · offers {n['offers']} · "
                f"rejected {n['rejected']}\napplied today {db.applied_today()}")
    if cmd == "show":
        j = db.get(jid) if jid else None
        if not j:
            return "Usage: /show &lt;id&gt;"
        rs = "\n".join("• " + _e(r[:140]) for r in (j.get("reasons") or [])[:8])
        return (f"{_line(j)}\n{_e(j.get('location'))} · status <b>{_e(j['status'])}</b>\n{rs}\n"
                f"<a href=\"{_e(j['url'])}\">Open posting</a>\n/skip_{j['id'][:8]} · /queue_{j['id'][:8]}")
    if cmd in STATUS_CMDS:
        j = db.get(jid) if jid else None
        if not j:
            return f"Usage: /{cmd} &lt;id&gt;"
        db.set_status(j["id"], STATUS_CMDS[cmd], "telegram")
        return f"✓ {_e(j['title'][:60])} · {_e(j['company'])} → <b>{STATUS_CMDS[cmd]}</b>"
    if cmd == "scan":
        subprocess.Popen([sys.executable, "-m", "jobhunter.cli", "scan"], cwd=str(config.home()),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return "Scan started on the laptop. New matches will arrive as alerts."
    return "Unknown command. /help"


def run() -> None:
    tok, chat = env("TELEGRAM_BOT_TOKEN"), str(env("TELEGRAM_CHAT_ID") or "")
    if not tok or not chat:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first.")
    api = f"https://api.telegram.org/bot{tok}"
    from .morning import load_state, save_state
    offset = load_state().get("tg_offset", 0)
    try:   # command menu in Telegram's "/" list
        requests.post(f"{api}/setMyCommands", timeout=20, json={"commands": [
            {"command": c, "description": d} for c, d in [
                ("brief", "morning brief now"), ("top", "best open jobs"), ("govt", "govt notices + deadlines"),
                ("review", "forms waiting for approval"), ("stats", "pipeline numbers"), ("scan", "scan now"),
                ("help", "all commands")]]})
    except requests.RequestException:
        pass
    print("JobHunter bot running (Ctrl+C to stop)")
    while True:
        try:
            r = requests.get(f"{api}/getUpdates", params={"offset": offset, "timeout": 50}, timeout=60).json()
        except (requests.RequestException, ValueError):
            time.sleep(10)
            continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            save_state(tg_offset=offset)
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != chat:
                continue                     # not you: ignore silently
            try:
                reply = handle(msg.get("text", ""))
            except Exception as exc:         # never let one bad command kill the bot
                reply = f"error: {_e(type(exc).__name__)}"
            try:
                requests.post(f"{api}/sendMessage", timeout=20, json={
                    "chat_id": chat, "text": reply[:4000], "parse_mode": "HTML", "disable_web_page_preview": True})
            except requests.RequestException:
                pass
