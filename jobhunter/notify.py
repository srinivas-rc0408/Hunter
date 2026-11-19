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
import re
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from .config import env
from .util import clip, console, parse_iso


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def apply_link(j: dict) -> str:
    """One tap from Telegram to the job's apply page: the company's own page when we know it ("" if no link)."""
    from .db import _url_rank
    urls = [u for u in [j.get("url"), *(j.get("urls") or [])] if u]
    if not urls:
        return ""
    u = max(urls, key=_url_rank)                     # stable: the first of equally good links wins
    host = re.sub(r"^https?://", "", u.lower()).split("/")[0]
    where = ("LinkedIn" if "linkedin." in host else "Naukri" if "naukri." in host else "Indeed" if "indeed." in host
             else "Glassdoor" if "glassdoor." in host else "company site")
    return f'<a href="{html.escape(u, quote=True)}">Apply on {where}</a>'


CUR = {"INR": "₹", "USD": "$", "CAD": "C$", "AUD": "A$", "SGD": "S$", "EUR": "€", "GBP": "£"}


def _num(v: float) -> str:
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def pay_text(j: dict) -> str:
    """Pay exactly as the source listed it (INR a year in LPA, the unit Indian postings use), else "not listed"."""
    lo, hi = j.get("pay_min"), j.get("pay_max")
    if not lo and not hi:
        return "not listed"
    lo, hi = lo or hi, hi or lo
    cur, per = (j.get("currency") or "").upper(), (j.get("pay_period") or "").lower()
    sym = CUR.get(cur, f"{cur} " if cur else "")
    if cur == "INR" and per == "year":
        lo, hi, unit = lo / 100000, hi / 100000, " LPA"
    else:
        unit = f"/{per}" if per else ""
    return f"{sym}{_num(lo)}{'' if hi == lo else '–' + _num(hi)}{unit}"


_REMOTE = re.compile(r"\bremote\b|work from home|\bwfh\b", re.I)
_HYBRID_LOC, _ONSITE_LOC = re.compile(r"\bhybrid\b", re.I), re.compile(r"\bon-?site\b|in-office|work from office|\bwfo\b", re.I)
_HYBRID_DESC = re.compile(r"\bhybrid (?:work|working|mode|role|model|setup|position|job|arrangement|schedule)\b|"
                          r"work(?:ing)? (?:mode|model|type)\s*[:\-–]\s*hybrid|\(hybrid\)", re.I)
_ONSITE_DESC = re.compile(r"\b(?:on-?site|in-office) (?:role|position|job|work|opportunity)\b|work from office|\bwfo\b|"
                          r"work(?:ing)? (?:mode|model|type)\s*[:\-–]\s*(?:on-?site|office)|\(on-?site\)", re.I)
_REMOTE_DESC = re.compile(r"\b(?:fully|100%) remote\b|\bremote (?:role|position|job|opportunity)\b|"
                          r"work(?:ing)? (?:mode|model|type)\s*[:\-–]\s*remote", re.I)


def mode_text(j: dict) -> str | None:
    """On-site / Hybrid / Remote only when the posting says so (location first, then the description). Else None."""
    loc, desc = j.get("location") or "", j.get("description") or ""
    for rx, mode in ((_HYBRID_LOC, "Hybrid"), (_REMOTE, "Remote"), (_ONSITE_LOC, "On-site")):
        if rx.search(loc):
            return mode
    if j.get("is_remote"):
        return "Remote"
    for rx, mode in ((_HYBRID_DESC, "Hybrid"), (_ONSITE_DESC, "On-site"), (_REMOTE_DESC, "Remote")):
        if rx.search(desc):
            return mode
    return None


def _clock(d) -> str:
    return f"{d:%I:%M}".lstrip("0") + (" am" if d.hour < 12 else " pm")


def _day(d, year: bool = True) -> str:
    return f"{d.day} {d:%b}" + (f" {d.year}" if year else "")


def when_text(j: dict) -> str:
    """'Posted 17 Nov 2026 · found 18 Nov, 8:34 am IST': the posted time only when the source gave one."""
    p = j.get("posted_at") or ""
    try:
        if "T" in p or " " in p.strip():
            d = parse_iso(p)
            if d is None:
                raise ValueError(p)
            posted = f"Posted {_day(d)}, {_clock(d)} IST"
        else:
            posted = f"Posted {_day(date.fromisoformat(p[:10]))}" if p else "Posted date not listed"
    except ValueError:
        posted = f"Posted {_esc(p)}"
    f = parse_iso(j.get("found_at"))
    return f"{posted} · found {_day(f, year=False)}, {_clock(f)} IST" if f else posted


def job_card(j: dict, db=None) -> str:
    """One job, whole, the same everywhere (/top, morning brief, alerts)."""
    jid = j["id"][:8]
    mode = mode_text(j)
    lines = [f"{'🏛 ' if j.get('kind') == 'govt' else ''}{'★ ' if j.get('dream') else ''}<b>{_esc(j['title'])}</b>"
             f" · {_esc(j['company'])} · score {j.get('score', 0)}",
             f"📍 {_esc(j.get('location')) or 'location not listed'} ({mode or 'work mode not listed'})",
             f"💰 {_esc(pay_text(j))}",
             f"🕒 {when_text(j)}"]
    if j.get("deadline"):
        try:
            lines.append(f"⏰ last date {_day(date.fromisoformat(j['deadline'][:10]))}")
        except ValueError:
            lines.append(f"⏰ last date {_esc(j['deadline'])}")
    link = apply_link(j)
    lines.append(f"🔗 {link}" if link else "🔗 no apply link listed")
    lines.append(f"/check_{jid} · /skip_{jid}")
    on = db.applied_on(j["id"]) if db is not None else None
    if on:
        lines.append(f"✅ Applied on {_day(parse_iso(on))}")
    return "\n".join(lines)


def pages(head: str, blocks: list[str], more: str = "", limit: int = 3800) -> list[str]:
    """Pack whole blocks into Telegram-sized messages: a job is never cut in half."""
    out, cur = [], head
    for b in blocks:
        if len(cur) + 2 + len(b) > limit and cur != head:
            out.append(cur)
            cur = head
        cur += "\n\n" + b
    out.append(cur)
    return [p + (more if i < len(out) - 1 else "") for i, p in enumerate(out)] if more else out


def format_job(j: dict, db=None) -> str:
    """A job alert: the card, plus why it matched."""
    why = "\n".join("• " + _esc(clip(r, 110)) for r in (j.get("reasons") or [])[:2])
    return job_card(j, db) + ("\n" + why if why else "")


def telegram(text: str) -> bool:
    """Send as HTML; if Telegram rejects the markup (or it was cut mid-tag), resend it as plain text."""
    tok, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return False
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
