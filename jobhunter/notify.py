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


def _inr(v: float) -> str:
    """Indian grouping: 600000 → 6,00,000."""
    s = str(int(round(v)))
    return s if len(s) <= 3 else re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", s[:-3]) + "," + s[-3:]


def _lpa(v: float) -> str:
    return f"{v / 1e5:.1f}".removesuffix(".0")                 # 1 decimal max, no trailing .0


def _span(lo, hi, f, pre: str, post: str) -> str:
    """'₹10–16 LPA', one number when min = max, 'from ₹X' when only a minimum, 'up to ₹X' when only a maximum."""
    if lo and hi:
        a, b = f(lo), f(hi)
        return f"{pre}{a}{post}" if a == b else f"{pre}{a}–{b}{post}"
    return f"from {pre}{f(lo)}{post}" if lo else f"up to {pre}{f(hi)}{post}"


def pay_text(j: dict) -> str:
    """Pay as the posting listed it: '₹10–16 LPA', '₹30,000/month', '$20–100/hour (not INR)', 'from ₹6 LPA',
    an internship's stipend per month, '≈ … (estimate by Indeed)' when the board guessed it, else 'not listed'."""
    lo, hi = j.get("pay_min") or None, j.get("pay_max") or None
    if not lo and not hi:
        return "not listed"
    cur, per = (j.get("currency") or "").upper(), (j.get("pay_period") or "").lower()
    intern = j.get("job_type") == "internship" or re.search(r"\bintern", j.get("title") or "", re.I)
    top = hi or lo
    if cur == "INR" and per == "year" and top < 100000:          # "60,000-70,000 P.A." for an engineer
        text = _span(lo, hi, _inr, "₹", "/year") + " as listed (looks like per month: check the posting)"
    elif cur == "INR" and per == "year" and intern:
        text = (_span(lo and lo / 12, hi and hi / 12, _inr, "₹", "/month") +
                f" ({_span(lo, hi, _lpa, '₹', ' LPA')} listed)")
    elif cur == "INR" and per == "year":
        text = _span(lo, hi, _lpa, "₹", " LPA")
    elif cur == "INR":
        text = _span(lo, hi, _inr, "₹", f"/{per}" if per else "")
        if per == "month" and (lo or hi) > 500000:
            text += " as listed (looks like per year: check the posting)"
    elif cur:
        text = _span(lo, hi, _num, CUR.get(cur, f"{cur} "), f"/{per}" if per else "") + " (not INR)"
    else:
        text = _span(lo, hi, _num, "", f"/{per}" if per else "") + " (currency not listed)"
    return f"≈ {text} (estimate by {j['pay_estimate']})" if j.get("pay_estimate") else text


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
    from .companies import is_agency
    if is_agency(j.get("company")):
        lines.append("⚠ agency: check the real employer")
    if "scam?" in (j.get("flags") or []):
        lines.append("⚠ <b>looks like a scam: check before applying</b>")
    lines.append(f"📱 /check_{jid} · ✅ /applied_{jid} · /skip_{jid}")
    if db is not None:
        from .apply.runner import applied_before
        on = applied_before(db, j)
        if on:
            d = parse_iso(on) if len(on) > 10 else None
            own = db.applied_on(j["id"]) or j.get("status") == "applied"
            lines.append(f"✅ {'Applied' if own else 'already applied'} on {_day(d) if d else _esc(on[:10])}"
                         + ("" if own else " (same role, another listing)"))
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


def _govt_reason(j: dict) -> str:
    """The one line that decides it: the first blocker, else the first open question, else what passed."""
    rs = j.get("reasons") or []
    for mark in ("✗ ", "? "):
        hit = next((r for r in rs if r.startswith(mark)), None)
        if hit:
            return hit[2:]
    ok = [r[2:] for r in rs if r.startswith("✓ ")]
    return "; ".join(ok[:2]) or "see notification"


def registrations() -> list[dict]:
    """Your govt portal registrations (OTR) from config/advantages.yaml: portal, sources, url, status."""
    from . import config
    return config.cfg("advantages").get("registrations") or []


def registration_text(r: dict) -> str:
    st = str(r.get("status") or "unknown").lower()
    mark = "✓ done" if st == "done" else "✗ not done" if st in ("not done", "no", "pending") else "? not recorded"
    return f"{_esc(r['portal'])} {mark}"


def govt_card(j: dict, profile: dict | None = None, today: date | None = None) -> str:
    """One govt notice, every value from the notification itself (else "see notification")."""
    from . import config
    from .eligibility import age_on, to_date
    d = j.get("details") or {}
    see = "see notification"
    today = today or date.today()
    prof = profile if profile is not None else config.cfg("profile")
    flags = j.get("flags") or []
    verdict = next((f.split(":", 1)[1] for f in flags if f.startswith("eligibility:")), "check")
    last = to_date(j.get("deadline") or d.get("last_date"))
    left = (last - today).days if last else None
    lines = []
    if left is not None and 0 <= left <= 3:
        lines.append(f"🔴 <b>closes in {left} day{'s' if left != 1 else ''}</b>" if left else "🔴 <b>closes today</b>")
    org = _esc(j["company"]) + (f" ({_esc(d['department'])})" if d.get("department") else "")
    lines.append(f"🏛 <b>{_esc(clip(d.get('post') or j['title'], 120))}</b> · {org} · score {j.get('score', 0)}")
    lines.append(f"📍 {_esc(j.get('location') or d.get('place') or see)}"
                 + (f" · posting: {_esc(clip(d['place'], 80))}" if d.get("place") and d["place"] not in (j.get("location") or "") else ""))
    lines.append(f"💰 {_esc(d.get('pay') or see)}")
    vac = d.get("vacancies")
    lines.append(f"👥 {vac if vac else see}" + (f" posts · SC {d['sc_vacancies']}" if vac and d.get("sc_vacancies") is not None
                                                 else " posts" if vac else ""))
    mark = {"eligible": "✅ Eligible", "check": "🟡 Check", "ineligible": "❌ Not eligible"}.get(verdict, "🟡 Check")
    lines.append(f"{mark}: {_esc(clip(_govt_reason(j), 160))}")
    cat = (prof.get("category") or "GM").upper()
    limit = f"{d['age_limit_sc']} for {cat}" if cat in ("SC", "ST") and d.get("age_limit_sc") else d.get("age_limit")
    dob, on = to_date(prof.get("date_of_birth")), to_date(d.get("age_cutoff")) or last
    mine = (f"you: {int(age_on(dob, on))} on the cut-off {_day(on)}" if dob and on else
            f"you: {int(age_on(dob, today))} today (cut-off date: {see})" if dob else "your age: add date_of_birth to private.yaml")
    lines.append(f"🎂 age limit {limit or see} · {mine}")
    lines.append(f"💸 fee for SC: {_esc(d.get('fee_sc') or see)}")
    rel, ex = to_date(d.get("released")), to_date(d.get("exam_date"))
    lines.append(f"🗓 released {_day(rel) if rel else see} · last date "
                 + (f"{_day(last)} ({left} day{'s' if left != 1 else ''} left)" if last else see)
                 + f" · exam {_day(ex) if ex else see}")
    links = []
    if d.get("pdf"):
        links.append(f'📄 <a href="{html.escape(d["pdf"], quote=True)}">Notification PDF</a>')
    apply_u = d.get("apply_url") or j.get("url")
    if apply_u:
        links.append(f'🔗 <a href="{html.escape(apply_u, quote=True)}">Apply / official page</a>')
    lines.append(" · ".join(links) or "🔗 no link listed")
    src = (j.get("source") or (j.get("sources") or [""])[0] or "").removeprefix("govt:")
    reg = next((r for r in registrations() if src and src in (r.get("sources") or [])), None)
    if reg:
        lines.append(f"🪪 {registration_text(reg)}" + (f' · <a href="{html.escape(reg["url"], quote=True)}">portal</a>' if reg.get("url") else ""))
    lines.append(f"/show_{j['id'][:8]} · /skip_{j['id'][:8]} · /applied_{j['id'][:8]}")
    return "\n".join(lines)


def govt_open(jobs: list[dict], today: date | None = None, scope: str = "all", eligible_only: bool = False) -> list[dict]:
    """Open govt notices (last date not passed), eligible first, then by last date. scope: all / karnataka / central."""
    today_s = str(today or date.today())
    out = []
    for j in jobs:
        f = j.get("flags") or []
        if j["status"] in ("skipped", "applied", "rejected") or (j.get("deadline") and j["deadline"][:10] < today_s):
            continue
        state = "govt:state" in f
        in_ka = state or bool((j.get("details") or {}).get("karnataka_places"))
        if (scope == "karnataka" and not in_ka) \
                or (scope == "central" and state):
            continue
        if eligible_only and "eligibility:eligible" not in f:
            continue
        out.append(j)
    rank = {"eligibility:eligible": 0, "eligibility:check": 1, "eligibility:ineligible": 2}
    out.sort(key=lambda j: (min((rank[x] for x in j.get("flags") or [] if x in rank), default=1), j.get("deadline") or "9999"))
    return out
