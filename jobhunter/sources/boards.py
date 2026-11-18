"""LinkedIn / Indeed / Naukri / Glassdoor / Google Jobs through JobSpy.

JobSpy reads the public, logged-out search pages, so your LinkedIn account is
never used for scanning and can't be flagged by it.
"""
from __future__ import annotations

import math
import re
import time
from typing import Iterable

from .. import config
from ..db import Job
from ..util import console

SITE_NAMES = {"linkedin", "indeed", "naukri", "glassdoor", "google"}


def _val(v):
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
    except TypeError:
        pass
    s = str(v)
    return None if s in ("nan", "NaT", "None", "") else v


def _job_type(raw, term_type: str | None, title: str) -> str | None:
    t = (str(raw or "") + " " + title).lower()
    if "intern" in t:
        return "internship"
    if "contract" in t or "temporary" in t:
        return "contract"
    if "full" in t or "fulltime" in t:
        return "fulltime"
    return term_type


def row_to_job(r: dict, term_type: str | None = None) -> Job | None:
    title = _val(r.get("title"))
    company = _val(r.get("company"))
    if not title or not company:
        return None
    period = _val(r.get("interval"))
    period = {"yearly": "year", "monthly": "month", "hourly": "hour", "weekly": "week",
              "daily": "day"}.get(str(period).lower(), period) if period else None
    emails = _val(r.get("emails"))
    if isinstance(emails, str):
        emails = [e.strip() for e in re.split(r"[,;\s]+", emails) if "@" in e]
    posted = _val(r.get("date_posted"))
    url = _val(r.get("job_url_direct")) or _val(r.get("job_url")) or ""
    site = str(_val(r.get("site")) or "board")
    desc = str(_val(r.get("description")) or "")
    exp = _val(r.get("experience_range"))
    if exp:
        desc = f"Experience: {exp}\n" + desc
    return Job(
        title=str(title).strip(),
        company=str(company).strip(),
        location=str(_val(r.get("location")) or ""),
        url=str(url),
        source=site,
        description=desc,
        posted_at=str(posted)[:10] if posted else None,
        job_type=_job_type(_val(r.get("job_type")), term_type, str(title)),
        is_remote=bool(_val(r.get("is_remote"))) or "remote" in str(_val(r.get("work_from_home_type")) or "").lower(),
        pay_min=_float(_val(r.get("min_amount"))),
        pay_max=_float(_val(r.get("max_amount"))),
        pay_period=period,
        currency=_val(r.get("currency")),
        emails=list(emails or []),
        easy_apply=None,
    )


def _float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def scan(errors: list[str], light: bool = False, sites: Iterable[str] | None = None,
         hours_old: int | None = None) -> list[Job]:
    try:
        from jobspy import scrape_jobs  # heavy import; only when scanning
    except ImportError:
        errors.append("python-jobspy not installed: pip install python-jobspy")
        return []
    s = config.cfg("search")
    boards = s.get("boards", {})
    enabled = [b for b, o in boards.items() if (o or {}).get("enabled") and b in SITE_NAMES]
    queries = s.get("queries", [])
    locations = s.get("locations", [s.get("default_location", "India")])
    if light:
        ls = s.get("light_scan", {})
        enabled = [b for b in enabled if b in ls.get("boards", enabled)]
        locations = ls.get("locations", locations[:1])
        queries = queries[: ls.get("max_queries", 6)] + [q for q in queries if "birla" in q["term"].lower()]
    if sites:
        enabled = [b for b in enabled if b in set(sites)]
    proxies = s.get("proxies") or None
    pause = float(s.get("pause_between_queries_sec", 3))
    hours = hours_old or s.get("hours_old", 48)
    out: list[Job] = []
    for site in enabled:
        opt = boards.get(site) or {}
        for q in queries:
            for loc in locations:
                if site == "google":
                    kw = dict(google_search_term=f"{q['term']} jobs near {loc.split(',')[0]} since {max(1, hours // 24)} days ago")
                else:
                    kw = dict(search_term=q["term"])
                try:
                    df = scrape_jobs(
                        site_name=[site], location=loc, results_wanted=int(opt.get("results", 30)),
                        hours_old=hours, country_indeed="India", job_type=q.get("job_type"),
                        linkedin_fetch_description=bool(opt.get("fetch_description")) and site == "linkedin",
                        proxies=proxies, verbose=0, description_format="markdown", **kw)
                except Exception as exc:  # JobSpy raises many kinds; never kill the scan
                    msg = f"{site} '{q['term']}' @ {loc}: {type(exc).__name__}: {str(exc)[:160]}"
                    errors.append(msg)
                    if "429" in str(exc):
                        console.print(f"[yellow]{site} rate-limited; skipping the rest of {site} this run[/]")
                        break
                    continue
                n = 0
                for rec in df.to_dict("records") if df is not None else []:
                    j = row_to_job(rec, q.get("job_type"))
                    if j:
                        out.append(j)
                        n += 1
                console.print(f"  {site:9} {q['term'][:32]:32} {loc.split(',')[0]:10} → {n}")
                time.sleep(pause)
                if site == "google":
                    break   # google query already names the city; one location is enough
    return out
