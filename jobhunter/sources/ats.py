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
    if not p:
        return None, None, None, None
    return p.get("min"), p.get("max"), p.get("period"), p.get("currency")


def scan(errors: list[str], offline: str | None = None) -> list[Job]:
    sc = scout()
    boards = config.cfg("companies").get("boards", [])
    roles = config.cfg("profile").get("targets", {}).get("roles", [])
    india_only = config.cfg("search").get("company_boards", {}).get("india_only", True)
    fetcher = sc.Fetcher(offline=offline, pause=0.6)
    today = dt.date.today()
    out: list[Job] = []
    for b in boards:
        notes: list[str] = []
        board = dict(b)
        board.setdefault("token", board.get("tenant") or board["company"].lower())
        jobs = sc.fetch_board(fetcher, board, today, roles, details=12, notes=notes)
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
    return out
