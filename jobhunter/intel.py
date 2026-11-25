"""/intel <company>: what OUR scan history and the last 90 days of news say about a company's hiring.

Every line is counted from stored postings or a dated, linked headline. Patterns are stated as patterns with the
number of postings behind them, never as certainty; with too little data the answer is "not enough history yet".
"""
from __future__ import annotations

import datetime as dt
import html
import re
import statistics
from collections import Counter

from . import companies, config
from .db import DB
from .score import years_required

EXTRA_SKILLS = ["Python", "Java", "C++", "Rust", "Scala", "Kotlin", "JavaScript", "TypeScript", "SQL", "Spark",
                "Hadoop", "Kafka", "Airflow", "AWS", "Azure", "GCP", "Kubernetes", "Docker", "Terraform", "PyTorch",
                "TensorFlow", "scikit-learn", "Pandas", "NumPy", "LLMs", "RAG", "LangChain", "LangGraph", "NLP",
                "Computer Vision", "Deep Learning", "Machine Learning", "MLOps", "FastAPI", "Django", "Flask",
                "React", "Node.js", "Spring", "Microservices", "REST APIs", "GraphQL", "PostgreSQL", "MongoDB",
                "Redis", "Linux", "Git", "CI/CD", "Power BI", "Tableau", "Excel", "Statistics", "SAP", "Salesforce"]
SIGNALS = {"funding": r"rais(?:es|ed|ing) .{0,30}(?:funding|crore|million|\$|₹|rs\.?)|funding round|series [a-f] (?:round|funding)", "new office": r"new (?:office|campus|centre|center|facility)|opens? .{0,30}(?:office|centre|center|campus)",
           "layoffs": r"layoff|lay off|job cuts|retrench", "campus drive": r"campus|hiring drive|fresher|graduates? hiring|recruit",
           "new product": r"launch|unveil|introduc"}
MIN_REPEAT = 3                                     # postings of one role before a "likely next" is stated


def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _skills() -> list[str]:
    """Your skills section only: resume tags like 'testing' or 'safety' are labels, not skills to count."""
    return [str(x) for g in (config.cfg("profile").get("skills") or {}).values() for x in (g or []) if len(str(x)) > 1]


def _has(text: str, skill: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(skill.lower())}s?(?![a-z0-9])", text))


def _role(title: str) -> str:
    """'Sr. GenAI Engineer - II (Bengaluru)' → 'genai engineer': the role, not its level or place."""
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", (title or "").lower())
    t = re.sub(r"\s[-–|,/].*$", " ", t)
    t = re.sub(r"\b(?:senior|sr|junior|jr|i{1,3}|iv|[1-4])\b\.?", " ", t)
    return " ".join(t.split())


def rows(db: DB, name: str) -> list[dict]:
    names = companies.names_of(name)
    cur = db.c.execute("SELECT * FROM jobs WHERE kind='private'")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()
            if any(companies.same_company(r[cols.index("company")], n) for n in names)]


def _day(j: dict) -> dt.date | None:
    for k in ("posted_at", "found_at"):
        try:
            return dt.date.fromisoformat((j.get(k) or "")[:10])
        except ValueError:
            continue
    return None


def analyse(jobs: list[dict], mine: list[str]) -> dict:
    """Pure counts over the postings (testable without a DB)."""
    days = [(j, _day(j)) for j in jobs]
    days = [(j, d) for j, d in days if d]
    texts = [(j["title"] + " " + (j.get("description") or "")).lower() for j, _ in days]
    vocab = list(dict.fromkeys(mine + EXTRA_SKILLS))
    skill_share = {s: sum(_has(t, s) for t in texts) / len(texts) for s in vocab} if texts else {}
    top = sorted(((s, v) for s, v in skill_share.items() if v > 0), key=lambda x: -x[1])[:10]
    have = {m.lower() for m in mine}
    gaps = [s for s, v in top if v >= 0.30 and s.lower() not in have][:3]
    fresher = 0
    for j, _ in days:
        y = years_required(j.get("description") or "")
        fresher += (y is None or y <= 1) and not re.search(r"\b(?:senior|sr\.?|lead|principal|staff|manager)\b", j["title"].lower())
    by_role: dict[str, list[dt.date]] = {}
    for j, d in days:
        by_role.setdefault(_role(j["title"]), []).append(d)
    repeats, likely = [], []
    for role, ds in by_role.items():
        months = sorted({(d.year, d.month) for d in ds})
        if len(months) >= 2:
            repeats.append((role, len(ds), [dt.date(y, m, 1).strftime("%b %Y") for y, m in months]))
        if len(months) >= MIN_REPEAT:
            firsts = sorted(min(d for d in ds if (d.year, d.month) == ym) for ym in months)
            gaps_days = [(b - a).days for a, b in zip(firsts, firsts[1:])]
            med = statistics.median(gaps_days)
            likely.append((role, firsts[-1] + dt.timedelta(days=med), med, len(firsts),
                           [d.strftime("%b") for d in firsts]))
    open_for = [(dt.date.fromisoformat(j["deadline"][:10]) - d).days for j, d in days
                if j.get("deadline") and re.match(r"\d{4}-\d\d-\d\d", j["deadline"])]
    return {"n": len(days),
            "per_month": sorted(Counter(d.strftime("%Y-%m") for _, d in days).items())[-12:],
            "skills": top, "gaps": gaps,
            "places": Counter((j.get("location") or "?").split(",")[0].strip() for j, _ in days).most_common(5),
            "fresher": fresher, "weekday": Counter(d.strftime("%A") for _, d in days).most_common(2),
            "open_days": statistics.median(open_for) if open_for else None, "open_n": len(open_for),
            "repeats": sorted(repeats, key=lambda x: -x[1])[:5], "likely": sorted(likely, key=lambda x: x[1])[:3]}


def signals(news: list[dict]) -> list[tuple[str, dict]]:
    return [(k, n) for n in news for k, rx in SIGNALS.items() if re.search(rx, n["title"], re.I)]


def report(db: DB, name: str, today: dt.date | None = None, news_fn=companies.news) -> str:
    today = today or dt.date.today()
    a = analyse(rows(db, name), _skills())
    L = [f"<b>🔎 Intel: {_e(name)}</b>", f"<i>From {a['n']} postings we scanned, plus news from the last 90 days.</i>"]
    if not a["n"]:
        L.append("Not enough history yet: none of our scans has seen a posting from them. "
                 f"Add them with /watch {_e(name)} so every scan checks them.")
    else:
        L.append("<b>Roles posted per month</b>\n" + " · ".join(f"{m}: {c}" for m, c in a["per_month"]))
        L.append("<b>Top skills in their postings</b>\n" + ", ".join(f"{_e(s)} {v:.0%}" for s, v in a["skills"][:8]))
        L.append("<b>Where</b> " + ", ".join(f"{_e(p)} ({c})" for p, c in a["places"]))
        L.append(f"<b>Fresher-friendly</b> {a['fresher']} of {a['n']} ({a['fresher'] / a['n']:.0%}) ask ≤ 1 year")
        if a["n"] >= 5:
            L.append("<b>Usually posts on</b> " + ", ".join(f"{d} ({c})" for d, c in a["weekday"]))
        L.append(f"<b>How long postings stay open</b> median {a['open_days']:.0f} days (from {a['open_n']} with a deadline)"
                 if a["open_n"] >= 3 else "<b>How long postings stay open</b> not enough postings with a deadline yet")
        if a["repeats"]:
            L.append("<b>Roles they post again</b>\n" + "\n".join(f"• {_e(r)}: {n}× ({', '.join(ms)})" for r, n, ms in a["repeats"]))
        if a["likely"]:
            L.append("<b>Likely next</b> (a pattern, not a promise)\n" + "\n".join(
                f"• {_e(r)}: posted in {', '.join(ms)}; it came back about every {g:.0f} days → watch for one around "
                f"{d:%d %b %Y} (based on {n} postings)" for r, d, g, n, ms in a["likely"]))
        else:
            L.append(f"<b>Likely next</b> not enough history yet (needs one role posted in {MIN_REPEAT}+ different months)")
        if a["gaps"]:
            L.append("<b>What they hire for that your resume doesn't show</b> (in ≥ 30% of their postings)\n" +
                     "\n".join(f"{i}. {_e(s)} ({dict(a['skills'])[s]:.0%})" for i, s in enumerate(a["gaps"], 1)))
        else:
            L.append("<b>Skill gaps</b> none: every skill in ≥ 30% of their postings is on your resume")
    try:
        news = news_fn(name, 90, 8)
    except Exception as exc:                       # news is a bonus; the counts above still stand
        news = []
        L.append(f"<i>News couldn't be read ({type(exc).__name__}).</i>")
    sig = signals(news)
    if sig:
        L.append("<b>Hiring signals</b>\n" + "\n".join(f"• {k}: {n['date']} <a href=\"{_e(n['url'])}\">{_e(n['title'])}</a>"
                                                    for k, n in sig[:5]))
    if news:
        L.append("<b>News (90 days)</b>\n" + "\n".join(f"• {n['date']} <a href=\"{_e(n['url'])}\">{_e(n['title'])}</a> · {_e(n['source'])}"
                                                    for n in news[:5]))
    elif not sig:
        L.append("<b>News (90 days)</b> none found")
    return "\n\n".join(L)


def monthly(db: DB | None = None, news_fn=companies.news) -> list[str]:
    """1st of the month: one report per dream company."""
    db = db or DB()
    return [report(db, c, news_fn=news_fn) for c in config.cfg("profile").get("dream_companies") or []]
