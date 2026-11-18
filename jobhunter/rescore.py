"""Re-score open jobs with the current rules (after an update, a profile change or a new dream company)."""
from __future__ import annotations

import json

from .db import DB, Job
from .score import score


def rescore(db: DB | None = None) -> dict:
    db = db or DB()
    fields = set(Job.__dataclass_fields__)
    dropped, changed, total = [], 0, 0
    for r in db.query(status=["new", "alerted", "queued", "filtered"], limit=100000):
        total += 1
        j = Job(**{k: r[k] for k in fields if k in r and k not in ("score", "reasons", "flags", "dream", "status")})
        old_score, old_status = r["score"], r["status"]
        j = score(j)
        if j.status == "filtered" and old_status != "filtered":
            db.c.execute("UPDATE jobs SET status='filtered', score=0, reasons=? WHERE id=?", (json.dumps(j.reasons), r["id"]))
            dropped.append((r["title"], r["company"], j.reasons[0]))
        else:
            db.set_score(r["id"], j.score, j.reasons, j.flags, j.dream)
            if old_status == "filtered" and j.status != "filtered":
                db.c.execute("UPDATE jobs SET status='new' WHERE id=?", (r["id"],))
        changed += j.score != old_score
    db.c.commit()
    return {"total": total, "changed": changed, "dropped": dropped}
