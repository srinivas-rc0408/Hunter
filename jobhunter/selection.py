"""Getting selected more: resume versions and their response rates, /check talking points, "why no replies?".

Versions: every new resume PDF (resumes.use) becomes v1, v2 … in data/resume-versions.json. The resume only changes
through resumes.use, so the version an application sent is the one active when it was sent.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from . import config
from .util import IST

MIN_PER_VERSION = 15          # fewer applications than this per version: "too few to tell"
MIN_SILENT = 20               # this many applications with 0 answers → the weekly "why no replies?" check


def _file():
    return config.data_dir() / "resume-versions.json"


def versions() -> list[dict]:
    try:
        vs = json.loads(_file().read_text())
    except (OSError, ValueError):
        vs = []
    pdf = config.path("config", "resume.pdf")
    if not vs and pdf.is_file():               # the resume you had before versions existed covers every past application
        vs = [{"v": 1, "sha": hashlib.sha256(pdf.read_bytes()).hexdigest(), "at": ""}]
        _file().write_text(json.dumps(vs, indent=1))
    return vs


def register(data: bytes, now: dt.datetime | None = None) -> str:
    """'v3' for this PDF: a new label when it's new, its old label when you send a previous version again."""
    sha = hashlib.sha256(data).hexdigest()
    vs = versions()
    old = next((x for x in vs if x["sha"] == sha), None)
    if old and old is vs[-1]:
        return f"v{old['v']}"
    vs.append({"v": old["v"] if old else max([x["v"] for x in vs] or [0]) + 1, "sha": sha,
               "at": (now or dt.datetime.now(IST)).replace(tzinfo=None).isoformat(timespec="seconds")})
    _file().write_text(json.dumps(vs, indent=1))
    return f"v{vs[-1]['v']}"


def version_at(when: dt.datetime | None, vs: list[dict] | None = None) -> str | None:
    vs = versions() if vs is None else vs
    if not vs:
        return None
    t = when.isoformat() if when else ""
    return f"v{[x for x in vs if x['at'] <= t][-1]['v']}" if any(x["at"] <= t for x in vs) else f"v{vs[0]['v']}"


def by_version(R: list[dict]) -> list[tuple[str, int, int, str]]:
    """[(version, applied, answered, '12%' | 'too few to tell')] from tracker.rows()."""
    vs = versions()
    tally: dict[str, list[int]] = {}
    for r in R:
        v = version_at(r["applied"], vs) or "v?"
        t = tally.setdefault(v, [0, 0])
        t[0] += 1
        t[1] += bool(r["answered"])
    return [(v, n, a, f"{round(100 * a / n)}%" if n >= MIN_PER_VERSION else f"too few to tell ({n} of {MIN_PER_VERSION})")
            for v, (n, a) in sorted(tally.items(), key=lambda x: int(x[0][1:]) if x[0][1:].isdigit() else 0)]


def version_lines(R: list[dict]) -> str:
    rows = by_version(R)
    return "" if not rows else "Response rate by resume version: " + " · ".join(f"{v} {txt} ({n} sent)" for v, n, _, txt in rows)


# ── /check: 3 talking points from resume.yaml only ─────────────────────────
def _has(text: str, term: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}s?(?![a-z0-9])", text))


def talking_points(job: dict, n: int = 3) -> list[str]:
    """Your projects/experience whose stack or tags appear in this JD, most overlap first. Nothing else is used."""
    text = ((job.get("title") or "") + " " + (job.get("description") or "")).lower()
    r = config.cfg("resume")
    items = [(p.get("name") or "", p) for p in r.get("projects") or []]
    items += [(f"{e.get('title') or ''} at {e.get('company') or ''}".strip(" at"), e) for e in r.get("experience") or []]
    scored = []
    for name, it in items:
        hits = [s for s in dict.fromkeys(list(it.get("stack") or []) + list(it.get("tags") or [])) if _has(text, str(s))]
        if hits and name:
            scored.append((len(hits), name, hits))
    scored.sort(key=lambda x: -x[0])
    return [f"{name}: uses {', '.join(map(str, hits[:5]))}, which this posting asks for" for _, name, hits in scored[:n]]


# ── weekly: why no replies? ────────────────────────────────────────────────
def why_no_replies(db, R: list[dict] | None = None) -> str | None:
    """With MIN_SILENT+ applications and not one real answer: the likely causes from our data, and one fix."""
    from . import intel
    from .score import years_required
    from .tracker import rows
    R = rows(db) if R is None else R
    if len(R) < MIN_SILENT or any(r["answered"] for r in R):
        return None
    jobs = [j for j in (db.get(r["full_id"]) for r in R) if j]
    senior = sum(1 for j in jobs if (years_required(j.get("description") or "") or 0) >= 2
                 or re.search(r"\b(?:senior|sr\.?|lead|principal|staff|manager)\b", j["title"].lower()))
    late = 0
    for r, j in zip(R, jobs):
        try:
            late += bool(r["applied"] and j.get("posted_at")
                         and (r["applied"].date() - dt.date.fromisoformat(j["posted_at"][:10])).days > 5)
        except ValueError:
            pass
    a = intel.analyse(jobs, intel._skills())
    causes = []
    if senior / len(jobs) >= 0.3:
        causes.append((senior / len(jobs), f"{senior} of {len(jobs)} were senior or asked 2+ years",
                       "apply only to roles asking 0-1 year: /top already ranks those first"))
    if late / len(jobs) >= 0.3:
        causes.append((late / len(jobs), f"{late} of {len(jobs)} were sent more than 5 days after posting",
                       "apply within 2 days: act on the 🚨 APPLY NOW alerts the same day"))
    if a["gaps"]:
        causes.append((0.5, "ATS keyword gaps: " + ", ".join(a["gaps"]) + " appear in 30%+ of these postings but not on your resume",
                       f"if you really have them, add {', '.join(a['gaps'][:2])} to your resume (only what's true)"))
    if not causes:
        causes.append((0, "no single cause stands out in our data", "ask for referrals: /referral_<id> on your top matches"))
    causes.sort(key=lambda c: -c[0])
    return (f"<b>🤔 {len(R)} applications, 0 replies: likely causes</b>\n" + "\n".join(f"• {c[1]}" for c in causes)
            + f"\n<b>Try one thing:</b> {causes[0][2]}")
