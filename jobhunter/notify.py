"""Alerts: Telegram (instant, on your phone) and email (daily digest).

Set in .env (laptop) or as GitHub Actions secrets (cloud):
  TELEGRAM_BOT_TOKEN   from @BotFather
  TELEGRAM_CHAT_ID     your chat id (run `hunt telegram-id` after messaging the bot)
  SMTP_USER / SMTP_PASS   Gmail address + 16-char App Password
  DIGEST_TO            where the digest goes (defaults to SMTP_USER)
Nothing is required: without them, alerts print to the console.
"""
from __future__ import annotations

import html
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from .config import env
from .util import clip, console


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def format_job(j: dict) -> str:
    star = "★ " if j.get("dream") else ""
    gov = "🏛 " if j.get("kind") == "govt" else ""
    pay = ""
    if j.get("pay_min"):
        per = j.get("pay_period") or ""
        pay = f" · ₹{j['pay_min']:,.0f}{('/' + per) if per else ''}"
    lines = [f"{gov}{star}<b>{_esc(j['title'])}</b>",
             f"{_esc(j['company'])} · {_esc(j.get('location'))}{pay} · score <b>{j.get('score', 0)}</b>"]
    if j.get("deadline"):
        lines.append(f"⏰ last date {_esc(j['deadline'])}")
    for r in (j.get("reasons") or [])[:3]:
        lines.append("• " + _esc(clip(r, 110)))
    if j.get("url"):
        lines.append(f'<a href="{_esc(j["url"])}">Open</a> · /show_{j["id"][:8]} · /skip_{j["id"][:8]}')
    return "\n".join(lines)


def telegram(text: str) -> bool:
    tok, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=20)
        return r.ok
    except requests.RequestException:
        return False


def alert_jobs(jobs: list[dict]) -> int:
    """One Telegram message per job (so each is tappable). Returns count sent."""
    sent = 0
    for j in jobs:
        msg = format_job(j)
        if telegram(msg):
            sent += 1
        else:
            console.print("[bold green]ALERT[/] " + html.unescape(msg.replace("<b>", "").replace("</b>", "")
                                                             .replace("<code>", "").replace("</code>", "")))
            sent += 1
    return sent


def email(subject: str, html_body: str, to: str | None = None) -> bool:
    user, pw = env("SMTP_USER"), env("SMTP_PASS")
    to = to or env("DIGEST_TO") or user
    if not user or not pw or not to:
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL(env("SMTP_HOST", "smtp.gmail.com"), int(env("SMTP_PORT", "465")), timeout=30) as s:
            s.login(user, pw)
            s.sendmail(user, [to], msg.as_string())
        return True
    except (smtplib.SMTPException, OSError) as exc:
        console.print(f"[red]email failed: {exc}[/]")
        return False


def digest_html(jobs: list[dict], govt: list[dict], stats: dict) -> str:
    def rows(items):
        out = []
        for j in items:
            out.append(
                f"<tr><td>{j.get('score', 0)}</td><td>{'★ ' if j.get('dream') else ''}<a href='{_esc(j.get('url'))}'>"
                f"{_esc(j['title'])}</a></td><td>{_esc(j['company'])}</td><td>{_esc(j.get('location'))}</td>"
                f"<td>{_esc(j.get('deadline') or j.get('posted_at') or '')}</td><td><code>{j['id'][:8]}</code></td></tr>")
        return "".join(out)
    head = "<tr><th>Score</th><th>Role</th><th>Company</th><th>Where</th><th>Date</th><th>id</th></tr>"
    st = " · ".join(f"{k}: {v}" for k, v in stats.items())
    return f"""<div style="font-family:system-ui,sans-serif;font-size:14px">
<h2 style="margin:0 0 4px">JobHunter digest</h2><p style="color:#555">{_esc(st)}</p>
<h3>Top new jobs</h3><table cellpadding="6" style="border-collapse:collapse" border="1">{head}{rows(jobs)}</table>
<h3>Government notices</h3><table cellpadding="6" style="border-collapse:collapse" border="1">{head}{rows(govt)}</table>
<p style="color:#555">Apply from your laptop: <code>hunt apply --top 10</code></p></div>"""
