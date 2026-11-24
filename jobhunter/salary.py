"""Approximate pay when a posting doesn't list it. Always marked as an estimate; never written to pay_min/pay_max.

Sources, checked live and in parallel (each one optional):
  1. AmbitionBox, that company + role: the 0–1 yr band (its page data, with the number of salaries behind it)
  2. AmbitionBox, the same role in that city generally: the lowest experience band
  3. Web search (Firecrawl, FIRECRAWL_API_KEY): Glassdoor India / Levels.fyi / Naukri / AmbitionBox pages whose
     snippet states a ₹ figure for freshers or entry level
Internships: a stipend per month. Results are cached 30 days (table salary_cache); a job's estimate is stored in
est_min / est_max (₹ per year) / est_source / est_at, and cards show "💰 not listed · ≈ ₹X–Y LPA est."
"""
from __future__ import annotations

import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .config import env
from .db import DB

CACHE_DAYS = 30
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
      "Accept-Language": "en-IN,en;q=0.9"}
ROLE_SLUG = {"ml": "machine-learning", "ai ml": "ai-ml", "ds": "data-scientist", "swe": "software-engineer",
             "sde": "software-development-engineer", "genai": "generative-ai", "gen ai": "generative-ai"}
CITY_SLUG = {"bengaluru": "bangalore", "bangalore": "bangalore", "karnataka": "bangalore", "mysuru": "mysore",
             "mysore": "mysore", "mangaluru": "mangalore", "hyderabad": "hyderabad", "pune": "pune", "chennai": "chennai",
             "mumbai": "mumbai", "delhi": "new-delhi", "gurugram": "gurgaon", "noida": "noida"}


def ensure(db: DB) -> None:
    db.c.execute("CREATE TABLE IF NOT EXISTS salary_cache (key TEXT PRIMARY KEY, at REAL, data TEXT)")


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    for a, b in ROLE_SLUG.items():
        s = re.sub(rf"\b{a}\b", b.replace("-", " "), s)
    return re.sub(r"\s+", "-", s)


def is_intern(role: str) -> bool:
    return bool(re.search(r"\bintern", role or "", re.I))


# ── sources ────────────────────────────────────────────────────────────────
def _ambitionbox(url: str) -> tuple[str, list[dict]] | None:
    """(final url, bands) from an AmbitionBox salary page; bands = [{lo_exp, hi_exp, min, max, n}] in ₹/year."""
    try:
        r = requests.get(url, headers=UA, timeout=15)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S) if r.ok else None
        if not m:
            return None
        sd = json.loads(m.group(1))["props"]["pageProps"].get("salaryData") or {}
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
    d = sd.get("data") or sd.get("profileSalaryData") or {}
    bands = []
    for b in d.get("experienceLevels") or d.get("experienceDistribution") or []:
        lo, hi = b.get("minExp"), b.get("maxExp")
        if lo is None and b.get("years"):
            lo, hi = (int(x) for x in str(b["years"]).split("-")[:2])
        n = int(b.get("count") or b.get("datapoints") or 0)
        if b.get("typicalMinCtc") and b.get("typicalMaxCtc") and n:
            bands.append({"lo_exp": int(lo), "hi_exp": int(hi), "min": float(b["typicalMinCtc"]),
                          "max": float(b["typicalMaxCtc"]), "n": n})
    return (r.url, sorted(bands, key=lambda b: b["lo_exp"])) if bands else None


def _band(found, label: str, kind: str) -> dict | None:
    if not found:
        return None
    url, bands = found
    b = bands[0]                                            # the lowest experience band = closest to a fresher
    return {"name": f"AmbitionBox · {label} · {b['lo_exp']}–{b['hi_exp']} yr ({b['n']} salaries)", "url": url,
            "min": b["min"], "max": b["max"], "n": b["n"], "fresher": b["lo_exp"] == 0, "kind": kind}


def company_source(company: str, role: str) -> dict | None:
    return _band(_ambitionbox(f"https://www.ambitionbox.com/salaries/{slug(company)}-salaries/{slug(role)}"),
                 f"{company} {role}", "company")


def role_source(role: str, location: str) -> dict | None:
    city = CITY_SLUG.get((location or "bengaluru").split(",")[0].strip().lower(), "bangalore")
    return _band(_ambitionbox(f"https://www.ambitionbox.com/profile/{slug(role)}-salary/{city}-location"),
                 f"{role} in {city.replace('-', ' ').title()}", "role")


RANGE = re.compile(r"₹\s?([\d.,]+)\s*(k|l|lpa|lakhs?|lacs?)?\s*(?:-|–|to)\s*₹?\s?([\d.,]+)\s*(k|l|lpa|lakhs?|lacs?)?"
                   r"\s*(per month|/month|a month|pm|per annum|p\.a\.|/yr|per year|lpa)?", re.I)
FRESH = re.compile(r"fresher|entry[- ]level|0\s*[-–]\s*1 (?:yr|year)|starting|graduate|campus|intern|stipend", re.I)


def _to_year(v: str, unit: str | None, per: str | None) -> float | None:
    try:
        x = float(v.replace(",", ""))
    except ValueError:
        return None
    u = (unit or "").lower()
    if u.startswith(("l", "lpa")):
        x *= 1e5
    elif u == "k":
        x *= 1e3
    if per and "month" in per.lower() or (per or "").lower() == "pm":
        x *= 12
    return x if 20_000 <= x <= 2e7 else None


def web_sources(company: str, role: str) -> list[dict]:
    """₹ ranges for freshers stated in search snippets from salary sites. Needs FIRECRAWL_API_KEY; else []."""
    key = env("FIRECRAWL_API_KEY")
    if not key:
        return []
    try:
        r = requests.post("https://api.firecrawl.dev/v2/search", timeout=20, headers={"Authorization": f"Bearer {key}"},
                          json={"query": f"{company} {role} fresher salary India glassdoor OR levels.fyi OR naukri OR ambitionbox",
                                "limit": 8})
        data = r.json().get("data") if r.ok else None
    except (requests.RequestException, ValueError):
        return []
    web = (data or {}).get("web") if isinstance(data, dict) else data
    out = []
    for w in web or []:
        text, url = f"{w.get('title') or ''} {w.get('description') or ''}", w.get("url") or ""
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        role_word = next((x for x in slug(role).split("-") if len(x) > 2 and x not in ("intern", "engineer")), slug(role).split("-")[0])
        if not re.search(r"glassdoor|levels\.fyi|naukri", host) or company.split()[0].lower() not in text.lower() \
                or role_word not in text.lower() or not FRESH.search(text):
            continue                                # only the named salary sites, about this role, for freshers
        for m in RANGE.finditer(text):
            unit = m.group(2) or m.group(4)
            lo, hi = _to_year(m.group(1), unit, m.group(5)), _to_year(m.group(3), unit, m.group(5))
            if lo and hi and lo <= hi <= 3 * lo:     # a 1–20 LPA "range" is a whole company, not a fresher band
                out.append({"name": f"{host.split('.')[0].title()} (search result, fresher)", "url": url, "min": lo, "max": hi, "n": 0,
                            "fresher": True, "kind": "web"})
                break
    return out[:2]


# ── the estimate ───────────────────────────────────────────────────────────
def fmt(lo: float, hi: float, intern: bool) -> str:
    if intern:
        a, b = round(lo / 12 / 1000), round(hi / 12 / 1000)
        return f"₹{a}k/month" if a == b else f"₹{a}–{b}k/month"
    a, b = f"{lo / 1e5:.1f}".removesuffix(".0"), f"{hi / 1e5:.1f}".removesuffix(".0")
    return f"₹{a} LPA" if a == b else f"₹{a}–{b} LPA"


def estimate(role: str, company: str = "", location: str = "Bengaluru", experience: str = "fresher",
             db: DB | None = None, lookup=None) -> dict:
    """{'min','max' (₹/year), 'intern', 'sources', 'confidence', 'note', 'at'} or {'sources': [], 'note': why}.
    lookup: (company_source, role_source, web_sources) for tests."""
    db = db or DB()
    ensure(db)
    key = "|".join(x.lower().strip() for x in (role, company, location, experience))
    hit = db.c.execute("SELECT at, data FROM salary_cache WHERE key=?", (key,)).fetchone()
    if hit and time.time() - hit[0] < CACHE_DAYS * 86400:
        return {**json.loads(hit[1]), "cached": True}
    cs, rs, ws = lookup or (company_source, role_source, web_sources)
    with ThreadPoolExecutor(max_workers=3) as ex:
        fc = ex.submit(cs, company, role) if company else None
        fr = ex.submit(rs, role, location)
        fw = ex.submit(ws, company, role) if company else None
        got = [fc.result() if fc else None, fr.result(), *(fw.result() if fw else [])]
    sources = [s for s in got if s]
    res = {"role": role, "company": company, "intern": is_intern(role), "sources": sources,
           "at": time.strftime("%d %b %Y")}
    if not sources:
        res["note"] = "no salary data found for this company and role, or for the role generally"
        return res
    comp = next((s for s in sources if s["kind"] == "company" and s["n"] >= 3), None)
    main = comp or next((s for s in sources if s["kind"] == "role"), sources[0])
    res.update(min=main["min"], max=main["max"])
    mid = lambda s: (s["min"] + s["max"]) / 2               # noqa: E731
    others = [s for s in sources if s is not main]
    spread = max([mid(s) for s in sources]) / min([mid(s) for s in sources])
    agree = any(max(mid(s), mid(main)) / min(mid(s), mid(main)) <= 1.35 for s in others)
    notes = []
    if spread > 1.6:
        notes.append("sources disagree a lot: " + " vs ".join(fmt(s["min"], s["max"], res["intern"]) for s in sources))
    if not main["fresher"]:
        notes.append("no fresher (0–1 yr) data: this is the lowest band reported")
    if not comp and company:
        notes.append(f"no {company}-specific data: this is the role in general")
    if comp and comp["n"] >= 20 and comp["fresher"] and agree and spread <= 1.6:
        res["confidence"] = "high"
    elif comp and (comp["fresher"] or comp["n"] >= 10) and comp["n"] >= 5 and spread <= 1.6:
        res["confidence"] = "medium"
    else:
        res["confidence"] = "low"
    res["note"] = "; ".join(notes)
    db.c.execute("INSERT OR REPLACE INTO salary_cache VALUES (?,?,?)", (key, time.time(), json.dumps(res)))
    db.c.commit()
    return res


def as_html(res: dict) -> str:
    e = lambda s: html.escape(str(s or ""), quote=False)    # noqa: E731
    who = " · ".join(x for x in (res.get("role"), res.get("company")) if x)
    if "min" not in res:
        return f"💰 <b>{e(who)}</b>: {e(res.get('note') or 'no data')}. No estimate (I won't guess)."
    src = "; ".join(f'<a href="{html.escape(s["url"], quote=True)}">{e(s["name"])}</a> {e(fmt(s["min"], s["max"], res["intern"]))}'
                    for s in res["sources"])
    return (f"💰 <b>{e(who)}</b>{' (stipend)' if res['intern'] else ''}\n"
            f"≈ <b>{e(fmt(res['min'], res['max'], res['intern']))}</b> (estimate, not from the posting)\n"
            f"sources (checked {e(res['at'])}): {src}\n"
            f"confidence: <b>{e(res['confidence'])}</b>" + (f"\n<i>{e(res['note'])}</i>" if res.get("note") else ""))


def store(db: DB, jid: str, res: dict) -> None:
    """Keep the estimate on the job, apart from the posting's own pay (pay_min/pay_max stay untouched)."""
    if "min" in res:
        db.c.execute("UPDATE jobs SET est_min=?, est_max=?, est_source=?, est_at=? WHERE id=?",
                     (res["min"], res["max"], f"{res['sources'][0]['name']} · confidence {res['confidence']}",
                      time.strftime("%Y-%m-%d"), jid))
        db.c.commit()
