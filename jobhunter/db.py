"""SQLite store: jobs (deduped across boards), events, govt notices, sent alerts.

One job seen on LinkedIn, Naukri and the company board is ONE row. The id is
built from company + title + city, and every board's link is kept.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from . import config
from .util import canonical_url, iso, norm, short_hash

STATUSES = ["new", "alerted", "queued", "filled", "needs_you", "applied", "interview",
            "offer", "rejected", "skipped", "expired", "filtered"]
# A status later in this tuple is "more advanced" and wins when merging two DBs.
RANK = {s: i for i, s in enumerate(["filtered", "new", "alerted", "queued", "expired", "skipped",
                                    "filled", "needs_you", "applied", "rejected", "interview", "offer"])}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'private',      -- private | govt
  title TEXT NOT NULL,
  company TEXT NOT NULL,
  location TEXT,
  is_remote INTEGER DEFAULT 0,
  job_type TEXT,                             -- internship | fulltime | contract | null
  url TEXT,                                  -- best link to apply
  urls TEXT DEFAULT '[]',                    -- every board's link (json)
  sources TEXT DEFAULT '[]',                 -- boards it was seen on (json)
  description TEXT,
  posted_at TEXT,
  found_at TEXT NOT NULL,
  pay_min REAL, pay_max REAL, pay_period TEXT, currency TEXT,
  pay_estimate TEXT,                         -- who estimated the pay ("Indeed"); NULL = the posting listed it
  details TEXT DEFAULT '{}',                 -- govt notice fields read from the notification (json)
  emails TEXT DEFAULT '[]',
  easy_apply INTEGER,
  score INTEGER DEFAULT 0,
  reasons TEXT DEFAULT '[]',
  flags TEXT DEFAULT '[]',
  dream INTEGER DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'new',
  status_at TEXT,
  deadline TEXT,
  notes TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_score ON jobs(score);
CREATE INDEX IF NOT EXISTS jobs_found ON jobs(found_at);
CREATE INDEX IF NOT EXISTS jobs_posted ON jobs(posted_at);
-- the WHERE clauses of db.query, bot.py and assistant.py: /top, /today, deadlines, company lookups
CREATE INDEX IF NOT EXISTS jobs_kind_status_score ON jobs(kind, status, score);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS jobs_deadline ON jobs(deadline);
CREATE TABLE IF NOT EXISTS events (
  job_id TEXT, at TEXT, kind TEXT, detail TEXT
);
DROP INDEX IF EXISTS events_job;                -- (job_id, kind): a prefix of the one below
CREATE INDEX IF NOT EXISTS events_job_kind_at ON events(job_id, kind, at);
CREATE INDEX IF NOT EXISTS events_kind_at ON events(kind, at);   -- applied today / ever
CREATE TABLE IF NOT EXISTS seen_links (      -- page watcher memory
  page TEXT, href TEXT, text TEXT, first_seen TEXT,
  PRIMARY KEY (page, href)
);
CREATE TABLE IF NOT EXISTS applications (   -- one row per apply attempt: what the bot did
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT, at TEXT, site TEXT, outcome TEXT, auto INTEGER, approved INTEGER, dry INTEGER,
  why TEXT, fields TEXT, screenshot TEXT, resume TEXT, resume_report TEXT, cover TEXT, page_url TEXT
);
CREATE INDEX IF NOT EXISTS applications_job ON applications(job_id);
CREATE TABLE IF NOT EXISTS runs (
  at TEXT, kind TEXT, found INTEGER, new INTEGER, alerted INTEGER, errors TEXT
);
"""


@dataclass
class Job:
    title: str
    company: str
    location: str = ""
    url: str = ""
    source: str = ""
    description: str = ""
    posted_at: str | None = None
    job_type: str | None = None
    is_remote: bool = False
    pay_min: float | None = None
    pay_max: float | None = None
    pay_period: str | None = None       # month | year | hour
    currency: str | None = None
    pay_estimate: str | None = None     # "Indeed" / "Glassdoor" when the board guessed the pay; None = listed
    details: dict = field(default_factory=dict)   # govt: post, pay, vacancies, fee, dates … read from the notice
    emails: list[str] = field(default_factory=list)
    easy_apply: bool | None = None
    kind: str = "private"
    deadline: str | None = None
    # filled by scoring
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    dream: bool = False
    status: str = "new"

    @property
    def id(self) -> str:
        return job_id(self.company, self.title, self.location)


_CITY_ALIASES = {"bangalore": "bengaluru", "mysore": "mysuru", "mangalore": "mangaluru",
                 "hubli": "hubballi", "belgaum": "belagavi", "gulbarga": "kalaburagi"}


def city_key(location: str | None) -> str:
    loc = norm(location)
    for a, b in _CITY_ALIASES.items():
        loc = loc.replace(a, b)
    first = re.split(r"\b(?:karnataka|india|in)\b", loc)[0].strip()
    return (first.split(" ")[0] if first else loc[:20]) or "any"


def job_id(company: str, title: str, location: str | None) -> str:
    t = re.sub(r"\b(?:i{1,3}|iv|1|2|3)\b|\(.*?\)|\[.*?\]", " ", norm(title))
    c = re.sub(r"\b(?:pvt|private|ltd|limited|inc|llp|technologies|technology|solutions|india)\b", " ",
               norm(company))
    return short_hash(" ".join(c.split()), " ".join(t.split()), city_key(location))


class DB:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else config.data_dir() / "jobs.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(self.path, timeout=30)   # a running scan writes too: wait, never fail
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        cols = {r[1] for r in self.c.execute("PRAGMA table_info(jobs)")}
        for col, typ in (("pay_estimate", "TEXT"), ("details", "TEXT DEFAULT '{}'"),     # DBs made before 1.7
                         ("est_min", "REAL"), ("est_max", "REAL"), ("est_source", "TEXT"), ("est_at", "TEXT")):   # 2.1
            if col not in cols:
                self.c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
        acols = {r[1] for r in self.c.execute("PRAGMA table_info(applications)")}
        for col in ("conf_id", "form_url"):                                                  # 2.3
            if col not in acols:
                self.c.execute(f"ALTER TABLE applications ADD COLUMN {col} TEXT DEFAULT ''")

    # ── jobs ────────────────────────────────────────────────────────────────
    def upsert(self, job: Job) -> bool:
        """Insert or merge. Returns True when the job is new to us."""
        jid = job.id
        row = self.c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        url = canonical_url(job.url)
        if row is None:
            self.c.execute(
                """INSERT INTO jobs (id, kind, title, company, location, is_remote, job_type, url, urls,
                   sources, description, posted_at, found_at, pay_min, pay_max, pay_period, currency,
                   emails, easy_apply, score, reasons, flags, dream, status, status_at, deadline, pay_estimate, details)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (jid, job.kind, job.title, job.company, job.location, int(job.is_remote), job.job_type,
                 url, json.dumps([url] if url else []), json.dumps([job.source]), job.description,
                 job.posted_at, iso(), job.pay_min, job.pay_max, job.pay_period, job.currency,
                 json.dumps(job.emails), None if job.easy_apply is None else int(job.easy_apply),
                 job.score, json.dumps(job.reasons), json.dumps(job.flags), int(job.dream),
                 job.status, iso(), job.deadline, job.pay_estimate, json.dumps(job.details or {})))
            self.c.commit()
            return True
        urls = json.loads(row["urls"] or "[]")
        if url and url not in urls:
            urls.append(url)
        sources = json.loads(row["sources"] or "[]")
        if job.source and job.source not in sources:
            sources.append(job.source)
        desc = row["description"] or ""
        if len(job.description or "") > len(desc):
            desc = job.description
        best_url = row["url"] or url
        # prefer a direct company/ATS link over aggregators for applying
        if url and _url_rank(url) > _url_rank(best_url):
            best_url = url
        emails = sorted(set(json.loads(row["emails"] or "[]")) | set(job.emails))
        self.c.execute(
            """UPDATE jobs SET urls=?, sources=?, description=?, url=?, emails=?,
               posted_at=COALESCE(posted_at, ?), job_type=COALESCE(job_type, ?),
               easy_apply=COALESCE(easy_apply, ?), deadline=COALESCE(deadline, ?)
               WHERE id=?""",
            (json.dumps(urls), json.dumps(sources), desc, best_url, json.dumps(emails),
             job.posted_at, job.job_type,
             None if job.easy_apply is None else int(job.easy_apply), job.deadline, jid))
        # pay moves as one unit (min, max, period, currency): two boards' numbers are never mixed into one
        # range, and a listed pay replaces an estimate
        had, has = row["pay_min"] or row["pay_max"], job.pay_min or job.pay_max
        if job.details:                                                        # a re-read notice: latest reading wins
            self.c.execute("UPDATE jobs SET details=?, deadline=COALESCE(?, deadline) WHERE id=?",
                           (json.dumps(job.details), job.deadline, jid))
        only_this_board = json.loads(row["sources"] or "[]") == [job.source]     # its pay came from this board
        if has and (not had or (row["pay_estimate"] and not job.pay_estimate) or (job.pay_estimate and only_this_board)):
            self.c.execute("UPDATE jobs SET pay_min=?, pay_max=?, pay_period=?, currency=?, pay_estimate=? WHERE id=?",
                           (job.pay_min, job.pay_max, job.pay_period, job.currency, job.pay_estimate, jid))
        self.c.commit()
        return False

    def find_repost(self, company: str, title: str, location: str | None, days: int = 30) -> str | None:
        """Id of a job found in the last `days` with the same company, title and city under another id
        (the same role re-listed with a different company suffix or location wording), else None."""
        from .companies import cname                    # lazy: companies imports this module
        import datetime as dt
        c, t, city = cname(company), norm(title), city_key(location)
        since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
        # ponytail: LIKE on the first word, then exact match in Python; fine for a few thousand rows a month
        for r in self.c.execute("SELECT id, company, title, location FROM jobs WHERE found_at >= ? AND id != ? "
                                "AND company LIKE ? ORDER BY found_at",
                                (since, job_id(company, title, location), f"%{c.split(' ')[0] if c else ''}%")):
            if cname(r["company"]) == c and norm(r["title"]) == t and city_key(r["location"]) == city:
                return r["id"]
        return None

    def add_url(self, jid: str, url: str) -> None:
        urls = json.loads(self.c.execute("SELECT urls FROM jobs WHERE id=?", (jid,)).fetchone()[0] or "[]")
        if (url := canonical_url(url)) and url not in urls:
            self.c.execute("UPDATE jobs SET urls=? WHERE id=?", (json.dumps(urls + [url]), jid))
            self.c.commit()

    def set_score(self, jid: str, score: int, reasons: list[str], flags: list[str], dream: bool,
                  status: str | None = None) -> None:
        self.c.execute("UPDATE jobs SET score=?, reasons=?, flags=?, dream=? WHERE id=?",
                       (score, json.dumps(reasons), json.dumps(flags), int(dream), jid))
        if status:
            self.c.execute("UPDATE jobs SET status=?, status_at=? WHERE id=? AND status IN ('new','filtered')",
                           (status, iso(), jid))
        self.c.commit()

    def set_status(self, jid: str, status: str, detail: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; use one of {', '.join(STATUSES)}")
        self.c.execute("UPDATE jobs SET status=?, status_at=? WHERE id=?", (status, iso(), jid))
        self.event(jid, status, detail)
        self.c.commit()

    def applied_on(self, jid: str) -> str | None:
        """When you applied (first 'applied' status), or None."""
        r = self.c.execute("SELECT MIN(at) FROM events WHERE job_id=? AND kind='applied'", (jid,)).fetchone()
        if r and r[0]:
            return r[0]
        r = self.c.execute("SELECT status_at FROM jobs WHERE id=? AND status='applied'", (jid,)).fetchone()
        return r[0] if r else None

    def note(self, jid: str, text: str) -> None:
        self.c.execute("UPDATE jobs SET notes = TRIM(COALESCE(notes,'') || char(10) || ?) WHERE id=?",
                       (text, jid))
        self.c.commit()

    def get(self, jid: str) -> dict | None:
        row = self.c.execute("SELECT * FROM jobs WHERE id=? OR id LIKE ?", (jid, jid + "%")).fetchone()
        return _row(row) if row else None

    def query(self, status: Iterable[str] | None = None, kind: str | None = None, min_score: int = 0,
              limit: int = 200, order: str = "score DESC, found_at DESC", since: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM jobs WHERE score >= ?", [min_score]
        if status:
            st = list(status)
            sql += f" AND status IN ({','.join('?' * len(st))})"
            args += st
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if since:
            sql += " AND found_at >= ?"
            args.append(since)
        sql += f" ORDER BY {order} LIMIT ?"
        args.append(limit)
        return [_row(r) for r in self.c.execute(sql, args).fetchall()]

    def count(self, where: str = "1=1", args: tuple = ()) -> int:
        return self.c.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", args).fetchone()[0]

    def applied_today(self, site: str | None = None) -> int:
        today = iso()[:10]
        sql = "SELECT COUNT(*) FROM events WHERE kind='applied' AND at >= ?"
        args: list = [today]
        if site:
            sql += " AND detail LIKE ?"
            args.append(f"%site={site}%")
        return self.c.execute(sql, args).fetchone()[0]

    # ── application records (Review Center) ─────────────────────────────────
    def add_application(self, job_id: str, site: str, outcome: str, auto: bool, approved: bool, dry: bool,
                        why: str, fields: list, screenshot: str, resume: str, resume_report: dict | None,
                        cover: str, page_url: str, conf_id: str = "", form_url: str = "") -> None:
        self.c.execute("""INSERT INTO applications (job_id, at, site, outcome, auto, approved, dry, why, fields,
                          screenshot, resume, resume_report, cover, page_url, conf_id, form_url)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (job_id, iso(), site, outcome, int(auto), int(approved), int(dry), why,
                        json.dumps(fields, default=str), screenshot, resume,
                        json.dumps(resume_report or {}, default=str), cover, page_url, conf_id or "", form_url or ""))
        self.c.commit()

    def app_meta(self, job_id: str) -> dict:
        """{conf_id, form_url} of your application: the newest real record that has each one ("" if none)."""
        out = {"conf_id": "", "form_url": ""}
        for r in self.c.execute("SELECT conf_id, form_url FROM applications WHERE job_id=? AND dry=0 ORDER BY n DESC",
                                (job_id,)):
            for k in out:
                out[k] = out[k] or (r[k] or "")
        return out

    def set_app_meta(self, job_id: str, site: str = "email", conf_id: str = "", form_url: str = "") -> None:
        """Fill a missing confirmation number / form link on your application record (one is added if there's none)."""
        row = self.c.execute("SELECT n, conf_id, form_url FROM applications WHERE job_id=? AND dry=0 AND outcome='applied' "
                             "ORDER BY n DESC LIMIT 1", (job_id,)).fetchone()
        if row is None:
            self.add_application(job_id, site, "applied", False, True, False, "recorded from the email", [], "", "",
                                 None, "", "", conf_id, form_url)
            return
        self.c.execute("UPDATE applications SET conf_id=COALESCE(NULLIF(conf_id,''), ?), "
                       "form_url=COALESCE(NULLIF(form_url,''), ?) WHERE n=?", (conf_id or "", form_url or "", row["n"]))
        self.c.commit()

    def applications(self, job_id: str | None = None, limit: int = 300) -> list[dict]:
        sql, args = "SELECT * FROM applications", []
        if job_id:
            sql += " WHERE job_id=?"
            args.append(job_id)
        sql += " ORDER BY n DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self.c.execute(sql, args):
            r = dict(r)
            for k in ("fields", "resume_report"):
                try:
                    r[k] = json.loads(r[k] or "null")
                except ValueError:
                    r[k] = None
            out.append(r)
        return out

    def set_app_fields(self, n: int, fields: list) -> None:
        self.c.execute("UPDATE applications SET fields=? WHERE n=?", (json.dumps(fields, default=str), n))
        self.c.commit()

    # ── events / runs ───────────────────────────────────────────────────────
    def event(self, jid: str, kind: str, detail: str = "") -> None:
        self.c.execute("INSERT INTO events VALUES (?,?,?,?)", (jid, iso(), kind, detail))
        self.c.commit()

    def events(self, jid: str) -> list[dict]:
        return [dict(r) for r in self.c.execute(
            "SELECT * FROM events WHERE job_id=? ORDER BY at", (jid,)).fetchall()]

    def log_run(self, kind: str, found: int, new: int, alerted: int, errors: list[str]) -> None:
        self.c.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                       (iso(), kind, found, new, alerted, json.dumps(errors[:50])))
        self.c.commit()

    # ── page watcher memory ─────────────────────────────────────────────────
    def link_is_new(self, page: str, href: str, text: str) -> bool:
        # OR IGNORE, never IntegrityError: a failed INSERT left its implicit transaction open, holding the
        # write lock through the PDF downloads that follow (the bot and other scans then hit "database is locked")
        new = self.c.execute("INSERT OR IGNORE INTO seen_links VALUES (?,?,?,?)", (page, href, text, iso())).rowcount == 1
        self.c.commit()
        return new

    def page_known(self, page: str) -> bool:
        return self.c.execute("SELECT 1 FROM seen_links WHERE page=? LIMIT 1", (page,)).fetchone() is not None

    # ── merge another DB (GitHub Actions state → laptop) ────────────────────
    def merge_from(self, other_path: str | Path) -> tuple[int, int]:
        """Pull rows from another jobs.db. New jobs are added; for existing ones
        the more advanced status wins (applied beats alerted, etc.)."""
        o = sqlite3.connect(other_path)
        o.row_factory = sqlite3.Row
        added = updated = 0
        cols = [r[1] for r in self.c.execute("PRAGMA table_info(jobs)")]
        for r in o.execute("SELECT * FROM jobs"):
            r = dict(r)
            mine = self.c.execute("SELECT status, score FROM jobs WHERE id=?", (r["id"],)).fetchone()
            if mine is None:
                keys = [k for k in cols if k in r]
                self.c.execute(f"INSERT INTO jobs ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                               [r[k] for k in keys])
                added += 1
            elif RANK.get(r["status"], 0) > RANK.get(mine["status"], 0):
                self.c.execute("UPDATE jobs SET status=?, status_at=? WHERE id=?",
                               (r["status"], r["status_at"], r["id"]))
                updated += 1
        for r in o.execute("SELECT * FROM seen_links"):
            self.c.execute("INSERT OR IGNORE INTO seen_links VALUES (?,?,?,?)", tuple(r))
        have = {(e["job_id"], e["at"], e["kind"]) for e in
                self.c.execute("SELECT job_id, at, kind FROM events")}
        for e in o.execute("SELECT * FROM events"):
            if (e["job_id"], e["at"], e["kind"]) not in have:
                self.c.execute("INSERT INTO events VALUES (?,?,?,?)", tuple(e))
        self.c.commit()
        o.close()
        return added, updated


def _url_rank(url: str | None) -> int:
    u = (url or "").lower()
    if any(h in u for h in ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "careers.", "/careers")):
        return 3
    if "linkedin.com" in u:
        return 2
    if "naukri.com" in u:
        return 2
    return 1


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("urls", "sources", "emails", "reasons", "flags"):
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except (TypeError, ValueError):
            d[k] = []
    try:
        d["details"] = json.loads(d.get("details") or "{}")
    except (TypeError, ValueError):
        d["details"] = {}
    d["dream"] = bool(d.get("dream"))
    d["is_remote"] = bool(d.get("is_remote"))
    return d


def job_from_row(d: dict) -> Job:
    keys = {f for f in Job.__dataclass_fields__}
    j = Job(**{k: v for k, v in d.items() if k in keys and k not in ("source",)})
    j.source = (d.get("sources") or [""])[0]
    return j


__all__ = ["DB", "Job", "job_id", "city_key", "STATUSES", "job_from_row", "asdict"]
