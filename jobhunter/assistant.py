"""Free-text chat on Telegram: any message that isn't a command.

The model sees your message, the last 10 turns of this chat (table chat_turns) and a list of tools that wrap
existing functions; it never writes SQL or code. It replies with JSON, {"answer": ...} or {"tool": ..., "args": ...};
at most 4 tool steps per message. Facts come only from tool results (the posting, resume.yaml, the DB).
Any change (status, note, skip, dream) is described first and runs only after you reply "yes".
Obvious questions ("what did I apply to this week", "send me the sheet", "more") skip the model.
NVIDIA only (ASSISTANT_MODEL, then 2 fallbacks, every key): Ollama is never used for chat. All of them down → FAIL.
"""
from __future__ import annotations

import datetime as dt
import html
import inspect
import json
import re
import threading
import time

from . import config, llm
from .config import env
from .util import log
from .db import DB, _row
from .notify import govt_card, job_card, mode_text, pages, pay_line, when_text

LIMIT = 3500
STEPS = 4
FAIL = "AI is busy, try again in a minute (commands like top 5 still work)"
# fastest NVIDIA models that returned valid JSON on the 12 examples (scripts/bench_assistant.py); ASSISTANT_MODEL overrides
# (24 Nov 2026: bolt 0.8 s median, 12/12 JSON, 11/12 right tool; nano-omni 2.7 s 10/12; laguna 1.2 s 9/12)
MODELS = ["nvidia/nemotron-3.5-bolt-e4b", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", "poolside/laguna-xs-2.1"]
OPEN = ("new", "alerted", "queued", "needs_you", "filled")


def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


# ── memory: chat turns, the change waiting for "yes", "more" pages ─────────
def ensure(db: DB) -> None:
    db.c.executescript("""
    CREATE TABLE IF NOT EXISTS chat_turns (chat TEXT, at REAL, role TEXT, text TEXT);
    CREATE INDEX IF NOT EXISTS chat_turns_chat ON chat_turns(chat, at);
    CREATE TABLE IF NOT EXISTS chat_pending (chat TEXT PRIMARY KEY, at REAL, tool TEXT, args TEXT, what TEXT);
    """)


def history(db: DB, chat: str, n: int = 10) -> list[tuple[str, str]]:
    rows = db.c.execute("SELECT role, text FROM chat_turns WHERE chat=? ORDER BY at DESC LIMIT ?", (chat, n)).fetchall()
    return [(r[0], r[1]) for r in reversed(rows)]


def remember(db: DB, chat: str, role: str, text: str) -> None:
    plain = html.unescape(re.sub(r"<[^>]+>", "", text or ""))
    db.c.execute("INSERT INTO chat_turns VALUES (?,?,?,?)", (chat, time.time(), role, plain[:600]))
    db.c.execute("DELETE FROM chat_turns WHERE chat=? AND at < (SELECT MIN(at) FROM (SELECT at FROM chat_turns "
                 "WHERE chat=? ORDER BY at DESC LIMIT 40))", (chat, chat))
    db.c.commit()


_MORE: dict[str, list[str]] = {}        # chat → cards not shown yet ("more")
_CACHE: dict[str, tuple[float, dict]] = {}
_LIVE: dict[str, float] = {}             # query|place → last live search (at most one a day each)
CACHE_SECS = 600


# ── finding jobs ───────────────────────────────────────────────────────────
from .assistant_rules import (  # noqa: E402,F401  (re-exported: tests and the evals use assistant.<name>)
    ALIASES, CITIES, CITY, FAST, KARNATAKA, ROLE_HEADS, SALARY, SHEET, STOP, TOP_ROLE, _fast, _put, _same, my_skills,
    role_summary)


def _words(query: str) -> list[list[str]]:
    """Each query word with its spellings: every word must match (any spelling)."""
    out = []
    for w in re.findall(r"[a-z0-9+#.]+", (query or "").lower()):
        w = w.strip(".")
        if w and w not in STOP:
            out.append(ALIASES.get(w.rstrip("s"), ALIASES.get(w, [w])))
    return out


def _has(text: str, spell: str) -> bool:
    """Whole word (plural allowed): 'intern' matches 'interns', never 'international'."""
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(spell)}(?:s|es)?(?![a-z0-9])", text))


def _place_ok(j: dict, location: str) -> bool:
    loc = (location or "").lower().strip()
    if loc in ("", "any", "india", "anywhere", "all"):
        return True
    names = CITY.get(loc.split(",")[0].strip(), [loc.split(",")[0].strip()])
    where = (j.get("location") or "").lower()
    return bool(j.get("is_remote")) or "remote" in where or any(n in where for n in names)


def db_search(db: DB, query: str, location: str = "Karnataka", days: int | None = None) -> list[dict]:
    today = str(dt.date.today())
    sql = f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(OPEN))}) AND (deadline IS NULL OR deadline >= ?)"
    args: list = [*OPEN, today]
    if days:
        sql += " AND COALESCE(posted_at, found_at) >= ?"
        args.append(str(dt.date.today() - dt.timedelta(days=int(days))))
    words = _words(query)
    out = []
    for j in map(_row, db.c.execute(sql + " ORDER BY score DESC LIMIT 3000", args)):
        hay = " ".join([j["title"], j["company"], j.get("description") or ""]).lower()
        if all(any(_has(hay, s) for s in w) for w in words) and _place_ok(j, location):
            out.append(j)
    title = lambda j: all(any(_has(f"{j['title']} {j['company']}".lower(), s) for s in w) for w in words)   # noqa: E731
    out.sort(key=lambda j: ((j["score"] or 0) < 40, not title(j), not j["dream"], -(j["score"] or 0)))   # weak ones last
    return out


def _full_place(location: str) -> str:
    loc = (location or "Karnataka").split(",")[0].strip().lower()
    if loc in ("karnataka", "", "bengaluru", "bangalore"):
        return "Bengaluru, Karnataka, India"
    if loc in ("india", "any", "anywhere", "all", "remote"):
        return "India"
    return f"{location}, India"


def live_scan(query: str, location: str, days: int | None = None, wait: float = 40) -> int:
    """LinkedIn + Naukri for this query now, in parallel; whatever arrives within `wait` s is saved and scored like a
    scan (late arrivals are still saved). Returns how many jobs came back in time."""
    from .pipeline import save
    from .sources import boards
    got = [0]

    def one(site: str) -> None:
        errors: list[str] = []
        jobs = boards.scan(errors, sites=[site], queries=[{"term": query}], locations=[_full_place(location)],
                           hours_old=24 * (days or 7), quiet=True)
        save(DB(), jobs)
        got[0] += len(jobs)
    ts = [threading.Thread(target=one, args=(s,), daemon=True) for s in ("linkedin", "naukri")]
    end = time.time() + wait
    for t in ts:
        t.start()
    for t in ts:
        t.join(max(0.1, end - time.time()))
    return got[0]


def _brief(j: dict) -> dict:
    """What the model sees of a job: never the whole DB row."""
    return {"id": j["id"][:8], "title": j["title"], "company": j["company"], "location": j.get("location") or "",
            "mode": mode_text(j) or "not listed", "pay": pay_line(j), "posted": when_text(j), "score": j.get("score"),
            "status": j["status"], "deadline": j.get("deadline")}


def _card(j: dict, db: DB) -> str:
    return govt_card(j) if j.get("kind") == "govt" else job_card(j, db)


def _cards(db: DB, ctx: dict, jobs: list[dict], head: str, limit: int) -> dict:
    cards = [_card(j, db) for j in jobs]
    shown, rest = cards[:limit], cards[limit:]
    _MORE[ctx["chat"]] = rest
    tail = f"\n\n{len(rest)} more: say 'more'" if rest else ""
    return {"data": [_brief(j) for j in jobs[:limit]], "more": rest,
            "show": [head, *shown[:-1], (shown[-1] + tail) if shown else tail]}


def find_job(db: DB, ref: str) -> dict | None:
    """An 8-hex id, or words of the title/company ('the Birlasoft job'): your applications first, then the best open."""
    ref = (ref or "").strip()
    m = re.search(r"\b[0-9a-f]{6,16}\b", ref.lower())
    if m and (j := db.get(m.group(0))):
        return j
    words = _words(ref)
    if not words:
        return None
    rank = {"interview": 0, "offer": 0, "applied": 1, "needs_you": 2, "queued": 2, "alerted": 3, "new": 3}
    hits = []
    for j in map(_row, db.c.execute("SELECT * FROM jobs WHERE status NOT IN ('filtered','expired')")):
        hay = f"{j['title']} {j['company']}".lower()
        if all(any(_has(hay, s) for s in w) for w in words):
            hits.append(j)
    hits.sort(key=lambda j: (rank.get(j["status"], 4), -(j.get("score") or 0)))
    return hits[0] if hits else None


# ── tools: read ────────────────────────────────────────────────────────────
def t_search_jobs(db: DB, ctx: dict, query: str, location: str = "Karnataka", limit: int = 5, days: int | None = None):
    limit = max(1, min(int(limit or 5), 10))
    days = int(days) if str(days or "").isdigit() else None
    rows = db_search(db, query, location, days)
    from .util import now, parse_iso
    newest = max((d for j in rows if (d := parse_iso(j["found_at"]))), default=None)
    scanned = ""
    key = f"{query.lower().strip()}|{(location or '').lower()}"
    if (newest is None or now() - newest > dt.timedelta(hours=24)) and time.time() - _LIVE.get(key, 0) > 86400:
        _LIVE[key] = time.time()
        ctx["progress"]("🔎 searching…")
        n = live_scan(query, location, days)
        rows = db_search(db, query, location, days)
        scanned = f" · live search just now ({n} found)"
    where = "" if (location or "").lower() in ("", "any", "india") else f" · {_e(location)}"
    if not rows:
        return {"data": [], "show": [f"<b>🔎 {_e(query)}{where}</b>{scanned}\nno open jobs match. Try fewer words."]}
    return _cards(db, ctx, rows, f"<b>🔎 {_e(query)}{where}: {len(rows)} open, best first</b>{scanned}", limit)


def t_top_jobs(db: DB, ctx: dict, n: int = 5):
    n = max(1, min(int(n or 5), 20))
    jobs = [j for j in db.query(status=["new", "alerted", "queued"], kind="private", min_score=40, limit=n * 3)
            if not j.get("deadline") or j["deadline"] >= str(dt.date.today())]
    jobs.sort(key=lambda j: (j["status"] != "queued", not j["dream"], -j["score"]))
    if not jobs:
        return {"data": [], "show": ["<b>Top open jobs</b>\nnone: run /scan"]}
    return _cards(db, ctx, jobs, f"<b>Top {min(n, len(jobs))} open jobs</b>", n)


def t_today(db: DB, ctx: dict):
    from .bot import _today
    return {"data": "shown", "show": [_today(db)]}


def t_govt(db: DB, ctx: dict, filter: str = "", closing_within_days: int | None = None):
    if closing_within_days:
        from .notify import govt_open
        today = dt.date.today()
        end = str(today + dt.timedelta(days=int(closing_within_days)))
        jobs = [j for j in govt_open(db.query(kind="govt", limit=1000)) if j.get("deadline") and j["deadline"][:10] <= end
                and "eligibility:ineligible" not in (j.get("flags") or [])]
        jobs.sort(key=lambda j: j["deadline"])
        if not jobs:
            return {"data": [], "show": [f"<b>🏛 Govt notices closing in {closing_within_days} days</b>\nnone you're eligible for"]}
        cards = [govt_card(j, config.cfg("profile"), today) for j in jobs]
        return {"data": [_brief(j) for j in jobs[:10]],
                "show": [f"<b>🏛 Govt notices closing in {closing_within_days} days: {len(jobs)}</b>", *cards]}
    from .bot import handle
    out = handle(f"/govt {filter}".strip(), db)
    return {"data": "shown", "show": out if isinstance(out, list) else [out]}


def t_job_details(db: DB, ctx: dict, id: str):
    j = find_job(db, id)
    if not j:
        return {"data": f"no job matches {id!r}", "show": None}
    fit = t_explain_job(db, ctx, j["id"])["data"]          # skills you have / miss, from resume.yaml: never the model's guess
    return {"data": {**_brief(j), "description": " ".join((j.get("description") or "not in the posting").split())[:2500],
                     "why_it_matched": (j.get("reasons") or [])[:5], "you_have": fit["you_have"],
                     "you_miss": fit["missing"], "fit": fit["fit"]}, "show": None, "card": _card(j, db)}


def t_company(db: DB, ctx: dict, name: str):
    from .bot import _company_cmd
    ctx["progress"]("🔎 checking " + _e(name) + "…")
    out = _company_cmd(name, db)
    return {"data": "shown", "show": out if isinstance(out, list) else [out]}


def t_applied(db: DB, ctx: dict, filter: str = ""):
    """filter: today | week | month | a status (interview, rejected …) | words of a company or title."""
    f = (filter or "").lower().strip()
    rows = db.query(status=["applied", "interview", "offer", "rejected"], limit=100000)
    rows = [(db.applied_on(j["id"]) or j.get("status_at") or "", j) for j in rows]
    today = dt.date.today()
    since = {"today": today, "week": today - dt.timedelta(days=today.weekday()), "this week": today - dt.timedelta(days=today.weekday()),
             "month": today.replace(day=1), "this month": today.replace(day=1)}.get(f)
    if since:
        rows = [(on, j) for on, j in rows if on[:10] >= str(since)]
    elif f in ("applied", "interview", "offer", "rejected"):
        rows = [(on, j) for on, j in rows if j["status"] == f]
    elif f:
        words = _words(f)
        rows = [(on, j) for on, j in rows if all(any(_has(f"{j['title']} {j['company']}".lower(), s) for s in w) for w in words)]
    rows.sort(key=lambda x: x[0], reverse=True)
    label = {"week": "this week", "today": "today", "month": "this month"}.get(f, f)
    head = f"<b>Your applications{(' · ' + _e(label)) if label else ''}: {len(rows)}</b>"
    if not rows:
        return {"data": [], "show": [head + "\nnone"]}
    mark = {"interview": "🎤", "offer": "🎉", "rejected": "✗", "applied": "·"}
    lines = [f"{mark[j['status']]} {_e(on[:10])} · {_e(j['title'][:60])} · {_e(j['company'])} /show_{j['id'][:8]}"
             for on, j in rows]
    return {"data": [{**_brief(j), "applied_on": on[:10]} for on, j in rows[:20]], "show": [head + "\n" + "\n".join(lines)]}


def t_stats(db: DB, ctx: dict):
    from .bot import _stats
    return {"data": "shown", "show": [_stats(db)]}


def t_explain_job(db: DB, ctx: dict, id: str):
    from .prep import top_skills
    from .tailor import keywords
    j = find_job(db, id)
    if not j:
        return {"data": f"no job matches {id!r}", "show": [f"No job matches <i>{_e(id)}</i>. Send its 8-letter id from a card."]}
    desc = " ".join((j.get("description") or "").replace("*", " ").split())
    asked = list({k.lower(): k for k in (keywords(desc) if desc else [])}.values())
    mine = my_skills()
    have = [s for s in asked if any(_same(s, m) for m in mine)]
    miss = [s for s in asked if s not in have]
    topics = top_skills(desc, 6) if desc else []
    sc = j.get("score") or 0
    verdict = ("strong fit" if sc >= 70 else "good fit" if sc >= 55 else "partial fit" if sc >= 40 else "weak fit")
    what = role_summary(desc)
    lines = [f"<b>{_e(j['title'])}</b> · {_e(j['company'])} · /show_{j['id'][:8]}",
             f"<b>Fit:</b> {verdict} (score {sc}/100)" + (f": {_e('; '.join((j.get('reasons') or [])[:2]))}" if j.get("reasons") else ""),
             f"<b>What the role does:</b> {_e(what)}",
             f"✅ <b>You have:</b> {_e(', '.join(have)) if have else 'none of the listed skills matched resume.yaml'}",
             f"❌ <b>Missing:</b> {_e(', '.join(miss)) if miss else ('nothing listed that you lack' if asked else 'the posting lists no skills')}",
             f"🎤 <b>Interview topics:</b> {_e(', '.join(topics)) if topics else 'not in the posting'}",
             f"💰 {_e(pay_line(j))} · 📍 {_e(j.get('location') or 'location not listed')}",
             f"📱 /check_{j['id'][:8]} · ✅ /applied_{j['id'][:8]} · /skip_{j['id'][:8]}"]
    return {"data": {**_brief(j), "skills_asked": asked, "you_have": have, "missing": miss, "fit": verdict},
            "show": ["\n".join(lines)]}


def t_salary_estimate(db: DB, ctx: dict, role: str, company: str = "", location: str = "Bengaluru",
                      experience: str = "fresher", id: str = ""):
    """Approximate pay from AmbitionBox / Glassdoor / Levels.fyi / Naukri; never from the model, never pay_min/pay_max."""
    from . import salary
    j = find_job(db, id) if id else None
    if j:
        role, company = role or j["title"], company or j["company"]
    key = "|".join(x.lower().strip() for x in (role, company, location, experience))
    salary.ensure(db)
    if not db.c.execute("SELECT 1 FROM salary_cache WHERE key=? AND at > ?", (key, time.time() - 30 * 86400)).fetchone():
        ctx["progress"]("🔎 searching…")
    res = salary.estimate(role, company, location, experience, db)
    targets = [j] if j else [x for x in map(_row, db.c.execute(
        "SELECT * FROM jobs WHERE LOWER(company) LIKE ? AND pay_min IS NULL AND pay_max IS NULL",
        (f"%{company.lower().strip()}%",))) if company and all(any(_has(x["title"].lower(), sp) for sp in w) for w in _words(role))]
    for x in targets:
        salary.store(db, x["id"], res)
    data = {k: res.get(k) for k in ("min", "max", "confidence", "note")}
    return {"data": data, "show": [salary.as_html(res)]}


def t_sheet_send(db: DB, ctx: dict):
    return {"data": "sent", "show": ["__SHEET__"]}


from .assistant_writes import (  # noqa: E402,F401  (re-exported)
    STATUS_WORDS, Nope, _job_or_err, w_set_status, w_add_note, w_skip, _dream, w_dream,
    w_undream, _sheet_job, _on_sheet, w_sheet_add_column, w_sheet_set, w_sheet_remove_row, w_sheet_show_hidden)


TOOLS = {
    "search_jobs": (t_search_jobs, False, "search_jobs(query, location='Karnataka', limit=5, days=null): open jobs for a role/skill, best first; days=7 for 'this week'"),
    "top_jobs": (t_top_jobs, False, "top_jobs(n=5): best open jobs overall"),
    "today": (t_today, False, "today(): jobs posted in the last 3 days"),
    "govt": (t_govt, False, "govt(filter='', closing_within_days=null): govt notices; filter karnataka|central|a word; closing_within_days=7 for 'closing soon'"),
    "job_details": (t_job_details, False, "job_details(id): one job's posting text, to answer a specific question about it"),
    "explain_job": (t_explain_job, False, "explain_job(id): what the role does, skills it asks that you have/miss, interview topics, fit; id may be an 8-letter id or 'Birlasoft'"),
    "company": (t_company, False, "company(name): a company's open roles (live) and news"),
    "applied": (t_applied, False, "applied(filter=''): your applications; filter today|week|month|interview|rejected|a company"),
    "stats": (t_stats, False, "stats(): applied today/week/total, interviews, offers, response rate"),
    "salary_estimate": (t_salary_estimate, False, "salary_estimate(role, company='', location='Bengaluru', experience='fresher', id=''): approximate pay from salary sites when a posting doesn't list it"),
    "sheet_send": (t_sheet_send, False, "sheet_send(): send the Excel tracker"),
    "set_status": (w_set_status, True, "set_status(id, status): mark applied|interview|offer|rejected|skipped|queued"),
    "add_note": (w_add_note, True, "add_note(id, text): add a note to a job, ONLY when the user says note / add a note"),
    "skip": (w_skip, True, "skip(id): hide a job"),
    "sheet_add_column": (w_sheet_add_column, True, "sheet_add_column(name): add your own column to the Excel tracker"),
    "sheet_set": (w_sheet_set, True, "sheet_set(id, column, value): 'put X in <column> for <id>': X goes in that sheet column (Referral, Notes, ...)"),
    "sheet_remove_row": (w_sheet_remove_row, True, "sheet_remove_row(id): hide a job's row from the sheet (nothing is deleted)"),
    "sheet_show_hidden": (w_sheet_show_hidden, True, "sheet_show_hidden(): put hidden rows back on the sheet"),
    "dream": (w_dream, True, "dream(name): add a dream company"),
    "undream": (w_undream, True, "undream(name): remove a dream company"),
}

SYSTEM = """You are the job-search assistant of one Indian fresher. Reply with ONE JSON object and nothing else:
{"tool": "<name>", "args": {...}} to use a tool, or
{"answer": "<plain words>"} when you can answer.
Facts come ONLY from tool results and the chat. Never invent requirements, pay, salaries, dates, companies or facts about the user.
If a posting doesn't say something, answer "not in the posting". A company fact (what it does, size, founded, offices,
funding, people) only with the source URL the tool gave; no URL in the tool results → answer "not found".
Pay questions: use salary_estimate; never state a number yourself.
"Which <role> job pays most": search_jobs with the role.
Advice (interview prep, what to learn): 3-4 "• " general tips, no numbers or firms. Else if no tool fits: can't do that yet.
For "is X a good fit" or "what do I miss" use explain_job. When a role or skill is named (GenAI, ML intern), use
search_jobs with it as the query; top_jobs only when none is named. Job ids are 8 hex letters like ab12cd34. "this week" means days=7. Changes go through their tool; the app asks the user to confirm.
Follow-ups ("same in Mysuru", "the first one", "mark it applied"): repeat the last tool call or id from the chat with the change.
After "Tool results": ≤4 short sentences from them only, naming job ids; don't list the cards.
Kannada-English: alli=in, yaavudu=which, ee vaara=this week, sambala=salary, maadide=did.
Tools:
"""


def _system() -> str:
    return SYSTEM + "\n".join("- " + d for _, _, d in TOOLS.values())


def _parse(raw: str) -> dict | None:
    t = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip()
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        obj = json.loads(t[a:b + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) and ("answer" in obj or "tool" in obj) else None


def _call(fn, db: DB, ctx: dict | None, args: dict, **extra):
    """Only the arguments the function takes (a model's stray keys never crash it)."""
    params = inspect.signature(fn).parameters
    kw = {k: v for k, v in (args or {}).items() if k in params and k not in ("db", "ctx", "run")}
    return fn(db, ctx, **kw) if ctx is not None else fn(db, **kw, **extra)


def run_tool(db: DB, ctx: dict, name: str, args: dict) -> dict:
    fn = TOOLS[name][0]
    key = name + json.dumps(args or {}, sort_keys=True)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_SECS and name not in ("sheet_send", "company"):
        if "more" in hit[1]:
            _MORE[ctx["chat"]] = hit[1]["more"]          # "more" follows what's shown
        return hit[1]
    res = _call(fn, db, ctx, args)
    _CACHE[key] = (time.time(), res)
    return res


# ── answers without the model when the intent is obvious ──────────────────
def route(text: str, db: DB, chat: str = "", models: list[str] | None = None) -> tuple[str, dict] | None:
    """(tool, args) the assistant would use for this message, nothing run: the fast paths, else the model's first JSON.
    ("more", {}) for "more", ("none", {"answer": …}) for a plain answer, None when the model gave nothing usable."""
    t = (text or "").strip()
    if MORE.match(t):
        return "more", {}
    if hit := _fast(db, t):
        return hit[0], hit[1]
    ensure(db)
    raw = _ask(_prompt(t, history(db, chat) if chat else [], []), models)
    obj = _parse(raw) if raw else None
    if obj is None:
        return None
    if "answer" in obj:
        return "none", {"answer": str(obj["answer"])}
    return str(obj.get("tool")), obj.get("args") or {}


def _prompt(t: str, hist: list[tuple[str, str]], steps: list[dict]) -> str:
    user = ("Chat so far:\n" + "\n".join(f"{r}: {x}" for r, x in hist) + "\n\n" if hist else "") + f"User: {t}"
    if steps:
        user += "\n\nTool results:\n" + json.dumps(steps, ensure_ascii=False, default=str)[:6000] + "\n\nNext JSON:"
    return user


def _ask(user: str, models: list[str] | None = None) -> str | None:
    return llm.chat(_system(), user, temperature=0, max_tokens=500, models=models or _models(), timeout=8,
                    provs=("nvidia",), valid=lambda x: _parse(x) is not None)


NO = re.compile(r"^(?:no|n|nope|cancel|stop|don'?t|do not|never ?mind|leave it)\W*$", re.I)
# a question wants a sentence back ("which pays most?", "how many did I apply to?"), not just cards
QUESTION = re.compile(r"\?\s*$|^(?:which|what|how|why|when|is|are|does|do|did|can|should|will|compare|tell me (?:if|whether|why))\b"
                      r"|\b(?:best one|most|highest|lowest|cheapest|better|vs\.?|versus|worth|enough|chance)\b", re.I)
LISTING = re.compile(r"^(?:show|list|find|search|get|send|give|top)\b|^(?:what|which|any) (?:new )?(?:jobs|roles|openings|internships)\b(?!.*\b(?:most|best|highest)\b)", re.I)
# "which genai job pays the most?": always the postings' own pay (the model kept reaching for salary_estimate)
PAYS = re.compile(r"^(?:which|what)(?: one| is the| are the)? (?P<r>.+?) (?:jobs?|roles?|internships?|openings?) "
                  r"(?:that |which )?(?:pays?|offers?|has) (?:the )?(?:most|best|highest)|^(?:the )?(?:best|highest)[- ]pa(?:id|ying) "
                  r"(?P<r2>.+?) (?:jobs?|roles?|internships?|openings?)", re.I)
MORE = re.compile(r"^(?:show )?more\W*$", re.I)
YES = re.compile(r"^(?:yes|y|yeah|yep|yes please|confirm|do it|ok(?:ay)?(?: do it)?|haan|ha)\W*$", re.I)


def _fit(msgs: list[str]) -> list[str]:
    """Every message ≤ 3500 chars, split between blocks (a card is never cut)."""
    out: list[str] = []
    for m in msgs:
        if len(m) <= LIMIT:
            out.append(m)
            continue
        blocks = []
        for b in m.split("\n\n"):
            while len(b) > LIMIT:
                cut = b.rfind("\n", 0, LIMIT)
                cut = cut if cut > 0 else LIMIT
                blocks.append(b[:cut])
                b = b[cut:].lstrip("\n")
            blocks.append(b)
        out += [p.strip() for p in pages("", blocks, limit=LIMIT)]
    return [m for m in out if m.strip()]


def _join(show: list[str]) -> list[str]:
    """Cards → whole messages: as many blocks as fit per message."""
    return _fit([p.strip() for p in pages("", [s for s in show if s], limit=LIMIT)])


def reply(text: str, chat: str = "", db: DB | None = None, progress=None) -> str | list[str]:
    db = db or DB()
    ensure(db)
    chat = chat or str(env("TELEGRAM_CHAT_ID") or "me")
    ctx = {"chat": chat, "progress": progress or (lambda s: None)}
    t = (text or "").strip()
    llm.LAST.clear()                                     # so the reply knows which AI answered THIS message
    pend = db.c.execute("SELECT at, tool, args FROM chat_pending WHERE chat=?", (chat,)).fetchone()
    db.c.execute("DELETE FROM chat_pending WHERE chat=?", (chat,))
    db.c.commit()
    if pend and NO.match(t):
        return "Cancelled: nothing changed."
    if YES.match(t):
        if not pend or time.time() - pend[0] > 900:
            return "Nothing is waiting for a yes. Tell me what to change."
        _CACHE.clear()
        out = _call(TOOLS[pend[1]][0], db, None, json.loads(pend[2]), run=True)
        remember(db, chat, "user", t)
        remember(db, chat, "assistant", out if isinstance(out, str) else out[0])
        return out
    if MORE.match(t):
        rest = _MORE.get(chat) or []
        if not rest:
            return "Nothing more to show. Ask me for a search."
        _MORE[chat] = rest[5:]
        return _join(rest[:5] + ([f"{len(rest) - 5} more: say 'more'"] if len(rest) > 5 else []))
    if hit := _fast(db, t):
        return _finish(db, ctx, t, *hit)
    hist = history(db, chat)
    steps: list[dict] = []
    if m := PAYS.search(t):
        name, args = "search_jobs", {"query": m["r"] or m["r2"]}
        res = run_tool(db, ctx, name, args)
        steps.append({"tool": name, "args": args, "result": res["data"], "card": res.get("card"),
                      "show": (res.get("show") or [])[:3]})
    for _ in range(STEPS):
        raw = _ask(_prompt(t, hist, steps))
        if raw is None:
            return FAIL
        obj = _parse(raw)
        if obj is None or "answer" in obj or obj.get("tool") not in TOOLS:
            if obj and obj.get("tool"):
                steps.append({"tool": obj["tool"], "error": "no such tool"})
                continue
            ans = _grounded(str((obj or {}).get("answer") or llm.clean(raw)), t, steps)
            ans = re.sub(r"(?:[;,]\s*|\s+)(?=• )", "\n", ans).strip()   # models run "• a; • b" on one line
            last = steps[-1] if steps else {}
            extra = [last["card"]] if last.get("card") else [x for x in last.get("show") or [] if x != "__SHEET__"]
            remember(db, chat, "user", t)
            remember(db, chat, "assistant", ans)
            return _join([_e(ans)] + extra)              # the answer first, then the jobs it is about
        name, args = obj["tool"], obj.get("args") or {}
        if TOOLS[name][1] or (name != "job_details" and not (QUESTION.search(t) and not LISTING.search(t))):
            return _finish(db, ctx, t, name, args, "")    # "show me / find": the cards are the answer
        try:
            res = run_tool(db, ctx, name, args)
        except Exception as exc:                          # a tool failing never silences the bot
            log.warning("assistant: %s failed: %r", name, exc)
            return f"⚠️ {_e(name)} failed: {_e(type(exc).__name__)}. Try the /help commands."
        steps.append({"tool": name, "args": args, "result": res["data"], "card": res.get("card"),
                      "show": (res.get("show") or [])[:3]})
    return _join(["I couldn't finish that in 4 steps. Try asking more simply, or use /help."])


def _grounded(ans: str, t: str, steps: list[dict]) -> str:
    """Every model-written answer: a fact not in this turn's tool results, resume.yaml or your message is cut."""
    from .grounding import check
    from .tailor import resume_text
    return check(ans, [json.dumps([s.get("result") for s in steps], ensure_ascii=False, default=str), resume_text(), t],
                 skills=bool(steps))


def _finish(db: DB, ctx: dict, t: str, name: str, args: dict, note: str) -> str | list[str]:
    """Run a read tool and show its cards, or describe a change and wait for "yes"."""
    chat = ctx["chat"]
    fn, writes, _ = TOOLS[name]
    remember(db, chat, "user", t)
    if writes:
        try:
            what = _call(fn, db, None, args, run=False)
        except Nope as exc:
            remember(db, chat, "assistant", str(exc))
            return str(exc)
        db.c.execute("INSERT OR REPLACE INTO chat_pending VALUES (?,?,?,?,?)", (chat, time.time(), name, json.dumps(args), what))
        db.c.commit()
        out = f"I'll {what}. Reply <b>yes</b> to confirm."
        remember(db, chat, "assistant", out)
        return out
    try:
        res = run_tool(db, ctx, name, args)
    except Exception as exc:                         # a tool failing never silences the bot
        return f"⚠️ {_e(name)} failed: {_e(type(exc).__name__)}. Try the /help commands."
    show = res.get("show") or []
    if show == ["__SHEET__"]:
        remember(db, chat, "assistant", "sent the sheet")
        return "__SHEET__"
    remember(db, chat, "assistant", f"[{name} {json.dumps(args)}] " + json.dumps(res["data"], default=str)[:400])
    head = [_e(note)] if note else []
    return _join(head + list(show))


def _models() -> list[str]:
    pick = [m.strip() for m in (env("ASSISTANT_MODEL") or "").split(",") if m.strip()]
    return pick + [m for m in MODELS if m not in pick][: max(0, 3 - len(pick))]
