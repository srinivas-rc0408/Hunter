"""Internships and fresher roles: Internshala, Unstop, Wellfound. Public pages and APIs their robots.txt allows.

  Internshala  /internships/<category>-internship-in-bangalore/ and /jobs/<category>-jobs-in-bangalore/, plus the
               work-from-home pages (remote India). Cards give title, company, place, stipend/salary, duration or
               experience, and "N days ago"; the detail page (/internship/detail/, allowed) adds the start date,
               "apply by" date and the full text, for the first `max_details` cards.
  Unstop       /api/public/opportunity/search-result (robots.txt: Allow /api/public/*). Open listings only.
  Wellfound    /role/l/<role>/bangalore and /role/r/<role> (remote): the page embeds its listings (__NEXT_DATA__).

A page that redirects elsewhere (an unknown category) is ignored, never scanned as if it were the one asked for.
Pay is kept as listed: a lump-sum stipend becomes per month only when the duration is listed.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from urllib.parse import quote

from .. import config
from ..db import Job
from ..util import IST, Http, console

UNIS = "https://internshala.com"


def _today() -> dt.date:
    return dt.datetime.now(IST).date()


def ago(text: str, today: dt.date | None = None) -> str | None:
    """'3 weeks ago' / 'Today' / 'Few hours ago' → YYYY-MM-DD; anything else (e.g. 'Be an early applicant') → None."""
    t, today = (text or "").strip().lower(), today or _today()
    if t in ("today", "just now", "few hours ago") or re.fullmatch(r"\d+ hours? ago", t):
        return str(today)
    m = re.fullmatch(r"(\d+) (day|week|month)s? ago", t)
    if not m:
        return None
    return str(today - dt.timedelta(days=int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]))


def _months(duration: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(month|week)", (duration or "").lower())
    return None if not m else float(m.group(1)) / (4.345 if m.group(2) == "week" else 1)


def pay(text: str, duration: str = "") -> tuple[float | None, float | None, str | None, str]:
    """'₹ 12,000 - 15,000 /month' → (12000, 15000, 'month', ''); lump sum → per month when the duration is known;
    'Unpaid' → (None, None, None, 'unpaid'). The 4th value is a note for the description."""
    t = (text or "").lower()
    if "unpaid" in t:
        return None, None, None, "unpaid"
    m = re.search(r"₹\s*([\d,]+)(?:\s*-\s*([\d,]+))?", text or "")
    if not m:
        return None, None, None, ""
    lo = float(m.group(1).replace(",", ""))
    hi = float(m.group(2).replace(",", "")) if m.group(2) else None
    if "lump sum" in t:
        mo = _months(duration)
        if not mo:
            return None, None, None, f"stipend {m.group(0).strip()} lump sum (duration not listed)"
        return round(lo / mo), round(hi / mo) if hi else None, "month", f"stipend {m.group(0).strip()} lump sum over {duration}"
    per = "year" if "/year" in t or "per annum" in t else "month" if "/month" in t else "week" if "/week" in t else None
    return lo, hi, per, ""


# ── Internshala ──────────────────────────────────────────────────────────────
def internshala_cards(html: str, kind: str, today: dt.date | None = None) -> list[dict]:
    from bs4 import BeautifulSoup
    out = []
    for c in BeautifulSoup(html, "html.parser").select("div.individual_internship"):
        if not c.get("internshipid") or not c.get("data-href"):
            continue
        title = c.select_one(".job-title-href")
        company = c.select_one(".company-name")
        if not title or not company:
            continue
        f = {"places": [], "money": "", "duration": "", "experience": ""}
        for it in c.select(".row-1-item"):
            icon = " ".join((it.find("i") or {}).get("class", []))
            text = " ".join(it.get_text(" ").split())
            if "map-pin" in icon:
                f["places"] = [p.strip() for p in text.split(",") if p.strip()]
            elif "money" in icon:
                f["money"] = text
            elif "calendar" in icon:
                f["duration"] = text
            elif "briefcase" in icon:
                f["experience"] = text
        labels = [" ".join(x.get_text(" ").split()) for x in c.select(".color-labels span")]
        posted = next((d for d in (ago(x, today) for x in labels) if d), None)
        skills = [" ".join(s.get_text(" ").split()) for s in c.select(".job_skill")]
        about = " ".join((c.select_one(".about_job .text") or c).get_text(" ").split())[:400] if c.select_one(".about_job .text") else ""
        out.append({"kind": kind, "id": c["internshipid"], "url": UNIS + c["data-href"], "title": title.get_text(strip=True),
                    "company": company.get_text(strip=True), **f, "posted": posted, "skills": skills, "about": about,
                    "early": any("early applicant" in x.lower() for x in labels)})
    return out


def internshala_detail(html: str) -> dict:
    """Start date, apply-by date, duration and the full text from a detail page ("" when not listed)."""
    from bs4 import BeautifulSoup
    s = BeautifulSoup(html, "html.parser")
    out = {"start": "", "apply_by": "", "duration": "", "text": ""}
    for it in s.select(".other_detail_item"):
        head = " ".join((it.select_one(".item_heading") or it).get_text(" ").split()).lower()
        body = " ".join((it.select_one(".item_body") or it).get_text(" ").split())
        if head.startswith("start date"):
            out["start"] = "Immediately" if "immediately" in body.lower() else body
        elif head.startswith("apply by"):
            out["apply_by"] = body
        elif head.startswith("duration") and not out["duration"]:
            out["duration"] = body
    texts = [" ".join(t.get_text(" ").split()) for t in s.select(".internship_details .text-container")]
    out["text"] = "\n".join(t for t in texts if len(t) > 3)[:6000]
    return out


def _apply_by(text: str, today: dt.date | None = None) -> str | None:
    """"30 Nov' 26" → 2026-11-30."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*'?\s*(\d{2,4})", text or "")
    if not m:
        return None
    y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
    try:
        return str(dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {y}", "%d %b %Y").date())
    except ValueError:
        return None


def card_to_job(c: dict, detail: dict | None = None) -> Job:
    d = detail or {}
    duration = d.get("duration") or c.get("duration") or ""
    if c["kind"] == "job":
        duration = ""                                   # a job card's calendar icon is a start date, not a duration
    lo, hi, per, note = pay(c.get("money", ""), duration)
    remote = any("work from home" in p.lower() for p in c["places"])
    places = [p for p in c["places"] if "work from home" not in p.lower()]
    loc = ", ".join(places) + (", India" if places else "") if places else "Remote, India"
    details = {k: v for k, v in (("duration", duration), ("start_date", d.get("start")),
                                 ("apply_by", d.get("apply_by")), ("experience", c.get("experience"))) if v}
    desc = "\n".join(x for x in (
        f"Experience: {c['experience']}" if c.get("experience") else "",
        f"Duration: {duration}" if duration else "", f"Start date: {d['start']}" if d.get("start") else "",
        note, "Skills: " + ", ".join(c["skills"]) if c.get("skills") else "", d.get("text") or c.get("about") or "") if x)
    j = Job(title=c["title"] + (" Intern" if c["kind"] == "internship" and "intern" not in c["title"].lower() else ""),
            company=c["company"], location=loc, url=c["url"], source="internshala", description=desc,
            posted_at=c.get("posted"), job_type="internship" if c["kind"] == "internship" else "fulltime",
            is_remote=remote, pay_min=lo, pay_max=hi, pay_period=per if (lo or hi) else None,
            currency="INR" if (lo or hi) else None, details=details, deadline=_apply_by(d.get("apply_by", "")))
    if c.get("early"):
        j.flags = ["few-applicants"]
    return j


def internshala(http: Http, errors: list[str], cfg: dict) -> list[Job]:
    pages = []
    for place in cfg.get("places", ["bangalore", "work-from-home"]):
        for cat in cfg.get("internships", []):
            pages.append(("internship", f"/internships/work-from-home-{cat}-internships/" if place == "work-from-home"
                          else f"/internships/{cat}-internship-in-{place}/"))
        for cat in cfg.get("jobs", []):
            pages.append(("job", f"/jobs/work-from-home-{cat}-jobs/" if place == "work-from-home"
                          else f"/jobs/{cat}-jobs-in-{place}/"))
    cards: dict[str, dict] = {}
    for kind, path in pages:
        url = UNIS + path
        if not http.allowed(url):
            errors.append(f"internshala: robots.txt disallows {path}")
            continue
        try:
            r = http.get(url)
        except Exception as exc:
            errors.append(f"internshala {path}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        if r.status_code != 200 or r.url.rstrip("/") != url.rstrip("/"):
            continue                                    # unknown category: Internshala redirects to a generic page
        for c in internshala_cards(r.text, kind):
            cards.setdefault(c["id"], c)
    out, left = [], int(cfg.get("max_details", 15))
    newest = sorted(cards.values(), key=lambda c: c.get("posted") or "", reverse=True)
    for c in newest:
        detail = None
        if left > 0 and http.allowed(c["url"]):
            left -= 1
            try:
                r = http.get(c["url"])
                detail = internshala_detail(r.text) if r.status_code == 200 else None
            except Exception as exc:
                errors.append(f"internshala detail: {type(exc).__name__}")
        out.append(card_to_job(c, detail))
    return out


# ── Unstop ───────────────────────────────────────────────────────────────────
def unstop_items(data: dict) -> list[Job]:
    out = []
    for it in ((data or {}).get("data") or {}).get("data") or []:
        reg = it.get("regnRequirements") or {}
        if reg.get("reg_status") not in (None, "STARTED") or not it.get("title"):
            continue                                    # registrations closed
        jd = it.get("jobDetail") or {}
        org = (it.get("organisation") or {}).get("name") or ""
        if not org:
            continue
        cities = [x.get("city") for x in (it.get("locations") or []) if x.get("city")] or list(jd.get("locations") or [])
        countries = {x.get("country") for x in (it.get("locations") or []) if x.get("country")}
        if countries and "India" not in countries:
            continue                                    # e.g. a UK listing
        remote = jd.get("type") == "wfh"
        lo = jd.get("min_salary") if jd.get("show_salary") and not jd.get("not_disclosed") else None
        hi = jd.get("max_salary") if lo is not None else None
        per = {"monthly": "month", "annually": "year", "weekly": "week"}.get(jd.get("pay_in") or "")
        intern = it.get("subtype") == "internships"
        exp = ", ".join(json.loads(reg.get("eligibility") or "{}").get("experience") or []) if reg.get("eligibility") else ""
        skills = [s.get("skill") or s.get("name") for s in (it.get("required_skills") or []) if isinstance(s, dict)]
        from bs4 import BeautifulSoup
        text = " ".join(BeautifulSoup(it.get("details") or "", "html.parser").get_text(" ").split())[:5000]
        j = Job(title=it["title"], company=org,
                location=(", ".join(cities) + ", India") if cities else ("Remote, India" if remote else "India"),
                url=f"https://unstop.com/{it.get('public_url') or ''}", source="unstop",
                description="\n".join(x for x in (f"Experience: {exp}" if exp and exp != "all" else "",
                                                  "Skills: " + ", ".join(s for s in skills if s) if skills else "", text) if x),
                posted_at=(it.get("approved_date") or it.get("updated_at") or "")[:10] or None,
                job_type="internship" if intern else "fulltime", is_remote=remote,
                pay_min=lo or None, pay_max=hi or None, pay_period=per if (lo or hi) else None,
                currency="INR" if (lo or hi) else None,
                deadline=(reg.get("end_regn_dt") or it.get("end_date") or "")[:10] or None)
        out.append(j)
    return out


def unstop(http: Http, errors: list[str], cfg: dict) -> list[Job]:
    out: list[Job] = []
    for opp in ("internships", "jobs"):
        for term in cfg.get("terms", []):
            url = (f"https://unstop.com/api/public/opportunity/search-result?opportunity={opp}"
                   f"&searchTerm={quote(term)}&per_page={int(cfg.get('per_page', 20))}&page=1")
            if not http.allowed(url):
                errors.append("unstop: robots.txt disallows its public search API")
                return out
            try:
                r = http.get(url, headers={"Accept": "application/json"})
                out += unstop_items(r.json()) if r.status_code == 200 else []
            except Exception as exc:
                errors.append(f"unstop '{term}': {type(exc).__name__}: {str(exc)[:120]}")
    return out


# ── Wellfound ────────────────────────────────────────────────────────────────
def _wf_pay(comp: str, intern: bool) -> tuple[float | None, float | None, str | None, str | None]:
    """'₹20L – ₹30L • No equity' → (2000000, 3000000, 'year', 'INR'). Only ₹ L (lakh/year) and ₹ K for internships
    (per month) are read; anything else stays in the text, never guessed."""
    nums = re.findall(r"₹\s*([\d.]+)\s*([LK])", comp or "")
    if not nums:
        return None, None, None, None
    unit = {n[1] for n in nums}
    if unit == {"L"}:
        v = [float(n[0]) * 100000 for n in nums]
        return v[0], v[1] if len(v) > 1 else None, "year", "INR"
    if unit == {"K"} and intern:
        v = [float(n[0]) * 1000 for n in nums]
        return v[0], v[1] if len(v) > 1 else None, "month", "INR"
    return None, None, None, None


def wellfound_items(html: str) -> list[Job]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html or "", re.S)
    if not m:
        return []
    ap = ((json.loads(m.group(1)).get("props") or {}).get("pageProps") or {}).get("apolloState", {}).get("data") or {}
    owner = {}
    for v in ap.values():
        if v.get("__typename") == "StartupResult":
            for ref in v.get("highlightedJobListings") or []:
                owner[ref.get("__ref")] = v.get("name")
    out = []
    for k, v in ap.items():
        if v.get("__typename") != "JobListingSearchResult" or not owner.get(k):
            continue
        intern = v.get("jobType") == "internship"
        lo, hi, per, cur = _wf_pay(v.get("compensation") or "", intern)
        places = v.get("locationNames") or []
        remote = bool(v.get("remote"))
        yrs = v.get("yearsExperienceMin")
        posted = dt.datetime.fromtimestamp(v["liveStartAt"], IST).date().isoformat() if v.get("liveStartAt") else None
        out.append(Job(title=v.get("title") or "", company=owner[k],
                       location=(", ".join(places) + ", India") if places else ("Remote, India" if remote else "India"),
                       url=f"https://wellfound.com/jobs/{v['id']}-{v.get('slug') or ''}", source="wellfound",
                       description="\n".join(x for x in (f"Experience: {yrs}+ years" if yrs is not None else "",
                                                         f"Pay: {v['compensation']}" if v.get("compensation") else "",
                                                         v.get("description") or "") if x),
                       posted_at=posted, job_type="internship" if intern else {"contract": "contract"}.get(
                           v.get("jobType"), "fulltime"), is_remote=remote, pay_min=lo, pay_max=hi,
                       pay_period=per, currency=cur))
    return out


def wellfound(http: Http, errors: list[str], cfg: dict) -> list[Job]:
    out: list[Job] = []
    for role in cfg.get("roles", []):
        for place in cfg.get("places", ["bangalore", "remote"]):
            url = f"https://wellfound.com/role/r/{role}" if place == "remote" else f"https://wellfound.com/role/l/{role}/{place}"
            if not http.allowed(url):
                errors.append(f"wellfound: robots.txt disallows {url}")
                continue
            try:
                r = http.get(url)
            except Exception as exc:
                errors.append(f"wellfound {role}/{place}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            if r.status_code != 200 or r.url.rstrip("/") != url.rstrip("/"):
                continue                                # unknown role: redirected to a generic page
            out += wellfound_items(r.text)
    return out


def scan(errors: list[str], quiet: bool = False) -> list[Job]:
    """Every enabled internship/fresher source in config/search.yaml → internships."""
    cfg = config.cfg("search").get("internships") or {}
    http = Http(pause=float(cfg.get("pause_sec", 2.0)))
    out: list[Job] = []
    for name, fn in (("internshala", internshala), ("unstop", unstop), ("wellfound", wellfound)):
        c = cfg.get(name) or {}
        if not c.get("enabled", True):
            continue
        try:
            got = fn(http, errors, c)
        except Exception as exc:                        # one broken site never stops the others
            errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:160]}")
            got = []
        if not quiet:
            console.print(f"  {name:11} → {len(got)}")
        out += got
    return out
