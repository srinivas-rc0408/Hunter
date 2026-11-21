"""Fit scoring for private jobs: hard filters first, then a 0–100 score with reasons.

Hard filters (job is marked 'filtered', never alerted):
  • outside Karnataka (unless remote-in-India)
  • excluded title words (senior, lead, sales, …) or an excluded company
  • asks for more years of experience than you have (max_years_experience_required)
  • pay listed and below your floor (stipend ₹15k/month, CTC ₹4.5 LPA)
  • not a tech role

Score = title fit 35 + skills 25 + early-career 12 + freshness 10 + pay 8 + place 5
        + dream company bonus 15 (capped at 100).
"""
from __future__ import annotations

import datetime as dt
import re

from . import config
from .db import Job
from .util import norm

# "Engineer II", "SDE-3", "Data Scientist III", "Level 4": experienced levels, not fresher roles ("Engineer I" stays).
LEVEL = re.compile(r"\b(?:engineer|developer|scientist|analyst|sde|swe|programmer|consultant)\s*[-–]?\s*"
                   r"(?:ii|iii|iv|v|vi|[2-6])\b(?!\s*\d)|\b(?:level|grade)\s*[3-9]\b|\bl[4-9]\b")
GIG = re.compile(r"\$\s?\d|/\s?hr\b|per hour|\bai training\b|\bfreelance\b")
TECH_WORDS = ("engineer", "developer", "scientist", "data", "machine learning", "ml", "ai", "software",
              "sde", "analyst", "research", "python", "backend", "full stack", "fullstack", "frontend",
              "devops", "mlops", "cloud", "nlp", "computer vision", "llm", "genai", "intern", "trainee",
              "programmer", "automation", "qa", "sdet", "platform", "technology", "it ")
EARLY = ("intern", "internship", "fresher", "freshers", "graduate", "entry level", "entry-level", "trainee",
         "new grad", "campus", "2026 batch", "2027 batch", "0-1 year", "0 - 1 year", "0-2 years", "junior",
         "associate", "apprentice", "early career")
ROLE_SYNONYMS = {
    "ai/ml engineer": ["ai ml", "aiml", "ai engineer", "ml engineer", "machine learning engineer"],
    "machine learning engineer": ["machine learning", "ml engineer", "ml ops", "mlops"],
    "ai engineer": ["ai engineer", "artificial intelligence", "generative ai", "genai", "llm", "applied ai"],
    "data scientist": ["data scientist", "data science", "applied scientist"],
    "software engineer": ["software engineer", "software developer", "swe", "backend", "full stack", "python developer"],
    "sde": ["sde", "software development engineer"],
    "mlops engineer": ["mlops", "ml ops", "ml platform"],
    "research engineer": ["research engineer", "research intern", "research associate"],
    "data analyst": ["data analyst", "analytics"],
}


def _profile():
    return config.cfg("profile")


def is_dream(company: str, prof: dict | None = None) -> str | None:
    prof = prof or _profile()
    c = norm(company)
    aliases = prof.get("dream_aliases", {}) or {}
    for d in prof.get("dream_companies", []) or []:
        names = [norm(d)] + [norm(a) for a in aliases.get(d, [])]
        if any(n and (n == c or re.search(rf"\b{re.escape(n)}\b", c)) for n in names):
            return d
    return None


def place_ok(job: Job, places: list[str]) -> tuple[bool, str]:
    loc = (job.location or "").lower()
    desc_head = (job.description or "")[:600].lower()
    if any(p in loc for p in places):
        return True, "Karnataka"
    remote = job.is_remote or "remote" in loc or "work from home" in loc
    rest = re.sub(r"remote|work from home|wfh|anywhere|hybrid|[^a-z]", "", loc)
    if remote and ("india" in loc or not rest):
        return True, "remote (India)"
    if loc.strip() in ("", "india", "in") and any(p in desc_head for p in places):
        return True, "Karnataka (from description)"
    return False, job.location or "unknown"


def years_required(text: str) -> int | None:
    t = (text or "").lower()
    pats = [r"(\d{1,2})\s*\+\s*(?:years|yrs)", r"(\d{1,2})\s*(?:-|to|–)\s*\d{1,2}\s*(?:years|yrs)",
            r"(?:minimum|min\.?|at least)\s*(?:of\s*)?(\d{1,2})\s*(?:years|yrs)",
            r"experience\s*[:\-]?\s*(\d{1,2})\s*(?:-|to|–)\s*\d{1,2}", r"(\d{1,2})\s*(?:years|yrs)\s*(?:of)?\s*(?:relevant|professional|industry|work)?\s*experience"]
    vals = []
    for p in pats:
        vals += [int(m) for m in re.findall(p, t[:4000])]
    vals = [v for v in vals if v < 25]
    return min(vals) if vals else None


def monthly_inr(job: Job, high: bool = False) -> tuple[float | None, float | None]:
    """(monthly, yearly) INR from the LISTED pay (the low end, or the high end with high=True), else (None, None).
    A board's estimate, or a figure whose unit is clearly wrong ("60,000 P.A." for an engineer), counts as unknown."""
    lo = (job.pay_max or job.pay_min) if high else (job.pay_min or job.pay_max)
    if not lo or job.pay_estimate:
        return None, None
    cur = (job.currency or "INR").upper()
    per = (job.pay_period or "").lower()
    if cur == "INR" and ((per == "year" and (job.pay_max or lo) < 100000) or (per == "month" and lo > 500000)):
        return None, None
    fx = {"INR": 1, "USD": 84, "EUR": 90, "GBP": 105}.get(cur, 1)
    lo *= fx
    if per == "hour":
        return lo * 160, lo * 160 * 12
    if per == "month":
        return lo, lo * 12
    if per == "week":
        return lo * 4.3, lo * 52
    if per == "day":
        return lo * 22, lo * 22 * 12
    if not per and lo < 200000:         # no unit given: a figure under 2L is a monthly stipend, not a CTC
        return lo, lo * 12
    return lo / 12, lo


SCAM = re.compile(r"\b(?:registration|processing|training|joining|security|interview|placement|refundable|onboarding|kit)"
                  r"\s+(?:fee|fees|charge|charges|deposit|amount)\b|\bpay(?:ing)? (?:to apply|for (?:the )?training)\b|"
                  r"\bdeposit of\b|\bjob guarantee|\bguaranteed (?:job|placement)|\bplacement guarantee|"
                  r"\b100 ?% (?:job|placement)|\bpay after placement|\btraining (?:followed by|then|and then|cum) "
                  r"(?:a )?(?:job|placement|employment)")
NEGATED = re.compile(r"\b(?:no|not|never|without|free of|zero|don'?t|doesn'?t|won'?t|nor)\b")
PERSONAL_MAIL = re.compile(r"@(?:gmail|yahoo|hotmail|outlook|rediffmail|ymail|live|aol)\.", re.I)


def scam_reason(job: Job, text: str) -> str | None:
    """Fee / deposit / training-then-job, or a personal mailbox as the recruiter. Private jobs only:
    govt notices legitimately list an application fee."""
    if job.kind != "private":
        return None
    for m in SCAM.finditer(text):           # "we will never charge a registration fee" is a disclaimer, not a fee
        if not NEGATED.search(re.split(r"[.;\n]", text[max(0, m.start() - 80):m.start()])[-1]):
            return f"asks “{m.group(0)}”"
    mail = next((e for e in job.emails or [] if PERSONAL_MAIL.search(e)), None)
    return "recruiter uses a personal email address" if mail else None


def score(job: Job, today: dt.date | None = None) -> Job:
    prof = _profile()
    s = config.cfg("search")
    tg = prof.get("targets", {})
    today = today or dt.date.today()
    title = norm(job.title)
    text = (job.title + "\n" + (job.description or "")).lower()
    reasons: list[str] = []
    flags: list[str] = []
    job.status = "new"

    dream = is_dream(job.company, prof)
    job.dream = bool(dream)

    def drop(why: str) -> Job:
        job.status, job.score = "filtered", 0
        job.reasons = [f"filtered: {why}"]
        return job

    # ── hard filters ───────────────────────────────────────────────────────
    ok, where = place_ok(job, [p.lower() for p in s.get("karnataka_places", ["karnataka", "bengaluru", "bangalore"])])
    if tg.get("karnataka_only", True) and not ok:
        return drop(f"location {where}")
    padded = f" {job.title.lower()} "
    for w in tg.get("exclude_title_words", []):
        if w.lower() in padded and not (dream and w.lower() in ("lead", "manager")):
            return drop(f"title has '{w.strip()}'")
    if any(norm(c) == norm(job.company) for c in tg.get("exclude_companies", [])):
        return drop("excluded company")
    if not any(re.search(rf"\b{re.escape(w.strip())}\b", title) for w in TECH_WORDS):
        return drop("not a tech role")
    yrs = years_required(job.description or "")
    max_yrs = tg.get("max_years_experience_required", 1)
    is_intern = job.job_type == "internship" or "intern" in title
    if yrs is not None and yrs > max_yrs and not is_intern and not dream:
        return drop(f"asks {yrs}+ years")
    lvl = LEVEL.search(job.title.lower())
    if lvl and not is_intern and not dream:
        return drop(f"level role '{lvl.group(0).strip()}' (needs experience)")
    if job.kind != "govt" and job.posted_at:
        try:
            age_d = (today - dt.date.fromisoformat(str(job.posted_at)[:10])).days
        except ValueError:
            age_d = None
        if age_d is not None and age_d > tg.get("max_posting_age_days", 45):
            return drop(f"posted {age_d} days ago (stale)")
    mon, yr = monthly_inr(job)
    top_m, top_y = monthly_inr(job, high=True)        # the floor drops a job only when even its top pay is under it
    if top_m is not None:
        if is_intern and top_m < tg.get("min_stipend_inr_month", 15000):
            return drop(f"stipend ₹{top_m:,.0f}/month < floor")
        if not is_intern and top_y and top_y < tg.get("min_ctc_inr_year", 450000) and top_y > 30000:
            return drop(f"CTC ₹{top_y / 1e5:.1f} LPA < floor")

    # ── score ──────────────────────────────────────────────────────────────
    pts = 0
    roles = [r.lower() for r in tg.get("roles", [])]
    best = 0
    for i, r in enumerate(roles):
        syns = ROLE_SYNONYMS.get(r, [r]) + [r]
        if any(norm(x) in title for x in syns):
            best = max(best, 35 - min(i, 6) * 3)
    if best:
        reasons.append(f"title fits target roles (+{best})")
    elif any(w in title for w in ("engineer", "developer", "scientist", "analyst")):
        best = 14
        reasons.append("tech title, not a top target (+14)")
    pts += best

    skills = [x for grp in prof.get("skills", {}).values() for x in (grp or [])]
    hits = [sk for sk in skills if re.search(rf"(?<![a-z]){re.escape(sk.lower())}(?![a-z])", text)]
    sk_pts = min(25, len(hits) * 4)
    if hits:
        reasons.append(f"skills match: {', '.join(hits[:7])} (+{sk_pts})")
    pts += sk_pts

    early = [w for w in EARLY if w in text]
    if early or is_intern or yrs == 0:
        pts += 12
        reasons.append(f"early-career ({(early or ['intern'])[0]}) (+12)")

    posted = None
    try:
        posted = dt.date.fromisoformat(str(job.posted_at)[:10]) if job.posted_at else None
    except ValueError:
        pass
    if posted:
        age = (today - posted).days
        f = 10 if age <= 1 else 7 if age <= 3 else 4 if age <= 7 else 0
        pts += f
        if f:
            reasons.append(f"posted {age}d ago (+{f})")
    else:
        pts += 5

    if mon is not None:
        good = tg.get("good_stipend_inr_month", 30000) if is_intern else tg.get("good_ctc_inr_year", 800000) / 12
        if mon >= good:
            pts += 8
            reasons.append(f"pay ₹{mon:,.0f}/month ≥ your good mark (+8)")
        else:
            pts += 3
    if where.startswith("Karnataka"):
        pts += 5
    if GIG.search(job.title.lower() + " " + (job.description or "")[:600].lower()):
        pts -= 20
        reasons.append("⚠ hourly / AI-training gig, not a job with a CTC (−20)")
    if dream:
        pts += 15
        reasons.insert(0, f"★ dream company: {dream} (+15)")
        flags.append("dream")
    if yrs is not None and yrs > 0 and not is_intern:
        flags.append(f"asks {yrs} yr")
        if yrs > max_yrs:
            pts -= 15
            reasons.append(f"⚠ asks {yrs}+ years: kept only because it's a dream company (−15)")
    if job.emails:
        flags.append("email-apply")
    if any(w in text for w in ("bond", "service agreement", "security deposit")):
        flags.append("bond?")
        reasons.append("⚠ mentions bond/service agreement")
    scam = scam_reason(job, text)
    if scam:
        flags.append("scam?")
        reasons.append(f"⚠ looks like a scam ({scam}): check before applying")
        pts -= 40
    job.score = max(0, min(100, pts))
    job.reasons, job.flags = reasons, flags
    return job
