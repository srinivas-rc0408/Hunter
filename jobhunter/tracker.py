"""The Excel tracker: every application, every email reply, and a summary, in one .xlsx.

Rewritten automatically after every application, status change and email sync, so it's always
current. Default file: ~/Documents/JobHunter/Applications.xlsx (config/search.yaml → tracker.xlsx).
Open it in LibreOffice/Excel any time; Telegram /sheet sends you the latest copy.

Sheets:
  Applications  one row per job you applied to: date and time applied (IST), company, role, location,
                work mode, pay, where you applied, status, last email (date + kind), email count,
                days since applied, next step, job link, resume sent, fields you fixed, job id.
  Email replies every application email: date, kind, from, subject, company/role it belongs to.
  Summary       totals by status, companies applied to, per week, response rate, interview rate.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from . import config
from .db import DB
from .util import parse_iso

DONE = ("applied", "interview", "offer", "rejected")
COLOR = {"applied": "DDEBF7", "interview": "FFF2CC", "offer": "C6EFCE", "rejected": "F8CBAD"}
KIND_COLOR = {"offer": "C6EFCE", "interview": "FFF2CC", "assessment": "FCE4D6", "rejected": "F8CBAD"}
NEXT = {"applied": "wait; follow-up suggested after 7 days", "interview": "prepare: /interview_<id>",
        "offer": "read the offer, reply in Gmail", "rejected": "—"}


def path() -> Path:
    t = (config.cfg("search").get("tracker") or {}).get("xlsx") or "~/Documents/JobHunter/Applications.xlsx"
    return Path(os.path.expanduser(t))


def _dt(s: str | None) -> dt.datetime | None:
    d = parse_iso(s) if s else None
    return d.replace(tzinfo=None) if d else None


def rows(db: DB) -> list[dict]:
    """The same list the dashboard and Telegram /applied show."""
    from .notify import mode_text, pay_text
    from .mail import ensure
    ensure(db)
    out = []
    for j in db.query(status=list(DONE), limit=100000, order="status_at DESC"):
        applied = db.applied_on(j["id"]) or j.get("status_at")
        apps = [a for a in db.applications(j["id"], limit=20) if not a.get("dry")]
        sent = next((a for a in apps if a.get("outcome") == "applied"), apps[0] if apps else {})
        mails = [dict(r) for r in db.c.execute("SELECT at, kind, subject FROM mail WHERE job_id=? ORDER BY at DESC", (j["id"],))]
        fixed = sum(1 for f in (sent.get("fields") or []) if f.get("basis") == "you")
        a = _dt(applied)
        out.append({
            "applied": a, "company": j["company"], "role": j["title"], "location": j.get("location") or "",
            "mode": mode_text(j) or "not listed", "pay": pay_text(j), "via": sent.get("site") or ", ".join(j.get("sources") or []),
            "status": j["status"], "status_at": _dt(j.get("status_at")),
            "last_mail": f"{mails[0]['at'][:10]} {mails[0]['kind']}" if mails else "",
            "mails": len(mails), "days": (dt.datetime.now() - a).days if a else None,
            "next": NEXT.get(j["status"], "").replace("<id>", j["id"][:8]),
            "url": j.get("url") or "", "resume": Path(sent.get("resume") or "").name, "fixed": fixed,
            "dream": "★" if j.get("dream") else "", "id": j["id"][:8]})
    return out


def write(db: DB | None = None, out: Path | None = None) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    db = db or DB()
    out = out or path()
    out.parent.mkdir(parents=True, exist_ok=True)
    R = rows(db)
    wb = Workbook()
    bold, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E78")

    def sheet(ws, headers, widths):
        ws.append(headers)
        for i, (h, w) in enumerate(zip(headers, widths), 1):
            c = ws.cell(row=1, column=i)
            c.font, c.fill, c.alignment = bold, head_fill, Alignment(vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        ws.row_dimensions[1].height = 30

    ws = wb.active
    ws.title = "Applications"
    H = ["Applied on", "Time (IST)", "★", "Company", "Role", "Location", "Work mode", "Pay", "Applied via", "Status",
         "Status since", "Last email", "Emails", "Days since applied", "Next step", "Job link", "Resume sent",
         "Fields you fixed", "Job id"]
    sheet(ws, H, [12, 10, 4, 26, 34, 24, 11, 18, 13, 11, 12, 20, 8, 10, 32, 14, 24, 9, 10])
    for r in R:
        a = r["applied"]
        ws.append([a.date() if a else None, a.strftime("%H:%M") if a else "", r["dream"], r["company"], r["role"],
                   r["location"], r["mode"], r["pay"], r["via"], r["status"],
                   r["status_at"].date() if r["status_at"] else None, r["last_mail"], r["mails"], r["days"], r["next"],
                   "open" if r["url"] else "", r["resume"], r["fixed"], r["id"]])
        n = ws.max_row
        ws.cell(row=n, column=1).number_format = ws.cell(row=n, column=11).number_format = "DD-MMM-YYYY"
        if r["url"]:
            ws.cell(row=n, column=16).hyperlink = r["url"]
            ws.cell(row=n, column=16).font = Font(color="0563C1", underline="single")
        if r["status"] in COLOR:
            ws.cell(row=n, column=10).fill = PatternFill("solid", fgColor=COLOR[r["status"]])
    if R:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(H))}{ws.max_row}"

    from .mail import replies
    ws2 = wb.create_sheet("Email replies")
    sheet(ws2, ["Date", "Time (IST)", "Kind", "Company", "Role", "From", "Subject", "Matched by", "Job id"],
          [12, 10, 12, 26, 32, 30, 60, 30, 10])
    for m in replies(db, limit=100000):
        d = _dt(m["at"])
        j = db.get(m["job_id"]) if m.get("job_id") else None
        ws2.append([d.date() if d else None, d.strftime("%H:%M") if d else "", m["kind"], j["company"] if j else "",
                    j["title"] if j else "", f"{m['sender_name']} <{m['sender']}>" if m["sender_name"] else m["sender"],
                    m["subject"], m["match"] or "not matched to an application", (m["job_id"] or "")[:8]])
        ws2.cell(row=ws2.max_row, column=1).number_format = "DD-MMM-YYYY"
        if m["kind"] in KIND_COLOR:
            ws2.cell(row=ws2.max_row, column=3).fill = PatternFill("solid", fgColor=KIND_COLOR[m["kind"]])
    if ws2.max_row > 1:
        ws2.auto_filter.ref = f"A1:I{ws2.max_row}"

    ws3 = wb.create_sheet("Summary")
    ws3.column_dimensions["A"].width, ws3.column_dimensions["B"].width = 34, 14
    total = len(R)
    by = {s: sum(1 for r in R if r["status"] == s) for s in DONE}
    replied = sum(1 for r in R if r["mails"] or r["status"] in ("interview", "offer", "rejected"))
    week_ago = dt.datetime.now() - dt.timedelta(days=7)
    lines = [("Updated", dt.datetime.now().strftime("%d %b %Y, %H:%M IST")),
             ("Applications (total)", total), ("Companies applied to", len({r["company"].lower() for r in R})),
             ("Applied in the last 7 days", sum(1 for r in R if r["applied"] and r["applied"] >= week_ago)),
             ("Waiting (applied)", by["applied"]), ("Interviews", by["interview"]), ("Offers", by["offer"]),
             ("Rejected", by["rejected"]),
             ("Response rate", f"{round(100 * replied / total)}%" if total else "—"),
             ("Interview rate", f"{round(100 * (by['interview'] + by['offer']) / total)}%" if total else "—")]
    for k, v in lines:
        ws3.append([k, v])
        ws3.cell(row=ws3.max_row, column=1).font = Font(bold=True)
    ws3.append([])
    ws3.append(["Week starting", "Applications"])
    weeks: dict[str, int] = {}
    for r in R:
        if r["applied"]:
            wk = (r["applied"] - dt.timedelta(days=r["applied"].weekday())).strftime("%d %b %Y")
            weeks[wk] = weeks.get(wk, 0) + 1
    for wk, n in sorted(weeks.items(), key=lambda x: dt.datetime.strptime(x[0], "%d %b %Y"), reverse=True):
        ws3.append([wk, n])
    wb.move_sheet("Summary", offset=-2)
    wb.active = 0
    tmp = out.with_name(f".{out.name}.tmp")
    wb.save(tmp)
    os.replace(tmp, out)                          # never a half-written file, even if it's open
    return out


def refresh(db: DB | None = None) -> Path | None:
    """Rewrite the tracker; never let a tracker problem break an apply or a sync."""
    try:
        return write(db)
    except Exception as exc:
        print(f"tracker not updated: {type(exc).__name__}: {exc}")
        return None
