"""LinkedIn / Indeed / Naukri / Glassdoor / Google Jobs through JobSpy.

JobSpy reads the public, logged-out search pages, so your LinkedIn account is
never used for scanning and can't be flagged by it.
"""
from __future__ import annotations

import copy
import math
import re
import time
from typing import Iterable

from .. import config
from ..db import Job
from ..util import console, pay_pair

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
    lo, hi = pay_pair(_val(r.get("min_amount")), _val(r.get("max_amount")))   # Naukri "0-24 Lacs": 0 = no min
    cur = _val(r.get("currency"))
    estimate = ("Glassdoor" if site == "glassdoor" else          # Glassdoor's p10–p90 range is its own model
                "Indeed" if "estimate" in str(_val(r.get("salary_source")) or "") else None)
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
        pay_min=lo,
        pay_max=hi,
        pay_period=period if (lo or hi) else None,
        currency=cur if (lo or hi) else None,
        pay_estimate=estimate if (lo or hi) else None,
        emails=list(emails or []),
        easy_apply=None,
    )



def _tag_indeed_estimates() -> None:
    """JobSpy fills Indeed pay with Indeed's own estimate when the employer listed none, and says nothing.
    Wrap its parser so those rows carry salary_source="indeed_estimate" (shown as "≈ … (estimate by Indeed)")."""
    import jobspy.indeed as ind
    from jobspy.model import Compensation
    if getattr(ind.get_compensation, "_tags", False):
        return

    class Pay(Compensation):
        salary_source: str | None = None
    real = ind.get_compensation

    def get_compensation(c):
        pay = real(c)
        if not c.baseSalary and c.estimated and (pay.min_amount or pay.max_amount):
            return Pay(**pay.model_dump(), salary_source="indeed_estimate")
        return pay
    get_compensation._tags = True
    ind.get_compensation = get_compensation


def enabled(light: bool = False) -> list[str]:
    """The boards a scan runs, in order: LinkedIn last (it rate-limits; everything else goes first)."""
    s = config.cfg("search")
    on = [b for b, o in s.get("boards", {}).items() if (o or {}).get("enabled") and b in SITE_NAMES]
    if light:
        on = [b for b in on if b in s.get("light_scan", {}).get("boards", on)]
    return sorted(on, key=lambda b: b == "linkedin")


def say_skipped(sites: Iterable[str] | None = None) -> None:
    """A board turned off because it blocks us: say so once, never retry."""
    for b, o in config.cfg("search").get("boards", {}).items():
        if (o or {}).get("skipped") and not (o or {}).get("enabled") and (not sites or b in set(sites)):
            console.print(f"  [yellow]{b.capitalize()} skipped ({o['skipped']})[/]")


def _rate_limited(exc: Exception | None, df, site: str) -> str | None:
    """JobSpy keeps a 429 in df.attrs["errors"] and returns what it got; older ones raise it."""
    msg = str(exc) if exc else str(((getattr(df, "attrs", None) or {}).get("errors") or {}).get(site) or "")
    return msg if "429" in msg else None


class RateLimited(Exception):
    pass


def _linkedin_text(job_url: str, timeout: float = 10) -> str | None:
    """One LinkedIn job page's full description (Markdown), read only for jobs that passed the filters."""
    import requests
    from bs4 import BeautifulSoup
    from jobspy.linkedin.constant import headers
    from jobspy.model import DescriptionFormat
    from jobspy.util import description_fields
    r = requests.get(job_url, headers=headers, timeout=timeout)
    if r.status_code == 429:
        raise RateLimited("response status code 429")
    if r.status_code != 200 or "/jobs/view/" not in r.url:       # a login wall redirects elsewhere
        return None
    div = BeautifulSoup(r.text, "html.parser").find("div", class_="show-more-less-html__markup")
    if not div:
        return None
    div.attrs.clear()
    return description_fields(div.prettify(formatter="html"), DescriptionFormat.MARKDOWN)["description"]


def _describe(jobs: list[Job], recs: list[dict], errors: list[str], pause: float, stop,
              stored=None, most: int = 10) -> None:
    """LinkedIn search cards have no description: read it for the jobs that pass the hard filters only, at most
    `most` pages a search. A job we already have keeps its stored text (stored(job_id) → description)."""
    from ..score import score
    for j, rec in zip(jobs, recs):
        if stop is not None and stop.is_set():
            return
        if j.description or score(copy.deepcopy(j)).status == "filtered":
            continue
        if stored is not None and (old := stored(j.id)):
            j.description = old
            continue
        if most <= 0:
            continue
        most -= 1
        try:
            text = _linkedin_text(str(_val(rec.get("job_url")) or j.url))
        except RateLimited:
            raise
        except Exception as exc:                        # one unreadable page: the card stays, without its text
            errors.append(f"linkedin description {j.url}: {type(exc).__name__}")
            continue
        if text:
            j.description = text
        time.sleep(pause)


def rotate(items: list, start: int) -> list:
    """items starting at `start`: the ones a cut-short scan didn't reach go first next time."""
    k = start % len(items) if items else 0
    return items[k:] + items[:k]


def scan(errors: list[str], light: bool = False, sites: Iterable[str] | None = None,
         hours_old: int | None = None, queries: list[dict] | None = None,
         locations: list[str] | None = None, quiet: bool = False, out: list | None = None,
         start: int = 0, cov: dict | None = None, stored=None) -> list[Job]:
    """Every enabled board × location × query. queries/locations override search.yaml (search on demand).
    Jobs go into `out` as each search lands, so a caller that stops waiting keeps what was found.
    One board at a time with a scan: searches start at `start` (rotation), and `cov` counts them as they run
    ({"done", "total"}; a set cov["stop"] event ends the board early)."""
    try:
        from jobspy import scrape_jobs  # heavy import; only when scanning
    except ImportError:
        errors.append("python-jobspy not installed: pip install python-jobspy")
        return []
    try:
        _tag_indeed_estimates()
    except (ImportError, AttributeError) as exc:      # a JobSpy upgrade moved it: say so, keep scanning
        errors.append(f"indeed estimate tag off: {exc}")
    s = config.cfg("search")
    boards = s.get("boards", {})
    if not quiet:
        say_skipped(sites)
    want_q, want_loc = queries, locations
    queries = s.get("queries", [])
    locations = s.get("locations", [s.get("default_location", "India")])
    on = enabled(light)
    if light:
        ls = s.get("light_scan", {})
        locations = ls.get("locations", locations[:1])
        queries = queries[: ls.get("max_queries", 6)] + [q for q in queries if "birla" in q["term"].lower()]
    if sites:
        on = [b for b in on if b in set(sites)]
    queries, locations = want_q or queries, want_loc or locations
    proxies = s.get("proxies") or None
    pause = float(s.get("pause_between_queries_sec", 3))
    hours = hours_old or s.get("hours_old", 48)
    out = [] if out is None else out
    cov = {} if cov is None else cov
    stop = cov.get("stop")
    for site in on:
        opt = boards.get(site) or {}
        name = {"linkedin": "LinkedIn"}.get(site, site.capitalize())
        # location outer: every query gets Bengaluru before any query gets Mysuru
        searches = [(loc, q) for loc in (locations[:1] if site == "google" else locations) for q in queries]
        cov.update(total=len(searches), done=0)
        searches = rotate(searches, start)[: opt.get("max_searches") or None]
        for loc, q in searches:
            if stop is not None and stop.is_set():
                break
            if site == "google":   # the google query names the city itself
                kw = dict(google_search_term=f"{q['term']} jobs near {loc.split(',')[0]} since {max(1, hours // 24)} days ago")
            else:
                kw = dict(search_term=q["term"])
            df, err = None, None
            try:
                df = scrape_jobs(
                    site_name=[site], location=loc, results_wanted=int(opt.get("results", 30)),
                    hours_old=hours, country_indeed="India", job_type=q.get("job_type"),
                    linkedin_fetch_description=bool(opt.get("fetch_description")) and site == "linkedin",
                    proxies=proxies, verbose=0, description_format="markdown", **kw)
            except Exception as exc:  # JobSpy raises many kinds; never kill the scan
                err = exc
                errors.append(f"{site} '{q['term']}' @ {loc}: {type(exc).__name__}: {str(exc)[:160]}")
            limited = _rate_limited(err, df, site)
            recs, jobs = [], []
            for rec in df.to_dict("records") if df is not None else []:
                if j := row_to_job(rec, q.get("job_type")):
                    recs.append(rec)
                    jobs.append(j)
            if site == "linkedin" and not opt.get("fetch_description") and not limited:
                try:
                    _describe(jobs, recs, errors, float(opt.get("description_pause_sec", 1)), stop, stored,
                              int(opt.get("max_descriptions", 10)))
                except RateLimited as exc:
                    limited = str(exc)
            out.extend(jobs)
            if not limited:
                cov["done"] = cov.get("done", 0) + 1        # a 429'd search isn't done: it goes first next time
            if not quiet and not err:
                console.print(f"  {site:9} {q['term'][:32]:32} {loc.split(',')[0]:10} → {len(jobs)}")
            if limited:    # never wait and retry: stop this board for the run
                errors.append(f"{name} rate-limited, skipped rest ({cov['total'] - cov['done']} searches)")
                console.print(f"[yellow]{name} rate-limited, skipped rest[/]")
                break
            if not quiet and not err:
                time.sleep(pause)
    return out
