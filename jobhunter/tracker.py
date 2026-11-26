"""The Excel tracker: every application, every email reply, and a summary, in one .xlsx.

Rewritten automatically after every application, status change and email sync, so it's always
current. Default file: ~/Documents/JobHunter/Applications.xlsx (config/search.yaml → tracker.xlsx).
Open it in LibreOffice/Excel any time; Telegram /sheet sends you the latest copy.

Sheets:
  Applications  an Excel Table, one row per application: the columns in H, then your own columns, Notes, Job id. Your columns, values and hidden rows live in the DB (sheet_columns,
                sheet_custom, sheet_hidden) and are re-applied on every rewrite; if you edit the file itself,
                Notes and your columns are read back before the next rewrite (nothing else is).
  Email replies every application email: date, kind, from, subject, company/role it belongs to.
  Referrals     every referral you asked for (`ref <id> <name>` / `ref done <id>` in Telegram), applied yet or not.
  Dashboard     (first) title, KPI tiles, applications per week + by-status charts, next actions.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from . import config
from .db import DB
from .util import IST, parse_iso

DONE = ("applied", "interview", "offer", "rejected")
NEXT = {"applied": "wait; follow-up suggested after 7 days", "interview": "prepare: /interview_<id>",
        "offer": "read the offer, reply in Gmail", "rejected": "—"}


# ── your own edits: custom columns, values, hidden rows (kept in the DB, re-applied on every rewrite) ──
def ensure(db: DB) -> None:
    db.c.executescript("""
    CREATE TABLE IF NOT EXISTS sheet_columns (name TEXT PRIMARY KEY, position INTEGER);
    CREATE TABLE IF NOT EXISTS sheet_custom (job_id TEXT, column TEXT, value TEXT, PRIMARY KEY (job_id, column));
    CREATE TABLE IF NOT EXISTS sheet_hidden (job_id TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS sheet_snapshot (job_id TEXT, column TEXT, value TEXT, PRIMARY KEY (job_id, column));
    CREATE TABLE IF NOT EXISTS referrals (job_id TEXT PRIMARY KEY, name TEXT, status TEXT, at TEXT);
    """)


# ── referrals: the name is only ever what you typed; nothing is looked up ──
def refer(db: DB, jid: str, name: str) -> str:
    """You asked `name` for a referral for this job (status 'asked'). Returns the cleaned name."""
    name = " ".join((name or "").split())[:60]
    if not name or "://" in name or name.lower().startswith("www."):
        raise ValueError("the referral needs a person's name, e.g. ref ab12cd34 Priya Sharma")
    ensure(db)
    db.c.execute("INSERT OR REPLACE INTO referrals VALUES (?,?,?,?)", (jid, name, "asked", dt.datetime.now(IST).isoformat()))
    db.c.commit()
    return name


def refer_done(db: DB, jid: str) -> dict:
    """They referred you: status 'referred', dated now."""
    r = referral(db, jid)
    if not r:
        raise ValueError("no referral asked for this job yet: ref <id> <name> first")
    db.c.execute("UPDATE referrals SET status='referred', at=? WHERE job_id=?", (dt.datetime.now(IST).isoformat(), jid))
    db.c.commit()
    return referral(db, jid)


def referral(db: DB, jid: str) -> dict | None:
    ensure(db)
    r = db.c.execute("SELECT job_id, name, status, at FROM referrals WHERE job_id=?", (jid,)).fetchone()
    return dict(zip(("job_id", "name", "status", "at"), r)) if r else None


def referrals(db: DB) -> list[dict]:
    """Newest first, each with its job (None if the job was deleted)."""
    ensure(db)
    out = []
    for jid, name, status, at in db.c.execute("SELECT job_id, name, status, at FROM referrals ORDER BY at DESC").fetchall():
        out.append({"job_id": jid, "name": name, "status": status, "at": at, "job": db.get(jid)})
    return out


def columns(db: DB) -> list[str]:
    ensure(db)
    return [r[0] for r in db.c.execute("SELECT name FROM sheet_columns ORDER BY position")]


def add_column(db: DB, name: str) -> str:
    name = " ".join((name or "").split())[:40]
    if not name:
        raise ValueError("the column needs a name")
    if name.lower() in {h.lower() for h in H + TAIL} or name.lower() in {c.lower() for c in columns(db)}:
        raise ValueError(f"the sheet already has a column “{name}”")
    pos = (db.c.execute("SELECT MAX(position) FROM sheet_columns").fetchone()[0] or 0) + 1
    db.c.execute("INSERT INTO sheet_columns VALUES (?,?)", (name, pos))
    db.c.commit()
    return name


def column_name(db: DB, name: str) -> str | None:
    """The real column for what you typed ('referral' → 'Referral'); Notes counts. None if there's no such column."""
    if (name or "").strip().lower() in ("notes", "note"):
        return "Notes"
    return next((c for c in columns(db) if c.lower() == (name or "").strip().lower()), None)


def set_value(db: DB, jid: str, column: str, value: str) -> None:
    col = column_name(db, column)
    if not col:
        raise ValueError(f"no column “{column}” in the sheet: add it first")
    if col == "Notes":
        db.c.execute("UPDATE jobs SET notes=? WHERE id=?", (value, jid))
    elif value:
        db.c.execute("INSERT OR REPLACE INTO sheet_custom VALUES (?,?,?)", (jid, col, value))
    else:
        db.c.execute("DELETE FROM sheet_custom WHERE job_id=? AND column=?", (jid, col))
    db.c.commit()


def hide(db: DB, jid: str) -> None:
    """Off the sheet only: the job and its application record stay."""
    ensure(db)
    db.c.execute("INSERT OR IGNORE INTO sheet_hidden VALUES (?)", (jid,))
    db.c.commit()


def show_hidden(db: DB) -> int:
    ensure(db)
    n = db.c.execute("DELETE FROM sheet_hidden").rowcount
    db.c.commit()
    return n


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def read_back(db: DB, f: Path) -> int:
    """Your edits in the .xlsx itself: ONLY Notes and the custom columns are read back (a new header you typed after
    the standard ones becomes a custom column). A cell counts as edited when it differs from what was last written;
    every other column always comes from the DB. Returns how many cells were taken."""
    if not f.is_file():
        return 0
    from openpyxl import load_workbook
    ws = load_workbook(f, read_only=True, data_only=True)["Applications"]
    rows = ws.iter_rows(values_only=True)
    head = [_cell(h) for h in next(rows, [])]
    if "Job id" not in head:
        return 0
    idx = head.index("Job id")
    mine = [(i, h) for i, h in enumerate(head) if h and (h == "Notes" or h not in H + TAIL + OLD)]
    for _, h in mine:
        if h != "Notes" and not column_name(db, h):
            add_column(db, h)
    snap = {(r[0], r[1]): r[2] for r in db.c.execute("SELECT job_id, column, value FROM sheet_snapshot")}
    took = 0
    for row in rows:
        short = _cell(row[idx]) if idx < len(row) else ""
        j = db.get(short) if len(short) >= 6 else None
        if not j:
            continue
        for i, h in mine:
            col = column_name(db, h)
            new = _cell(row[i]) if i < len(row) else ""
            if new != snap.get((j["id"], col), ""):
                set_value(db, j["id"], col, new)
                took += 1
    return took


# Applications sheet: these, then your own columns (Referral, …), then Notes and Job id. Notes + yours are read back.
H = ["Applied on", "Time (IST)", "★", "Company", "Role", "Location", "Work mode", "Pay", "Score", "Applied via",
     "Status", "Status since", "Application ID", "Job link", "Application form", "Last email", "Emails",
     "Days waiting", "Next step", "Referral name", "Referral status", "Referral date", "Resume sent", "Fields you fixed"]
TAIL = ["Notes", "Job id"]
OLD = ["Days since applied"]          # standard in sheets written before 2.3: never mistaken for one of your columns
WIDTH = {"Applied on": 12, "Time (IST)": 9, "★": 4, "Company": 24, "Role": 32, "Location": 22, "Work mode": 11,
         "Pay": 22, "Score": 7, "Applied via": 12, "Status": 12, "Status since": 12, "Application ID": 16,
         "Job link": 10, "Application form": 12, "Last email": 18, "Emails": 8, "Days waiting": 9, "Next step": 30,
         "Referral name": 20, "Referral status": 10, "Referral date": 12, "Resume sent": 22, "Fields you fixed": 9, "Notes": 36, "Job id": 10}
RESPONSE = ("viewed", "assessment", "interview", "rejected", "offer")    # real answers; "received" is automatic
STATUS_ORDER = ["applied", "interview", "offer", "rejected"]
NAVY, INK, MUTED = "1F3A5F", "1F2937", "6B7280"
TILE = {"Applied": "1F3A5F", "This week": "2E6DA4", "Waiting": "7A8796", "Interviews": "B7791F", "Offers": "2F855A",
        "Rejected": "B83232", "Response rate": "5B4B8A", "Interview rate": "2C7A7B"}


def path() -> Path:
    t = (config.cfg("search").get("tracker") or {}).get("xlsx") or "~/Documents/JobHunter/Applications.xlsx"
    return Path(os.path.expanduser(t))


def _dt(s: str | None) -> dt.datetime | None:
    """Naive IST datetime (Excel has no time zones), whatever zone the DB stored it in."""
    d = parse_iso(s) if s else None
    if d is None:
        return None
    return (d.astimezone(IST) if d.tzinfo else d).replace(tzinfo=None)


def rows(db: DB) -> list[dict]:
    """Every application, newest first: the sheet, the Dashboard and the numbers all read this."""
    from .notify import mode_text, pay_line
    from .mail import ensure
    ensure(db)
    out = []
    now = dt.datetime.now(IST).replace(tzinfo=None)
    refs = {r["job_id"]: r for r in referrals(db)}
    for j in db.query(status=list(DONE), limit=100000, order="status_at DESC"):
        applied = db.applied_on(j["id"]) or j.get("status_at")
        apps = [a for a in db.applications(j["id"], limit=20) if not a.get("dry")]
        sent = next((a for a in apps if a.get("outcome") == "applied"), apps[0] if apps else {})
        meta = db.app_meta(j["id"])
        mails = [dict(r) for r in db.c.execute("SELECT at, kind, subject FROM mail WHERE job_id=? ORDER BY at DESC", (j["id"],))]
        answered = j["status"] in ("interview", "offer", "rejected") or any(m["kind"] in RESPONSE for m in mails)
        fixed = sum(1 for f in (sent.get("fields") or []) if f.get("basis") == "you")
        a = _dt(applied)
        pay = pay_line(j).replace("not listed · ", "")
        ref = refs.get(j["id"]) or {}
        out.append({
            "applied": a, "company": j["company"], "role": j["title"], "location": j.get("location") or "",
            "mode": mode_text(j) or "not listed", "pay": pay, "score": j.get("score") or 0,
            "via": sent.get("site") or ", ".join(j.get("sources") or []),
            "status": j["status"], "status_at": _dt(j.get("status_at")), "conf_id": meta["conf_id"],
            "form_url": meta["form_url"] or sent.get("page_url") or "",
            "last_mail": f"{_dt(mails[0]['at']):%d %b} · {mails[0]['kind']}" if mails else "",
            "last_kind": mails[0]["kind"] if mails else "", "mails": len(mails), "answered": answered,
            "days": (now - a).days if a else None,
            "waiting": (now - a).days if a and j["status"] == "applied" and not answered else None,
            "next": NEXT.get(j["status"], "").replace("<id>", j["id"][:8]),
            "url": j.get("url") or "", "ref_name": ref.get("name") or "",
            "ref_status": ref.get("status") or "", "ref_at": _dt(ref.get("at")), "resume": Path(sent.get("resume") or "").name, "fixed": fixed,
            "dream": "★" if j.get("dream") else "", "id": j["id"][:8], "full_id": j["id"], "notes": (j.get("notes") or "").strip()})
    return out


def numbers(db: DB, R: list[dict] | None = None) -> dict:
    """The tiles, /stats and `hunt mail` share these. Response rate counts only real answers (viewed, test,
    interview, rejection, offer), never an automatic "we received it". Interview rate = (interview + offer) / applied."""
    R = rows(db) if R is None else R
    total = len(R)
    by = {s: sum(1 for r in R if r["status"] == s) for s in DONE}
    week = dt.datetime.now(IST).replace(tzinfo=None) - dt.timedelta(days=7)
    answered = sum(1 for r in R if r["answered"])
    pct = lambda n: f"{round(100 * n / total)}%" if total else "—"          # noqa: E731
    return {"Applied": total, "This week": sum(1 for r in R if r["applied"] and r["applied"] >= week),
            "Waiting": sum(1 for r in R if r["status"] == "applied" and not r["answered"]),
            "Interviews": by["interview"], "Offers": by["offer"], "Rejected": by["rejected"],
            "Response rate": pct(answered), "Interview rate": pct(by["interview"] + by["offer"]),
            "answered": answered, "companies": len({r["company"].lower() for r in R})}


def actions(db: DB, R: list[dict]) -> list[tuple]:
    """Next actions for the Dashboard: interviews, tests, follow-ups due. (when, what, company, role, job id)."""
    out = []
    tests = {r[0] for r in db.c.execute("SELECT job_id FROM mail WHERE kind='assessment' AND job_id IS NOT NULL")}
    for r in R:
        if r["status"] == "interview":
            out.append((r["status_at"], "Interview: prepare (/interview_" + r["id"] + ")", r["company"], r["role"], r["id"]))
        elif r["full_id"] in tests and r["status"] == "applied":
            out.append((r["status_at"], "Online test: check the email and take it", r["company"], r["role"], r["id"]))
        elif r["waiting"] is not None and r["waiting"] >= 7:
            out.append((r["applied"] + dt.timedelta(days=7), f"Follow up: no reply in {r['waiting']} days",
                        r["company"], r["role"], r["id"]))
    return sorted(out, key=lambda x: x[0] or dt.datetime.max)[:15]


def write(db: DB | None = None, out: Path | None = None) -> Path:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, DoughnutChart, Reference
    from openpyxl.chart.label import DataLabelList
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter as L
    from openpyxl.worksheet.table import Table, TableStyleInfo
    db = db or DB()
    out = out or path()
    out.parent.mkdir(parents=True, exist_ok=True)
    ensure(db)
    try:
        read_back(db, out)                        # your edits in the file first, so the rewrite keeps them
    except Exception as exc:                      # an unreadable file never blocks the rewrite
        print(f"tracker: couldn't read your edits back ({type(exc).__name__}: {exc})")
    hidden = {r[0] for r in db.c.execute("SELECT job_id FROM sheet_hidden")}
    R = [r for r in rows(db) if r["full_id"] not in hidden]
    custom = columns(db)
    vals = {(r[0], r[1]): r[2] for r in db.c.execute("SELECT job_id, column, value FROM sheet_custom")}
    N = numbers(db, R)
    now = dt.datetime.now(IST)
    fill = lambda c: PatternFill("solid", fgColor=c)                       # noqa: E731
    thin = Side(style="thin", color="D1D5DB")
    wrap = Alignment(vertical="top", wrap_text=True)
    link_font = Font(color="0563C1", underline="single")

    def table_sheet(ws, name, headers, widths):
        """Header row, widths, frozen header, print setup. The Excel Table itself is added once rows are in."""
        ws.append(headers)
        for i, (h, w) in enumerate(zip(headers, widths), 1):
            ws.column_dimensions[L(i)].width = w
            ws.cell(row=1, column=i).alignment = Alignment(vertical="center", wrap_text=True)
        ws.row_dimensions[1].height = 32
        ws.freeze_panes = "A2"
        ws.sheet_view.zoomScale = 90
        _print(ws, "1:1")

    def add_table(ws, name, ncols):
        if ws.max_row < 2:                         # Excel rejects an empty Table: header only, styled by hand
            for c in ws[1]:
                c.font, c.fill = Font(bold=True, color="FFFFFF"), fill(NAVY)
            return
        t = Table(displayName=name, ref=f"A1:{L(ncols)}{ws.max_row}")
        t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True, showColumnStripes=False)
        ws.add_table(t)

    def _print(ws, title_rows=None):
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_options.horizontalCentered = True
        ws.page_margins.left = ws.page_margins.right = 0.4
        if title_rows:
            ws.print_title_rows = title_rows

    wb = Workbook()
    # ── Dashboard ────────────────────────────────────────────────────────────
    ds = wb.active
    ds.title = "Dashboard"
    ds.sheet_view.showGridLines = False
    ds.sheet_view.zoomScale = 90
    ds.column_dimensions["A"].width = 2
    for i in range(2, 18):
        ds.column_dimensions[L(i)].width = 11.5
    ds.merge_cells("B1:Q1")
    ds["B1"] = f"{config.cfg('profile').get('name') or 'My'} · Job Applications"
    ds["B1"].font, ds["B1"].fill = Font(size=22, bold=True, color="FFFFFF"), fill(NAVY)
    ds["B1"].alignment = Alignment(vertical="center", indent=1)
    ds.row_dimensions[1].height = 44
    ds.merge_cells("B2:Q2")
    ds["B2"] = (f"Updated {now:%d %b %Y, %H:%M} IST · {N['companies']} companies · response rate counts real answers "
                "only (viewed, test, interview, rejection, offer)")
    ds["B2"].font = Font(size=10, italic=True, color=MUTED)
    ds["B2"].alignment = Alignment(indent=1)
    for k, (label, colour) in enumerate(TILE.items()):
        c0 = 2 + 2 * k                             # each tile: 2 columns × (label row + 2 number rows)
        a, b = L(c0), L(c0 + 1)
        ds.merge_cells(f"{a}4:{b}4")
        ds.merge_cells(f"{a}5:{b}6")
        ds[f"{a}4"] = label
        ds[f"{a}5"] = N[label]
        ds[f"{a}4"].font = Font(size=10, bold=True, color="FFFFFF")
        ds[f"{a}5"].font = Font(size=26, bold=True, color="FFFFFF")
        for r in (4, 5, 6):
            for c in (a, b):
                ds[f"{c}{r}"].fill = fill(colour)
        ds[f"{a}4"].alignment = Alignment(horizontal="center", vertical="bottom")
        ds[f"{a}5"].alignment = Alignment(horizontal="center", vertical="center")
    ds.row_dimensions[4].height, ds.row_dimensions[5].height, ds.row_dimensions[6].height = 20, 24, 24
    # chart data, right of the dashboard (columns T–W), small and grey: the charts read it
    ds["T3"], ds["U3"] = "Week of", "Applications"
    monday = (now.date() - dt.timedelta(days=now.weekday()))
    for i in range(8):
        wk = monday - dt.timedelta(weeks=7 - i)
        n = sum(1 for r in R if r["applied"] and wk <= r["applied"].date() < wk + dt.timedelta(days=7))
        ds.cell(row=4 + i, column=20, value=f"{wk:%d %b}")
        ds.cell(row=4 + i, column=21, value=n)
    ds["W3"], ds["X3"] = "Status", "Jobs"
    present = [s for s in STATUS_ORDER if any(r["status"] == s for r in R)] or ["applied"]
    for i, s in enumerate(present):
        ds.cell(row=4 + i, column=23, value=s.capitalize())
        ds.cell(row=4 + i, column=24, value=sum(1 for r in R if r["status"] == s))
    for col in "TUWX":
        ds.column_dimensions[col].width = 11
        for r in range(3, 13):
            ds[f"{col}{r}"].font = Font(size=8, color="9CA3AF")
    bar = BarChart()
    bar.type, bar.style, bar.title = "col", 10, "Applications per week (last 8 weeks)"
    bar.add_data(Reference(ds, min_col=21, min_row=3, max_row=11), titles_from_data=True)
    bar.set_categories(Reference(ds, min_col=20, min_row=4, max_row=11))
    bar.legend, bar.y_axis.majorGridlines = None, None
    bar.y_axis.title, bar.gapWidth = "applications", 60
    bar.y_axis.majorUnit, bar.y_axis.numFmt = max(1, -(-max(ds.cell(row=4 + i, column=21).value for i in range(8)) // 5)), "0"
    bar.y_axis.scaling.min = 0
    bar.series[0].graphicalProperties.solidFill = "2E6DA4"
    bar.dataLabels = DataLabelList(showVal=True, showSerName=False, showCatName=False, showLegendKey=False)
    bar.height, bar.width = 7.5, 19
    ds.add_chart(bar, "B8")
    pie = DoughnutChart(holeSize=55)
    pie.title = "By status"
    pie.add_data(Reference(ds, min_col=24, min_row=3, max_row=3 + len(present)), titles_from_data=True)
    pie.set_categories(Reference(ds, min_col=23, min_row=4, max_row=3 + len(present)))
    from openpyxl.chart.series import DataPoint
    for i, s in enumerate(present):
        pt = DataPoint(idx=i)
        pt.graphicalProperties.solidFill = {"applied": "2E6DA4", "interview": "B7791F", "offer": "2F855A",
                                            "rejected": "B83232"}[s]
        pie.series[0].dPt.append(pt)
    pie.dataLabels = DataLabelList(showVal=True, showSerName=False, showCatName=False, showLegendKey=False,
                                   showPercent=False)
    pie.height, pie.width = 7.5, 11.8
    ds.add_chart(pie, "L8")
    top = 24                                       # next actions table under the charts
    ds.merge_cells(f"B{top}:Q{top}")
    ds[f"B{top}"] = "Next actions"
    ds[f"B{top}"].font = Font(size=14, bold=True, color=NAVY)
    heads = [("B", "C", "Due"), ("D", "I", "What to do"), ("J", "L", "Company"), ("M", "P", "Role"), ("Q", "Q", "Job id")]
    acts = actions(db, R) or [(None, "Nothing due: new applications show here after 7 days without a reply", "", "", "")]
    for k, row in enumerate([None, *acts]):
        r = top + 1 + k
        for (a, b, h), v in zip(heads, [h for *_, h in heads] if row is None else
                                [row[0].date() if row[0] else "", row[1], row[2], row[3], row[4]]):
            if a != b:
                ds.merge_cells(f"{a}{r}:{b}{r}")
            c = ds[f"{a}{r}"]
            c.value = v
            c.alignment = Alignment(vertical="center", wrap_text=True)
            if row is None:
                c.font, c.fill = Font(bold=True, color="FFFFFF"), fill(NAVY)
            elif k % 2 == 0:
                for col in range(ord(a), ord(b) + 1):
                    ds[f"{chr(col)}{r}"].fill = fill("EEF2F7")
            if a == "B" and row is not None and v:
                c.number_format = "DD-MMM-YYYY"
            ds[f"{a}{r}"].border = Border(bottom=thin)
        ds.row_dimensions[r].height = 20
    _print(ds)
    ds.print_area = f"B1:Q{top + 1 + len(acts)}"

    # ── Applications: a real Excel Table ─────────────────────────────────────
    ws = wb.create_sheet("Applications")
    heads = H + custom + TAIL
    col = {h: i for i, h in enumerate(heads, 1)}
    table_sheet(ws, "Applications", heads, [WIDTH.get(h, 20) for h in heads])
    snapshot = []
    for r in R:
        a = r["applied"]
        extra = [vals.get((r["full_id"], c), "") for c in custom]
        ws.append([a.date() if a else None, a.time().replace(second=0, microsecond=0) if a else None, r["dream"],
                   r["company"], r["role"], r["location"], r["mode"], r["pay"], r["score"], r["via"], r["status"],
                   r["status_at"].date() if r["status_at"] else None, r["conf_id"],
                   "Open job" if r["url"] else "", "Open form" if r["form_url"] else "", r["last_mail"], r["mails"],
                   r["waiting"], r["next"], r["ref_name"], r["ref_status"], r["ref_at"].date() if r["ref_at"] else None,
                   r["resume"], r["fixed"], *extra, r["notes"], r["id"]])
        snapshot += [(r["full_id"], c, v) for c, v in zip(["Notes", *custom], [r["notes"], *extra])]
        n = ws.max_row
        for h in ("Applied on", "Status since", "Referral date"):
            ws.cell(n, col[h]).number_format = "DD-MMM-YYYY"
        ws.cell(n, col["Time (IST)"]).number_format = "HH:MM"
        for h, u in (("Job link", r["url"]), ("Application form", r["form_url"])):
            if u:
                ws.cell(n, col[h]).hyperlink, ws.cell(n, col[h]).font = u, link_font
        for c in ws[n]:
            c.alignment = wrap
    add_table(ws, "Applications", len(heads))
    last = max(ws.max_row, 2)
    st = L(col["Status"])
    for s, (bg, fg) in {"applied": ("DBEAFE", "1E40AF"), "interview": ("FEF3C7", "92400E"), "offer": ("D1FAE5", "065F46"),
                        "rejected": ("FEE2E2", "991B1B")}.items():
        ws.conditional_formatting.add(f"{st}2:{st}{last}", FormulaRule(formula=[f'${st}2="{s}"'], fill=fill(bg),
                                                                     font=Font(bold=True, color=fg)))
    dw = L(col["Days waiting"])
    ws.conditional_formatting.add(f"{dw}2:{dw}{last}", FormulaRule(formula=[f"AND(ISNUMBER(${dw}2),${dw}2>14)"],
                                                                 fill=fill("FECACA"), font=Font(bold=True, color="991B1B")))

    # ── Email replies ────────────────────────────────────────────────────────
    from .mail import replies
    ws2 = wb.create_sheet("Email replies")
    eh = ["Date", "Time (IST)", "Kind", "Company", "Role", "From", "Subject", "Matched by", "Job id"]
    table_sheet(ws2, "Replies", eh, [12, 9, 12, 24, 30, 30, 56, 30, 10])
    for m in replies(db, limit=100000):
        d = _dt(m["at"])
        j = db.get(m["job_id"]) if m.get("job_id") else None
        ws2.append([d.date() if d else None, d.time().replace(second=0, microsecond=0) if d else None, m["kind"],
                    j["company"] if j else "", j["title"] if j else "",
                    f"{m['sender_name']} <{m['sender']}>" if m["sender_name"] else m["sender"],
                    m["subject"], m["match"] or "not matched to an application", (m["job_id"] or "")[:8]])
        ws2.cell(ws2.max_row, 1).number_format, ws2.cell(ws2.max_row, 2).number_format = "DD-MMM-YYYY", "HH:MM"
        for c in ws2[ws2.max_row]:
            c.alignment = wrap
    add_table(ws2, "Replies", len(eh))
    last2 = max(ws2.max_row, 2)
    for k, (bg, fg) in {"offer": ("D1FAE5", "065F46"), "interview": ("FEF3C7", "92400E"), "assessment": ("FFEDD5", "9A3412"),
                        "rejected": ("FEE2E2", "991B1B"), "viewed": ("E0E7FF", "3730A3"), "received": ("F3F4F6", "374151")}.items():
        ws2.conditional_formatting.add(f"C2:C{last2}", FormulaRule(formula=[f'$C2="{k}"'], fill=fill(bg),
                                                                   font=Font(bold=True, color=fg)))
    # ── Referrals: every one you asked for, applied yet or not ──────────────
    ws3 = wb.create_sheet("Referrals")
    rh = ["Date", "Company", "Role", "Referrer", "Status", "Job status", "Job link", "Job id"]
    table_sheet(ws3, "Referrals", rh, [12, 24, 32, 22, 11, 12, 10, 10])
    for x in referrals(db):
        j, d = x["job"] or {}, _dt(x["at"])
        ws3.append([d.date() if d else None, j.get("company", ""), j.get("title", ""), x["name"], x["status"],
                    j.get("status", "deleted"), "Open job" if j.get("url") else "", x["job_id"][:8]])
        ws3.cell(ws3.max_row, 1).number_format = "DD-MMM-YYYY"
        if j.get("url"):
            ws3.cell(ws3.max_row, 7).hyperlink, ws3.cell(ws3.max_row, 7).font = j["url"], link_font
    add_table(ws3, "Referrals", len(rh))
    wb.active = 0
    tmp = out.with_name(f".{out.name}.tmp")
    wb.save(tmp)
    os.replace(tmp, out)                          # one file, replaced in one step: never half-written, never a copy
    db.c.execute("DELETE FROM sheet_snapshot")    # what the file says now: the next read-back finds only YOUR edits
    db.c.executemany("INSERT INTO sheet_snapshot VALUES (?,?,?)", snapshot)
    db.c.commit()
    return out


def preview(out_dir: Path, f: Path | None = None) -> list[Path]:
    """PNG of each sheet (LibreOffice headless renders a workbook's first sheet): Dashboard, Applications, Email,
    replies. Temporary one-sheet copies go in out_dir, never next to the real file."""
    import shutil
    import subprocess
    from openpyxl import load_workbook
    f, out_dir = f or path(), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("LibreOffice isn't installed (sudo pacman -S libreoffice-fresh)")
    made = []
    for name in ("Dashboard", "Applications", "Email replies", "Referrals"):
        src = out_dir / f"{name.replace(' ', '_')}.xlsx"
        if name == "Dashboard":
            shutil.copyfile(f, src)                # first sheet already; charts need the untouched file
        else:
            wb = load_workbook(f)
            for other in [s for s in wb.sheetnames if s != name]:
                del wb[other]
            wb.save(src)
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(src)],
                       capture_output=True, timeout=180)           # PDF first: page 1 at 170 dpi stays readable
        src.unlink(missing_ok=True)
        pdf, png = src.with_suffix(".pdf"), src.with_suffix(".png")
        if not pdf.exists():
            continue
        subprocess.run(["pdftoppm", "-r", "170", "-png", "-singlefile", "-f", "1", "-l", "1", str(pdf),
                        str(png.with_suffix(""))], capture_output=True, timeout=120)
        pdf.unlink(missing_ok=True)
        try:                                       # trim the white page margin
            from PIL import Image, ImageChops
            im = Image.open(png).convert("RGB")
            box = ImageChops.difference(im, Image.new("RGB", im.size, "white")).getbbox()
            if box:
                im.crop((max(0, box[0] - 24), max(0, box[1] - 24), min(im.width, box[2] + 24),
                         min(im.height, box[3] + 24))).save(png)
        except (ImportError, OSError):
            pass
        if png.exists():
            made.append(png)
    return made


def refresh(db: DB | None = None) -> Path | None:
    """Rewrite the tracker; never let a tracker problem break an apply or a sync."""
    try:
        return write(db)
    except Exception as exc:
        print(f"tracker not updated: {type(exc).__name__}: {exc}")
        return None
