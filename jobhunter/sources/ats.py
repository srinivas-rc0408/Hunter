"""Company career boards (Greenhouse, Lever, Ashby, Workday, Eightfold).

Reuses the hunter-skill scout's tested parsers (skills/hunter-scout/jobs.py),
so a fix in one place fixes both. These public APIs are built for job seekers
and usually list a role hours before it reaches LinkedIn or Naukri.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
from functools import lru_cache

from .. import config
from ..db import Job


@lru_cache(maxsize=1)
def scout():
    f = config.path("skills", "hunter-scout", "jobs.py")
    spec = importlib.util.spec_from_file_location("hunter_scout_jobs", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


INDIA = ("india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai", "mumbai", "gurgaon",
         "gurugram", "noida", "delhi", "remote", "apac", "asia", "anywhere", "karnataka", "mysuru")


def _pay(p: dict | None):
    from ..util import pay_pair
    lo, hi = pay_pair((p or {}).get("min"), (p or {}).get("max"))
    if not (lo or hi):
        return None, None, None, None
    return lo, hi, p.get("period"), p.get("currency")


def scan(errors: list[str], offline: str | None = None, out: list | None = None) -> list[Job]:
    """Jobs go into `out` board by board, so a caller that stops waiting keeps what was found."""
    sc = scout()
    boards, extra = [], []
    for b in config.cfg("companies").get("boards", []) or []:
        (boards if b.get("type") in ("greenhouse", "lever", "ashby", "workday", "eightfold") else extra).append(b)        # watchlist (company search → "Watch"): scout-type boards join the loop, the rest go to ats_more
    try:
        from ..companies import watchlist
        for w in watchlist():
            if w.get("type") in ("greenhouse", "lever", "ashby", "workday"):
                boards.append(w)
            elif w.get("type") not in ("page", None):
                extra.append(w)
    except Exception as exc:
        errors.append(f"watchlist: {exc}")
    roles = config.cfg("profile").get("targets", {}).get("roles", [])
    india_only = config.cfg("search").get("company_boards", {}).get("india_only", True)
    fetcher = sc.Fetcher(offline=offline, pause=0.6)
    today = dt.date.today()
    out = [] if out is None else out
    for b in boards:
        notes: list[str] = []
        board = dict(b)
        board.setdefault("token", board.get("tenant") or board["company"].lower())
        try:
            jobs = sc.fetch_board(fetcher, board, today, roles, details=12, notes=notes)
        except Exception as exc:                  # one slow or broken board (timeout, bad JSON) never stops the rest
            errors.append(f"{b.get('type')} {board['token']}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        errors.extend(notes)
        for j in jobs:
            loc = (j.get("location") or "").lower()
            if india_only and not any(w in loc for w in INDIA):
                continue
            mn, mx, per, cur = _pay(j.get("pay"))
            out.append(Job(
                title=j["title"], company=j["company"], location=j.get("location") or "",
                url=j.get("url") or "", source=j.get("source") or b["type"],
                description=j.get("_text") or "", posted_at=j.get("posted"),
                job_type="internship" if "intern" in j["title"].lower() else None,
                is_remote=bool(j.get("remote")), pay_min=mn, pay_max=mx, pay_period=per, currency=cur))
    out.extend(_scan_more(extra, errors, india_only))
    return out


def _scan_more(boards: list[dict], errors: list[str], india_only: bool) -> list[Job]:
    if not boards:
        return []
    import requests

    from . import ats_more
    s = requests.Session()
    s.headers["User-Agent"] = ats_more.UA
    get = lambda u: s.get(u, timeout=15)
    post = lambda u, b: s.post(u, json=b, timeout=15)
    out = []
    for b in boards:
        try:
            rows = ats_more.fetch(b, get, post, b["company"])
        except Exception as exc:
            errors.append(f"{b['company']} ({b['type']}): {type(exc).__name__}")
            continue
        for r in rows:
            if india_only and not any(w in (r.get("location") or "").lower() for w in INDIA):
                continue
            out.append(Job(title=r["title"], company=b["company"], location=r.get("location") or "",
                           url=r.get("apply_url") or r["url"], source=r["source"], description=r.get("description") or "",
                           posted_at=r.get("posted"), job_type=r.get("job_type"), is_remote=bool(r.get("remote")),
                           pay_min=r.get("pay_min"), pay_max=r.get("pay_max"), currency=r.get("currency"),
                           pay_period=r.get("pay_period"), deadline=r.get("deadline")))
    return out
