"""Questions: anything a form needs that isn't in your files is ASKED, never guessed.

When the bot meets a required field it can't answer from config/private.yaml,
config/profile.yaml or data/answers.yaml, the field becomes a question in
data/questions.yaml (with the form's own dropdown options, if any). You answer it
once: in the Review Center (Questions tab), on Telegram (/questions, /answer 2 Yes)
or in the terminal (`hunt questions`). The answer is saved as a learned fact, and every
job that was waiting only on answered questions goes back into the apply queue.

Never asked (you handle these in the browser yourself): captcha/OTP, cover-letter
essays, and voluntary diversity questions (caste, religion, disability…).
"""
from __future__ import annotations

import yaml

from .. import config
from ..db import DB
from ..util import iso, norm
from . import fields as F

NEVER_ASK = {"captcha", "otp", "cover_letter", "resume", "cover_letter_file", "diversity", "marketing", "consent"}


def _path():
    return config.data_dir() / "questions.yaml"


def load() -> dict:
    f = _path()
    if not f.is_file():
        return {}
    return yaml.safe_load(f.read_text(encoding="utf-8")) or {}


def _save(q: dict) -> None:
    _path().write_text(yaml.safe_dump(q, allow_unicode=True, sort_keys=False), encoding="utf-8")


def add_from_record(job_id: str, record: list[dict], site: str = "") -> int:
    """Turn the unanswered required fields of one form into questions. Returns how many are new."""
    q = load()
    new = 0
    for f in record:
        if not f.get("required") or f.get("basic") or f.get("key") in NEVER_ASK:
            continue
        if f.get("value") not in (None, "", False) and f.get("basis") == "drafted":
            continue
        label = (f.get("label") or "").strip().rstrip("*").strip()
        if len(label) < 2:
            continue
        a = F.answer(label, f.get("type") or "text")
        if a.basic:                          # answered meanwhile
            continue
        k = norm(label)
        item = q.get(k) or {"label": label, "type": f.get("type") or "text", "options": [], "jobs": [], "sites": [],
                            "first_asked": iso(), "why": f.get("note") or ""}
        if not q.get(k):
            new += 1
        if f.get("options"):
            item["options"] = sorted(set(item["options"]) | set(f["options"]))
        if job_id not in item["jobs"]:
            item["jobs"].append(job_id)
        if site and site not in item["sites"]:
            item["sites"].append(site)
        q[k] = item
    _save(q)
    return new


def pending() -> list[dict]:
    """Open questions, the ones blocking the most jobs first."""
    q = load()
    rows = [dict(v, key=k) for k, v in q.items()]
    rows.sort(key=lambda r: (-len(r.get("jobs", [])), r.get("first_asked", "")))
    return rows


def answer(key_or_index: str | int, value: str) -> dict:
    """Save your answer, drop the question, re-queue jobs that no longer need you."""
    rows = pending()
    item = None
    if isinstance(key_or_index, int) or str(key_or_index).isdigit():
        i = int(key_or_index) - 1
        item = rows[i] if 0 <= i < len(rows) else None
    else:
        item = next((r for r in rows if r["key"] == norm(str(key_or_index))), None)
    if item is None:
        raise KeyError(f"no open question {key_or_index!r}")
    value = (value or "").strip()
    if not value:
        raise ValueError("empty answer")
    if item.get("options"):
        choice = F.pick_option(value, item["options"])
        if not choice:
            raise ValueError(f"pick one of: {', '.join(item['options'][:12])}")
        value = choice
    F.learn(item["label"], value, ",".join(item.get("sites", [])))
    q = load()
    q.pop(item["key"], None)
    _save(q)
    requeued = requeue(item.get("jobs", []))
    return {"label": item["label"], "value": value, "requeued": requeued}


def requeue(job_ids: list[str]) -> list[str]:
    """Jobs waiting on you go back to the apply queue once every required field they lacked
    can now be answered from your facts (they then auto-submit on the next `hunt apply`)."""
    db = DB()
    open_keys = set(load())
    out = []
    for jid in job_ids:
        j = db.get(jid)
        if not j or j["status"] != "needs_you":
            continue
        apps = db.applications(jid, limit=1)
        if not apps:
            continue
        blocked = False
        for f in apps[0].get("fields") or []:
            if not f.get("required") or f.get("basic") or f.get("key") in NEVER_ASK - {"cover_letter"}:
                continue
            if f.get("key") == "cover_letter" or norm(f.get("label") or "") in open_keys:
                blocked = True
                break
            if not F.answer(f.get("label") or "", f.get("type") or "text").basic:
                blocked = True
                break
        if not blocked:
            db.set_status(jid, "queued", "all questions answered")
            out.append(jid)
    return out
