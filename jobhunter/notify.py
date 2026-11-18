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


def apply_link(j: dict) -> str:
    """One tap from Telegram to the job's apply page ("" when there's no link)."""
    return f'<a href="{html.escape(j["url"], quote=True)}">Apply</a>' if j.get("url") else ""


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
        lines.append(f'{apply_link(j)} · /show_{j["id"][:8]} · /skip_{j["id"][:8]}')
    return "\n".join(lines)


def telegram(text: str) -> bool:
    """Send as HTML; if Telegram rejects the markup (or it was cut mid-tag), resend it as plain text."""
    tok, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return False
    import re
    for payload in ({"parse_mode": "HTML", "text": text[:4000]},
                    {"text": html.unescape(re.sub(r"<[^>]+>", "", text))[:4000]}):
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                              json={"chat_id": chat, "disable_web_page_preview": True, **payload}, timeout=20)
            if r.ok:
                return True
        except requests.RequestException:
            pass
    return False


SRC_WORDS = {"profile": "your details", "consent": "consent", "learned": "learned from you", "you": "you changed it",
             "drafted": "AI draft: read it", "none": "EMPTY"}


def form_lines(fields: list[dict], limit: int = 40) -> list[str]:
    """Numbered, plain-words list of what was filled: '3. Notice period → Immediate (your details)'."""
    out = []
    for i, f in enumerate(fields or [], 1):
        if i > limit:
            out.append(f"… and {len(fields) - limit} more (Review Center)")
            break
        v = f.get("value")
        v = "✓ ticked" if v is True else "unticked" if v is False else ("—" if v in (None, "") else clip(str(v), 70))
        src = SRC_WORDS.get(f.get("basis") or "none", f.get("basis") or "")
        if f.get("basis") == "none" and not f.get("required"):
            src = "optional, empty"
        warn = "❗" if (f.get("required") and (f.get("basis") or "none") == "none") else ""
        out.append(f"{i}. {warn}{_esc(clip(str(f.get('label') or f.get('key') or '?'), 60))} → <b>{_esc(v)}</b> <i>({_esc(src)})</i>")
    return out


def telegram_photo(path, caption: str = "") -> bool:
    tok, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not tok or not chat or not path:
        return False
    try:
        with open(path, "rb") as fh:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendPhoto", timeout=60,
                              data={"chat_id": chat, "caption": caption[:1000]}, files={"photo": fh})
        if not r.ok:          # very tall full-page shots are refused as photos: send as a file
            with open(path, "rb") as fh:
                r = requests.post(f"https://api.telegram.org/bot{tok}/sendDocument", timeout=60,
                                  data={"chat_id": chat, "caption": caption[:1000]}, files={"document": fh})
        return r.ok
    except (OSError, requests.RequestException):
        return False


def review_needed(job: dict, why: str, fields: list[dict], shot=None) -> None:
    """The bot stopped on a form while you were away: what it filled, what's missing, the screenshot."""
    jid = job["id"][:8]
    head = (f"✋ <b>Check needed</b>: {_esc(job['title'])} · {_esc(job['company'])}\n<i>{_esc(clip(why, 200))}</i>\n")
    body = "\n".join(form_lines(fields, 25)) or "No form fields were filled."
    tail = (f"\n\nFix a field: <code>/fix_{jid} 3 your answer</code>\n"
            f"Send as shown: /approve_{jid} · open on laptop to check: /check_{jid} · /skip_{jid}"
            + (f"\nOr on your phone: {apply_link(job)}" if job.get("url") else ""))
    telegram(head + body + tail)
    try:
        if shot and str(shot) and __import__("os").path.exists(str(shot)):
            telegram_photo(shot, f"{job['title']} · {job['company']}: the filled form")
    except OSError:
        pass


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
