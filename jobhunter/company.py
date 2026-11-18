"""Company search: type a name → pick the company → every listing it has, with dates and links.

suggest()   ranked, typo-tolerant name matches from: jobs already found, your company
            boards, watched career pages, dream companies + aliases, followed companies.
fetch()     live listings for one company: its own career board (Greenhouse/Lever/Ashby:
            configured or auto-discovered) + LinkedIn/Naukri/Indeed searched by name,
            filtered to that exact company (no "similar companies" noise).
listings()  everything in your DB for that company, newest first.
follow()    put a company on your watchlist: searched by name on every scan, alerts at
            a lower bar, and its career board gets added when one is found.
outreach()  LinkedIn links to its recruiters and REVA alumni there, plus a referral note
            built only from your real resume facts.
"""
from __future__ import annotations

import datetime as dt
import difflib
import re
from pathlib import Path
from urllib.parse import quote_plus

import yaml

from . import config
from .db import DB, Job
from .util import console, norm

SUFFIX = re.compile(r"\b(?:private|pvt|ltd|limited|inc|incorporated|llp|llc|corp|corporation|co|company|"
                    r"technologies|technology|tech|solutions|services|software|systems|labs|india|global|group|the)\b")


def key(name: str) -> str:
    """Comparable company key: 'Sarvam AI Pvt. Ltd.' → 'sarvam ai'."""
    k = " ".join(SUFFIX.sub(" ", norm(name)).split())
    return k or norm(name)


def same(a: str, b: str) -> bool:
    ka, kb = key(a), key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    short, long_ = sorted((ka, kb), key=len)
    if len(short) >= 4 and re.search(rf"\b{re.escape(short)}\b", long_) and long_.startswith(short):
        return True
    return difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.9


# ── catalogue of known companies ───────────────────────────────────────────
def catalogue(db: DB | None = None) -> dict[str, dict]:
    db = db or DB()
    cat: dict[str, dict] = {}

    def add(name, **kw):
        if not name:
            return
        k = key(name)
        e = cat.setdefault(k, {"name": name, "jobs": 0, "open": 0, "applied": 0, "tags": set(), "board": None,
                               "aliases": set()})
        if kw.get("prefer_name"):
            e["name"] = name
        for t in kw.get("tags", []):
            e["tags"].add(t)
        if kw.get("board"):
            e["board"] = kw["board"]
        e["aliases"].add(norm(name))
        return e

    for r in db.c.execute("SELECT company, status, kind, COUNT(*) n FROM jobs GROUP BY company, status, kind"):
        e = add(r["company"], tags=["govt"] if r["kind"] == "govt" else [])
        e["jobs"] += r["n"]
        if r["status"] in ("new", "alerted", "queued", "needs_you"):
            e["open"] += r["n"]
        if r["status"] in ("applied", "interview", "offer", "rejected"):
            e["applied"] += r["n"]
    comp = config.cfg("companies")
    for b in comp.get("boards", []) or []:
        add(b["company"], tags=[f"board:{b['type']}"], board=b, prefer_name=True)
    for w in comp.get("watch", []) or []:
        add(w["company"], tags=["careers-page"], prefer_name=True)
    prof = config.cfg("profile")
    aliases = prof.get("dream_aliases", {}) or {}
    for d in prof.get("dream_companies", []) or []:
        e = add(d, tags=["dream"], prefer_name=True)
        for a in aliases.get(d, []):
            e["aliases"].add(norm(a))
    for f in followed():
        add(f, tags=["followed"], prefer_name=True)
    return cat


def suggest(q: str, db: DB | None = None, n: int = 12) -> list[dict]:
    """Best matches for what you typed (prefix > word > substring > fuzzy), with job counts."""
    qn, qk = norm(q), key(q)
    if not qn:
        return []
    out = []
    for k, e in catalogue(db).items():
        names = {k, *e["aliases"]}
        best = 0.0
        for nm in names:
            if nm == qn or nm == qk:
                s = 100
            elif nm.startswith(qn):
                s = 90
            elif re.search(rf"\b{re.escape(qn)}", nm):
                s = 80
            elif qn in nm:
                s = 65
            else:
                s = 60 * difflib.SequenceMatcher(None, qn, nm[:max(len(qn) + 3, 4)]).ratio()
            best = max(best, s)
        if best >= 42:
            out.append({**e, "tags": sorted(e["tags"]), "aliases": sorted(e["aliases"]), "match": round(best),
                        "key": k})
    out.sort(key=lambda e: (-e["match"], "dream" not in e["tags"], -e["open"], e["name"].lower()))
    return out[:n]


def listings(name: str, db: DB | None = None, aliases: list[str] | None = None) -> list[dict]:
    db = db or DB()
    names = [name, *(aliases or [])]
    rows = [j for j in db.query(limit=5000, order="found_at DESC")
            if any(same(j["company"], n) for n in names)]
    rows.sort(key=lambda j: (j.get("posted_at") or j["found_at"][:10]), reverse=True)
    return rows


# ── career-board discovery ─────────────────────────────────────────────────
def _slugs(name: str) -> list[str]:
    k = key(name)
    words = k.split()
    c = [k.replace(" ", ""), k.replace(" ", "-"), words[0] if words else k, norm(name).replace(" ", "")]
    if len(words) > 1:
        c.append("".join(w[0] for w in words) + words[-1])
    seen, out = set(), []
    for s in c:
        s = re.sub(r"[^a-z0-9-]", "", s)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:5]


def discover_board(name: str, http=None) -> dict | None:
    """Find the company's public Greenhouse/Lever/Ashby board by trying likely slugs."""
    from .sources.ats import scout
    sc = scout()
    import requests
    get = http or (lambda url: requests.get(url, timeout=12, headers={"User-Agent": "jobhunter/1.1 (job seeker)"}))
    for slug in _slugs(name):
        for typ, url in (("greenhouse", sc.GH_BOARD % slug), ("lever", sc.LEVER % slug), ("ashby", sc.ASHBY % slug)):
            try:
                r = get(url)
                if r.status_code != 200:
                    continue
                data = r.json()
            except Exception:
                continue
            if typ == "greenhouse":
                title = (data or {}).get("name", "")
                if title and same(title, name):
                    return {"company": title, "type": "greenhouse", "token": slug}
            elif typ == "lever":
                if isinstance(data, list) and data:
                    return {"company": name, "type": "lever", "token": slug}
            elif typ == "ashby":
                if isinstance(data, dict) and data.get("jobs"):
                    return {"company": name, "type": "ashby", "token": slug}
    return None


# ── live fetch ─────────────────────────────────────────────────────────────
def fetch(name: str, errors: list[str], days: int = 45, boards: tuple = ("linkedin", "naukri", "indeed"),
          discover: bool = True) -> tuple[list[Job], dict | None]:
    """Live listings for one company. Returns (jobs, career_board_used)."""
    from .sources import ats
    from .sources.boards import row_to_job
    jobs: list[Job] = []
    cat = catalogue()
    entry = cat.get(key(name)) or {}
    board = entry.get("board") or (discover_board(name) if discover else None)
    if board:
        sc = ats.scout()
        notes: list[str] = []
        roles = config.cfg("profile").get("targets", {}).get("roles", [])
        for j in sc.fetch_board(sc.Fetcher(pause=0.5), dict(board), dt.date.today(), roles, details=15, notes=notes):
            jobs.append(Job(title=j["title"], company=board["company"], location=j.get("location") or "",
                            url=j.get("url") or "", source=board["type"], description=j.get("_text") or "",
                            posted_at=j.get("posted"), is_remote=bool(j.get("remote")),
                            job_type="internship" if "intern" in j["title"].lower() else None))
        errors += notes
    try:
        from jobspy import scrape_jobs
    except ImportError:
        errors.append("python-jobspy missing")
        return jobs, board
    for site in boards:
        try:
            df = scrape_jobs(site_name=[site], search_term=f'"{name}"' if site == "linkedin" else name,
                             location="India", results_wanted=60, hours_old=24 * days, country_indeed="India",
                             linkedin_fetch_description=False, verbose=0, description_format="markdown")
        except Exception as exc:
            errors.append(f"{site}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        for rec in df.to_dict("records") if df is not None else []:
            j = row_to_job(rec)
            if j and same(j.company, name):
                jobs.append(j)
    return jobs, board


def refresh(name: str, live: bool = True) -> dict:
    """Fetch live, score, store. Returns summary used by CLI and Review Center."""
    from .score import score
    db = DB()
    errors: list[str] = []
    board = None
    new = 0
    if live:
        found, board = fetch(name, errors)
        for j in found:
            score(j)
            if j.status == "filtered" and j.reasons and j.reasons[0].startswith("filtered: location"):
                j.flags.append("outside-karnataka")
            if db.upsert(j):
                new += 1
                db.set_score(j.id, j.score, j.reasons, j.flags, j.dream, j.status)
        if board and not any(same(b["company"], name) for b in config.cfg("companies").get("boards", []) or []):
            board["discovered"] = True
    entry = catalogue(db).get(key(name)) or {}
    rows = listings(name, db, sorted(entry.get("aliases", [])))
    return {"name": entry.get("name", name), "rows": rows, "new": new, "board": board, "errors": errors,
            "followed": any(same(f, name) for f in followed()), "outreach": outreach(entry.get("name", name))}


# ── follow list (config/companies.yaml → follow:) ──────────────────────────
def _companies_file() -> Path:
    return config.path("config", "companies.yaml")


def followed() -> list[str]:
    return list(config.cfg("companies").get("follow") or [])


def follow(name: str, board: dict | None = None) -> str:
    f = _companies_file()
    text = f.read_text(encoding="utf-8")
    if any(same(x, name) for x in followed()):
        return f"already following {name}"
    if not re.search(r"(?m)^follow:\s*$", text):
        text = text.rstrip() + ("\n\n# Companies you follow: searched by name on every scan, alerted at a lower bar.\n"
                                "# `hunt company <name>` → f to add; `hunt unfollow <name>` to remove.\nfollow:\n")
    text = re.sub(r"(?m)^follow:\s*$", "follow:\n  - " + yaml.safe_dump(name, default_flow_style=True).strip()
                  .removesuffix("\n...").strip(), text, count=1)
    if board and not any(b.get("token") == board.get("token") and b.get("type") == board.get("type")
                         for b in config.cfg("companies").get("boards", []) or []):
        line = (f"  - {{company: {board['company']!r}, type: {board['type']}, token: {board['token']}, "
                f"checked: {dt.date.today()}}}   # auto-discovered\n")
        text = re.sub(r"(?m)^boards:\s*\n", lambda m: m.group(0) + line, text, count=1)
    f.write_text(text, encoding="utf-8")
    yaml.safe_load(text)          # never leave a broken file
    config.reload()
    return f"following {name}" + (f" + added its {board['type']} board" if board else "")


def unfollow(name: str) -> str:
    f = _companies_file()
    lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
    out, in_follow, removed = [], False, False
    for ln in lines:
        if re.match(r"^follow:\s*$", ln):
            in_follow = True
        elif in_follow and not ln.startswith("  -") and ln.strip():
            in_follow = False
        if in_follow and ln.startswith("  -"):
            val = yaml.safe_load(ln.strip()[2:].strip()) if ln.strip()[2:].strip() else ""
            if same(str(val), name):
                removed = True
                continue
        out.append(ln)
    f.write_text("".join(out), encoding="utf-8")
    config.reload()
    return f"unfollowed {name}" if removed else f"{name} was not followed"


# ── outreach ───────────────────────────────────────────────────────────────
def outreach(name: str) -> dict:
    q = quote_plus
    prof = config.cfg("profile")
    edu = next((e for e in prof.get("education", []) if e.get("level") == "bachelors"), {})
    links = {
        "Recruiters in Bengaluru": f"https://www.linkedin.com/search/results/people/?keywords={q(name + ' recruiter Bengaluru')}",
        "Engineers in Bengaluru": f"https://www.linkedin.com/search/results/people/?keywords={q(name + ' engineer Bengaluru')}",
        "REVA University alumni there": f"https://www.linkedin.com/school/reva-university/people/?keywords={q(name)}",
        "All their LinkedIn jobs": f"https://www.linkedin.com/jobs/search/?keywords={q(name)}&location=Karnataka%2C%20India",
        "Glassdoor interviews": f"https://www.google.com/search?q={q(name + ' interview questions glassdoor')}",
    }
    first = (prof.get("first_name") or prof.get("name", "")).strip()
    note = (f"Hi <name>, I'm {first}, a final-year B.Tech {edu.get('branch', '')} student at "
            f"{(edu.get('institution') or '').split(',')[0]}. I'm applying for <role> at {name}. "
            f"I'd value 10 minutes on what the team looks for, and a referral if you think I fit. "
            f"Resume + GitHub: {prof.get('links', {}).get('github', '')}")
    return {"links": links, "note": note[:300]}
