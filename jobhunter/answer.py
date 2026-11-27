"""Grounded answers about named jobs (used by assistant.reply).

mentioned(): the jobs a question is about ("the Artha Labs ML job", "2a56ee6e", "Uniphore vs CommerceIQ"), found in code
before the model runs, so a fast model can't answer "not in the posting" without having seen the posting.
passages(): the parts of a long posting that answer the question (the stack is often in the last paragraph).
page(): the job's own page re-read for deadline / pay / eligibility questions, 24 h cache, a browser only when the page
needs JavaScript. cite(): the job ids an answer names must be ones the tools returned; the cards are exactly those.
scam_note(): any answer about applying to a job flagged scam? starts with the warning, whatever the model said.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .companies import _main_name, cname
from .db import DB
from .notify import pages as _pages

_LIMIT = 3500                    # Telegram's cap is 4096: room for the HTML tags

ANALYSIS = re.compile(r"\b(?:fit|fits|compare|comparison|vs\.?|versus|better|should i apply|worth|missing|miss|meet|"
                      r"eligible|qualify|chances?|tailor|gaps?|strengths?|weakness)\b", re.I)
APPLYING = re.compile(r"\b(?:should i apply|apply (?:to|for)|worth applying|go for (?:it|this)|recommend)\b", re.I)
TOPICS = {   # what the question asks → words a posting uses for it
    "deadline": (r"deadline|last date|closes?|closing|due|apply by|till", "deadline last date apply by closing closes date till"),
    "pay": (r"salary|stipend|pays?|ctc|lpa|package|compensation|earn", "stipend salary ctc ₹ lpa pay compensation per month inr lakh"),
    "eligibility": (r"eligib\w*|qualif\w*|degree|cgpa|gpa|age|batch|graduat\w*|experience|years|meet|require\w*",
                    "eligib qualif degree cgpa gpa graduat batch experience years age b.tech b.e master bachelor require"),
    "stack": (r"stack|tech|tools?|technolog\w*|frameworks?|librar\w*|languages?|skills?",
              "stack python tools technolog framework librar language skills pytorch tensorflow sql react java"),
    "duration": (r"how long|duration|months?|weeks?|tenure", "duration months weeks month"),
    "place": (r"remote|hybrid|onsite|on-site|office|location|wfh|relocat\w*", "remote hybrid onsite office location work from home relocat"),
    "process": (r"interview|process|rounds?|selection|assessment", "interview process round selection assessment test"),
}
VERIFY = ("deadline", "pay", "eligibility")             # questions whose answer is re-checked on the job's own page
GENERIC = {"ai", "data", "digital", "smart", "global", "new", "next", "first", "open", "applied", "in", "of", "for",
           "and", "my", "one", "job", "jobs", "role", "the", "big", "cloud", "tech", "info", "best", "top", "hr"}
JOBISH = re.compile(r"\b(?:job|role|intern|internship|posting|position|opening|post|recruitment|vacanc\w*|company)\b", re.I)
STOP = {"the", "a", "an", "and", "or", "of", "for", "to", "in", "at", "on", "is", "are", "does", "do", "what", "which",
        "how", "job", "role", "me", "my", "i", "it", "its", "this", "that", "with", "about", "tell", "posting", "page"}
HEXID = re.compile(r"(?<![0-9a-z])[0-9a-f]{8,12}(?![0-9a-z])")


def topics(t: str) -> list[str]:
    return [k for k, (rx, _) in TOPICS.items() if re.search(rf"\b(?:{rx})\b", t, re.I)]


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+#]+", (s or "").lower()) if w not in STOP and len(w) > 1}


def _names(company: str) -> set[str]:
    full = cname(_main_name(company))
    full = re.sub(r"(?:\s+(?:in|of|and|part|yc|w\d\d))+$", "", full).strip()
    out = {full} if len(full) >= 3 else set()
    first = full.split()[0] if full else ""
    if len(first) >= 3 and first not in GENERIC:
        out.add(first)
    return out


def mentioned(db: DB, t: str, most: int = 3) -> list[dict]:
    """The jobs this question names: by id, else by company (+ title words to pick the right posting), in the order named."""
    low = (t or "").lower()
    by_id = [j for i in HEXID.findall(low) if (j := db.get(i))]
    if by_id:
        return list({j["id"]: j for j in by_id}.values())[:most]
    jobish = bool(JOBISH.search(low))
    q = _words(low)
    rank = {"interview": 0, "offer": 0, "applied": 1, "needs_you": 2, "queued": 2, "alerted": 3, "new": 3}
    best: dict[str, tuple] = {}
    for jid, title, company, status, score in db.c.execute(
            "SELECT id, title, company, status, score FROM jobs WHERE status NOT IN ('filtered','expired')"):
        names = _names(company)
        full = max(names, key=len, default="")
        for n in names:
            at = re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", low)
            if not at:
                continue
            overlap = len(q & _words(title))
            if not overlap and not jobish:
                continue
            cand = (n != full, -overlap, rank.get(status, 4), -(score or 0), at.start(), jid)
            if n not in best or cand < best[n]:          # one pick per name the question used ("IQVIA" = IQVIA, Iqvia Biotech)
                best[n] = cand
    picks = list(best.values())
    if any(not c[0] for c in picks):                     # "Inspot IT" named in full: drop first-word guesses ("Agentic …")
        picks = [c for c in picks if not c[0]]
    elif picks:                                          # only first words ("KEA …", "Women …"): the best title match
        top = min(c[1] for c in picks)
        picks = [c for c in picks if c[1] == top]
    picks.sort(key=lambda c: c[4])                       # in the order the question names them
    return [db.get(c[5]) for c in picks[:most]]


def passages(text: str, question: str, budget: int = 2500) -> str:
    """The posting whole when it fits; else its opening plus the sentences that answer the question, in order."""
    text = " ".join((text or "").split())
    if len(text) <= budget:
        return text
    want = _words(question) | {w for k in topics(question) for w in TOPICS[k][1].split()}
    parts = re.split(r"(?<=[.!?•])\s+|\s(?=#+\s)|\s(?=- )", text)
    head, keep, used = parts[0][:400], set(), len(parts[0][:400])
    hits = sorted(range(1, len(parts)), key=lambda i: -sum(w in parts[i].lower() for w in want))
    for i in hits:
        if not any(w in parts[i].lower() for w in want) or used + len(parts[i]) > budget:
            continue
        keep.add(i)
        used += len(parts[i]) + 3
    return " … ".join([head] + [parts[i] for i in sorted(keep)])


# ── the job's own page ─────────────────────────────────────────────────────
NEEDS_JS = re.compile(r"enable javascript|requires javascript|you need to enable|please turn on javascript|<noscript>", re.I)


def _markdown(html: str) -> str:
    """Main text as Markdown, headings and lists kept; menus, scripts and footers dropped."""
    from bs4 import BeautifulSoup
    from markdownify import markdownify
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "iframe"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"}) or soup.body or soup
    md = markdownify(str(main), heading_style="ATX", bullets="-", strip=["a", "img"])
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def _ensure(db: DB) -> None:
    db.c.execute("CREATE TABLE IF NOT EXISTS page_cache (url TEXT PRIMARY KEY, at REAL, text TEXT, how TEXT)")


def page(db: DB, url: str, timeout: float = 10, ttl: float = 86400) -> dict:
    """{"url", "text", "how": "page"|"browser", "at"} or {"url", "error"}; a fresh copy is read once a day at most."""
    _ensure(db)
    if row := db.c.execute("SELECT at, text, how FROM page_cache WHERE url=? AND at > ?", (url, time.time() - ttl)).fetchone():
        return {"url": url, "text": row[1], "how": row[2], "at": row[0]}
    import requests
    from .companies import _render
    html, how = "", "page"
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/130 Safari/537.36"})
        if r.status_code >= 400:
            return {"url": url, "error": f"the page answered {r.status_code}"}
        html = r.text
    except requests.RequestException as exc:
        return {"url": url, "error": f"couldn't open it ({type(exc).__name__})"}
    text = _markdown(html)
    if len(text) < 400 or NEEDS_JS.search(html[:20000]) and len(text) < 1500:   # an app shell: render it
        html, how = _render(url), "browser"
        text = _markdown(html) if html else ""
    if len(text) < 200:
        return {"url": url, "error": "the page had no readable text"}
    db.c.execute("INSERT OR REPLACE INTO page_cache VALUES (?,?,?,?)", (url, time.time(), text[:60000], how))
    db.c.commit()
    return {"url": url, "text": text, "how": how, "at": time.time()}


def pages(db: DB, urls: list[str]) -> list[dict]:
    """Up to 3 pages at once, 10 s each (the browser fallback has its own 30 s)."""
    with ThreadPoolExecutor(max_workers=3) as ex:
        return list(ex.map(lambda u: page(DB(db.path), u), urls[:3]))


# ── steps the model sees before it answers ─────────────────────────────────
def prefetch(db: DB, ctx: dict, t: str, details, link) -> tuple[list[dict], list[dict]]:
    """(jobs named, tool steps): each named job's details (the parts that answer t) and, for deadline / pay /
    eligibility questions, what its own page says now."""
    jobs = mentioned(db, t)
    steps = [{"tool": "job_details", "args": {"id": j["id"][:8]}, "result": details(db, ctx, j["id"])["data"]} for j in jobs]
    want = [k for k in topics(t) if k in VERIFY]
    urls = [(j, u) for j in jobs if want and (u := link(j))]
    if urls:
        ctx["progress"]("🔎 checking the job's own page…")
        for (j, u), p in zip(urls, pages(db, [u for _, u in urls])):
            res = {"id": j["id"][:8], "url": u, "says": passages(p["text"], t, 1500)} if "text" in p else \
                  {"id": j["id"][:8], "url": u, "error": p["error"]}
            steps.append({"tool": "job_page", "args": {"url": u}, "result": res})
    return jobs, steps


def cite(ans: str, steps: list[dict], jobs: list[dict]) -> str:
    """Only ids the tools returned survive; an answer about exactly one job that names none gets that job's id."""
    seen = set(HEXID.findall(json.dumps([s.get("result") for s in steps], default=str).lower()))
    ok = {i[:8] for i in seen}

    def keep(m):
        return m.group(0) if m.group(0)[:8] in ok else ""
    ans = re.sub(r"\s*\[\s*\]", "", HEXID.sub(keep, ans)).strip()
    named = {i[:8] for i in HEXID.findall(ans.lower())}
    if not named and len(jobs) == 1 and not re.fullmatch(r"\W*not (?:found|in the posting)\W*", ans, re.I):
        ans = f"{ans.rstrip()} [{jobs[0]['id'][:8]}]"
    return ans


def scam_note(db: DB, t: str, ans: str, jobs: list[dict]) -> str:
    """'⚠ X looks like a scam …' for any job the answer is about that is flagged, when applying is in question."""
    if not (APPLYING.search(t) or re.search(r"\bapply\b", ans, re.I)):
        return ""
    ids = {i[:8] for i in HEXID.findall(ans.lower())} | {j["id"][:8] for j in jobs}
    out = []
    for i in ids:
        j = db.get(i)
        if j and "scam?" in (j.get("flags") or []):
            why = next((r for r in j.get("reasons") or [] if "scam" in r), "looks like a scam")
            out.append(f"⚠️ {j['title']} · {j['company']} [{i}]: {why.lstrip('⚠ ')}. Don't pay anything; check the "
                       f"company's own careers page before applying.")
    return "\n".join(out)


def sources(steps: list[dict]) -> str:
    """'Checked: <url> (today)' for every job page the answer used."""
    read = [s["result"]["url"] for s in steps if s.get("tool") == "job_page" and "says" in s.get("result", {})]
    return ("Checked on the job's page today: " + " · ".join(read)) if read else ""


# ── tools the model can call ───────────────────────────────────────────────
def closing_soon(db: DB, days: int = 7) -> list[dict]:
    """Open jobs (any kind) whose last date is between today and `days` from now, soonest first."""
    today = dt.date.today()
    return [db.get(r[0]) for r in db.c.execute(
        "SELECT id FROM jobs WHERE status IN ('new','alerted','queued','needs_you','filled') AND deadline BETWEEN ? AND ? "
        "ORDER BY deadline, score DESC", (str(today), str(today + dt.timedelta(days=int(days)))))]


def best_paid(jobs: list[dict]) -> list[dict]:
    """Postings that list pay, highest first (per year: a monthly stipend ×12); the rest after, by score."""
    per_year = lambda j: (j.get("pay_max") or j.get("pay_min") or 0) * (12 if (j.get("pay_period") or "") == "month" else 1)  # noqa: E731
    return sorted(jobs, key=lambda j: (-per_year(j), -(j.get("score") or 0)))


def closing_days(t: str) -> dict | None:
    """"which jobs close tomorrow / this week / in 3 days" → closing_soon's args."""
    m = re.search(r"\b(?:clos\w*|deadlines?|last dates?|due)\b.*?\b(today|tomorrow|this week|next week|in (\d+) days?)\b|"
                  r"\b(today|tomorrow|this week)\b.*?\b(?:clos\w*|deadlines?|last dates?)\b", t or "", re.I)
    if not m:
        return None
    w = (m[1] or m[3]).lower()
    return {"today": {"days": 0}, "tomorrow": {"start": 1, "days": 1}, "this week": {"days": 7},
            "next week": {"start": 7, "days": 14}}.get(w) or {"days": int(m[2])}


def t_closing_soon(db: DB, ctx: dict, days: int = 7, start: int = 0):
    from .assistant import _cards
    days, start = int(days), int(start)
    jobs = [j for j in closing_soon(db, days) if j["deadline"][:10] >= str(dt.date.today() + dt.timedelta(days=start))]
    when = {(1, 1): "tomorrow", (0, 0): "today"}.get((start, days), f"in the next {days} days")
    if not jobs:
        return {"data": [], "show": [f"No open job closes {when}."]}
    return _cards(db, ctx, jobs, f"⏰ <b>{len(jobs)} open job{'s' * (len(jobs) > 1)} close {when}</b>", 5)


# ── Telegram's size limit ──────────────────────────────────────────────────
def _fit(msgs: list[str]) -> list[str]:
    """Every message ≤ 3500 chars, split between blocks (a card is never cut)."""
    out: list[str] = []
    for m in msgs:
        if len(m) <= _LIMIT:
            out.append(m)
            continue
        blocks = []
        for b in m.split("\n\n"):
            while len(b) > _LIMIT:
                cut = b.rfind("\n", 0, _LIMIT)
                cut = cut if cut > 0 else _LIMIT
                blocks.append(b[:cut])
                b = b[cut:].lstrip("\n")
            blocks.append(b)
        out += [p.strip() for p in _pages("", blocks, limit=_LIMIT)]
    return [m for m in out if m.strip()]


def _join(show: list[str]) -> list[str]:
    """Cards → whole messages: as many blocks as fit per message."""
    return _fit([p.strip() for p in _pages("", [s for s in show if s], limit=_LIMIT)])


