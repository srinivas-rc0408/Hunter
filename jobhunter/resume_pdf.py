"""Resumes: master data → hunter-resume markdown → ATS score + truth check → one-page PDF.

Flow for a job (`hunt tailor <id>`, also run automatically before every apply):
  1. hunter/resumes/base.md      : your master resume (config/resume.yaml + private.yaml)
  2. hunter/resumes/<id>.jd.md   : the job description
  3. ats.py score base.md        : "before" score + what the JD asks that you have / lack
  4. tailor: projects ordered by relevance, JD's matching skills first, and (if an
     LLM is available) bullets reworded into the JD's own words
  5. ats.py compare --facts      : truth check. Any invented number/skill → the
     LLM rewrite is thrown away and the deterministic version is used
  6. hunter/resumes/<id>.md + data/resumes/<Name>_Resume_<id>.pdf (one page, ATS-safe)
The before/after score is saved on the job and shown in the Review Center.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Table, TableStyle

from . import config, llm
from .util import norm


# ── data ────────────────────────────────────────────────────────────────────
def master() -> dict:
    return copy.deepcopy(config.cfg("resume"))


def _live(items):
    out = []
    for it in items or []:
        if isinstance(it, str):
            if "TODO" not in it:
                out.append(it)
            continue
        it = dict(it)
        it["bullets"] = [b for b in it.get("bullets", []) if b and "TODO" not in b]
        out.append(it)
    return out


def jd_keywords(jd: str) -> set[str]:
    from .tailor import keywords
    return {norm(k) for k in keywords(jd)}


def relevance(item: dict, jd: str) -> int:
    t = " " + norm(jd) + " "
    terms = [*(item.get("tags") or []), *(item.get("stack") or [])]
    pts = sum(3 for x in terms if f" {norm(x)} " in t)
    for b in item.get("bullets", []):
        pts += sum(1 for w in set(re.findall(r"[A-Za-z][A-Za-z+.#-]{2,}", b)) if f" {norm(w)} " in t and len(w) > 3)
    return pts


def tailor_data(job: dict | None, max_projects: int = 4) -> dict:
    """Deterministic tailoring: select + reorder only. Never changes a word."""
    d = master()
    jd = (job or {}).get("description") or ""
    projs = [p for p in _live(d.get("projects")) if p.get("bullets")]
    if jd:
        projs.sort(key=lambda p: relevance(p, jd), reverse=True)
        for p in projs:   # most relevant bullet first inside each project
            p["bullets"].sort(key=lambda b: relevance({"bullets": [b]}, jd), reverse=True)
    d["projects"] = projs[:max_projects]
    d["experience"] = [e for e in _live(d.get("experience")) if e.get("bullets")]
    prof = config.cfg("profile")
    first = jd_keywords(jd) if jd else set()
    d["skills"] = {g: sorted(v or [], key=lambda s: 0 if norm(s) in first else 1)
                   for g, v in (prof.get("skills") or {}).items()}
    if first:  # groups with JD hits first
        d["skills"] = dict(sorted(d["skills"].items(), key=lambda kv: -sum(norm(s) in first for s in kv[1])))
    return d


# ── markdown (hunter-resume's base.md format) ───────────────────────────────
def to_markdown(d: dict) -> str:
    p = config.cfg("profile")
    loc = p.get("location", {})
    links = p.get("links", {})
    L = [p.get("name", ""), " · ".join(x for x in [f"{loc.get('city')}, {loc.get('state')}", p.get("phone"),
                                                  p.get("email"), links.get("linkedin"), links.get("github"),
                                                  links.get("portfolio")] if x), ""]
    if d.get("summary"):
        L += ["Summary", d["summary"], ""]
    if d.get("experience"):
        L.append("Experience")
        for e in d["experience"]:
            L.append(f"{e['title']}, {e['company']}, {e.get('location', '')}, {e.get('start')} to {e.get('end')}")
            L += [f"- {b}" for b in e["bullets"]]
        L.append("")
    if d.get("projects"):
        L.append("Projects")
        for x in d["projects"]:
            L.append(f"{x['name']} ({', '.join(x.get('stack') or [])}), {x.get('date', '')}")
            L += [f"- {b}" for b in x["bullets"]]
        L.append("")
    L.append("Education")
    for e in p.get("education", []):
        if e.get("level") == "bachelors":
            L.append(f"{e['institution']}, {e['degree']} in {e['branch']}, {e['start']} to {e['end']}, CGPA {e['cgpa']}/10")
        elif e.get("percent"):
            L.append(f"{e['degree']}, {e.get('institution') or ''}, {e['percent']}%, {e.get('year')}")
    L += ["", "Skills"]
    L += [f"{g.replace('_', ' ').title()}: {', '.join(v)}" for g, v in d.get("skills", {}).items() if v]
    certs = _live(d.get("certifications"))
    if certs:
        L += ["", "Certifications"] + [f"- {c}" for c in certs]
    lead = _live(d.get("leadership"))
    if lead or d.get("achievements"):
        L += ["", "Leadership and Achievements"]
        for x in lead:
            L.append(f"{x['title']}, {x['org']}, {x.get('start')} to {x.get('end')}")
            L += [f"- {b}" for b in x.get("bullets", [])]
        L += [f"- {a}" for a in _live(d.get("achievements"))]
    return "\n".join(L).strip() + "\n"


def hunter_dir() -> Path:
    d = config.home() / "hunter" / "resumes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_base() -> Path:
    f = hunter_dir() / "base.md"
    f.write_text(to_markdown(tailor_data(None, max_projects=99)), encoding="utf-8")
    facts = hunter_dir() / "facts.md"
    fx = config.cfg("resume").get("facts") or []
    facts.write_text("\n".join(f"- {x}" for x in fx) + ("\n" if fx else ""), encoding="utf-8")
    return f


def _ats(*args: str) -> dict | None:
    script = config.path("skills", "hunter-resume", "ats.py")
    try:
        r = subprocess.run([sys.executable, str(script), *args, "--json"], capture_output=True, text=True, timeout=60)
        return json.loads(r.stdout) if r.stdout.strip().startswith("{") else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def _best_selection(job: dict, base: Path, jd_f: Path, keep: int = 4) -> dict:
    """Pick the `keep` projects that give the highest hunter-resume ATS score for this JD
    (the one-page resume can't hold all of them). Falls back to relevance order."""
    import itertools
    full = tailor_data(job, max_projects=99)
    projs = full["projects"]
    if len(projs) <= keep or not (job.get("description") or "").strip():
        full["projects"] = projs[:keep]
        return full
    best, best_score = None, -1
    tmp = hunter_dir() / "_try.md"
    for combo in itertools.combinations(range(len(projs)), keep):
        trial = dict(full, projects=[projs[i] for i in combo])   # combo keeps relevance order
        tmp.write_text(to_markdown(trial), encoding="utf-8")
        sc = (_ats("score", str(tmp), "--jd", str(jd_f)) or {}).get("raw", -1)
        if sc > best_score:
            best, best_score = trial, sc
    tmp.unlink(missing_ok=True)
    return best or dict(full, projects=projs[:keep])


# ── LLM rewording (optional, truth-checked) ────────────────────────────────
REWORD_SYS = """You reword resume bullets so they use the job description's own words for things the
candidate really did. Rules: keep every number, tool and fact exactly; add NO new number, tool, skill,
employer, metric or claim; start with a strong past-tense verb when the work is finished (keep present
tense for ongoing work); 12-28 words each. Return JSON: {"bullets": ["...", ...]} with the same count and order."""


def _reword(bullets: list[str], jd: str) -> list[str] | None:
    out = llm.chat(REWORD_SYS, f"JOB DESCRIPTION:\n{jd[:3000]}\n\nBULLETS:\n" + json.dumps(bullets), max_tokens=1200)
    if not out:
        return None
    m = re.search(r"\{.*\}", out, re.S)
    try:
        new = json.loads(m.group(0))["bullets"] if m else None
    except (ValueError, KeyError):
        return None
    return new if isinstance(new, list) and len(new) == len(bullets) and all(isinstance(b, str) for b in new) else None


# ── tailoring for one job ──────────────────────────────────────────────────
def tailor_job(job: dict, use_llm: bool = True) -> dict:
    jd = job.get("description") or ""
    base = write_base()
    hd = hunter_dir()
    jid = job["id"][:12]
    jd_f = hd / f"{jid}.jd.md"
    jd_f.write_text(f"{job['title']} at {job['company']}\n\n{jd}", encoding="utf-8")
    data = _best_selection(job, base, jd_f)
    how = "reordered"
    if use_llm and jd and llm.available():
        trial = copy.deepcopy(data)
        for sec in ("experience", "projects"):
            for item in trial.get(sec, []):
                new = _reword(item["bullets"], jd)
                if new:
                    item["bullets"] = new
        trial_f = hd / f"{jid}.llm.md"
        trial_f.write_text(to_markdown(trial), encoding="utf-8")
        cmp_ = _ats("compare", str(base), str(trial_f), "--jd", str(jd_f), "--facts", str(hd / "facts.md"))
        if cmp_ and cmp_.get("trace", {}).get("clean"):
            data, how = trial, "reworded (truth check CLEAN)"
        else:
            how = "reordered (LLM rewrite rejected by truth check)"
        trial_f.unlink(missing_ok=True)
    out_md = hd / f"{jid}.md"
    out_md.write_text(to_markdown(data), encoding="utf-8")
    cmp_ = _ats("compare", str(base), str(out_md), "--jd", str(jd_f), "--facts", str(hd / "facts.md")) or {}
    score = _ats("score", str(out_md), "--jd", str(jd_f)) or {}
    pdf = build(pdf_path(job), data)
    return {"pdf": str(pdf), "md": str(out_md), "how": how, "before": cmp_.get("before"), "after": cmp_.get("after"),
            "clean": (cmp_.get("trace") or {}).get("clean", True),
            "missing": [m["term"] for m in score.get("missing", [])][:12],
            "reword": [f"{r['you_wrote']} → {r['posting_says']}" for r in score.get("reword", [])][:8]}


def pdf_path(job: dict | None) -> Path:
    who = "_".join(config.cfg("profile").get("name", "Resume").split())
    d = config.data_dir() / "resumes"
    d.mkdir(parents=True, exist_ok=True)
    return d / (f"{who}_Resume_{job['id'][:8]}.pdf" if job else f"{who}_Resume.pdf")


# ── PDF (one page, single column, real text) ───────────────────────────────
def _esc(s) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


ACC = HexColor("#1f3a5f")


def _story(d: dict, n_proj: int, n_bul: int, n_cert: int, size: float) -> list:
    p = config.cfg("profile")
    S = {
        "name": ParagraphStyle("n", fontName="Helvetica-Bold", fontSize=size + 8.5, alignment=TA_CENTER, leading=size + 11, textColor=ACC),
        "hl": ParagraphStyle("hl", fontName="Helvetica", fontSize=size + 0.6, alignment=TA_CENTER, leading=size + 3),
        "c": ParagraphStyle("c", fontName="Helvetica", fontSize=size - 0.6, alignment=TA_CENTER, leading=size + 2.4),
        "h": ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=size + 1.2, spaceBefore=4.5, leading=size + 3, textColor=ACC),
        "b": ParagraphStyle("b", fontName="Helvetica", fontSize=size, leading=size + 2.3, leftIndent=9, bulletIndent=1),
        "t": ParagraphStyle("t", fontName="Helvetica", fontSize=size, leading=size + 2.3),
        "r": ParagraphStyle("r", fontName="Helvetica", fontSize=size, leading=size + 2.3, alignment=2),
    }
    links = p.get("links", {})
    strip = lambda u: (u or "").replace("https://", "").replace("http://", "").replace("www.", "")
    el = [Paragraph(_esc(p.get("name")), S["name"])]
    if d.get("headline"):
        el.append(Paragraph(_esc(d["headline"]), S["hl"]))
    contact = " | ".join(x for x in [f"{p['location']['city']}, {p['location']['state']}", p.get("phone"), p.get("email")] if x)
    lk = " | ".join(f'<link href="{_esc(links[k])}">{_esc(strip(links[k]))}</link>' for k in ("portfolio", "linkedin", "github") if links.get(k))
    el += [Paragraph(_esc(contact), S["c"]), Paragraph(lk, S["c"])]

    def head(t):
        el.extend([Paragraph(t.upper(), S["h"]), HRFlowable(width="100%", thickness=0.7, color=ACC, spaceAfter=1.5)])

    def row(left, right):
        t = Table([[Paragraph(left, S["t"]), Paragraph(_esc(right), S["r"])]], colWidths=["78%", "22%"])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0.5),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5)]))
        el.append(t)

    if d.get("summary"):
        head("Summary")
        el.append(Paragraph(_esc(d["summary"]), S["t"]))
    head("Education")
    for e in p.get("education", []):
        if e.get("level") == "bachelors":
            row(f"<b>{_esc(e['degree'])} in {_esc(e['branch'])}</b>, {_esc(e['institution'])} | CGPA: {e['cgpa']}/{e.get('cgpa_scale', 10)}",
                f"{e['start']} – {e['end']}")
        elif e.get("percent"):
            row(f"{_esc(e['degree'])} | {_esc(e.get('institution') or '')} | {e['percent']}%", str(e.get("year") or ""))
    if d.get("experience"):
        head("Experience")
        for x in d["experience"]:
            row(f"<b>{_esc(x['title'])}</b> | {_esc(x['company'])} | {_esc(x.get('location', ''))}", f"{x.get('start')} – {x.get('end')}")
            if x.get("stack"):
                el.append(Paragraph(f"<i>{_esc(('Project: ' + x['project'] + ' | ') if x.get('project') else '')}{_esc(', '.join(x['stack']))}</i>", S["t"]))
            for b in x["bullets"][:n_bul + 1]:
                el.append(Paragraph(_esc(b), S["b"], bulletText="•"))
    if d.get("projects"):
        head("Projects")
        for x in d["projects"][:n_proj]:
            lk = []
            if x.get("live"):
                lk.append(f'<link href="{_esc(x["live"])}">Live</link>')
            if x.get("link"):
                lk.append(f'<link href="{_esc(x["link"])}">GitHub</link>')
            row(f"<b>{_esc(x['name'])}</b>" + (f" | {' / '.join(lk)}" if lk else ""), x.get("date", ""))
            el.append(Paragraph(f"<i>{_esc((x['context'] + ' | ') if x.get('context') else '')}{_esc(', '.join(x.get('stack') or []))}</i>", S["t"]))
            for b in x["bullets"][:n_bul]:
                el.append(Paragraph(_esc(b), S["b"], bulletText="•"))
    head("Technical Skills")
    for g, items in d.get("skills", {}).items():
        if items:
            el.append(Paragraph(f"<b>{_esc(g.replace('_', ' ').title().replace('Ml', 'ML / AI').replace('Core Cs', 'Core CS'))}:</b> "
                                f"{_esc(', '.join(items))}", S["t"]))
    certs = _live(d.get("certifications"))[:n_cert]
    if certs:
        head("Certifications")
        el.append(Paragraph(_esc(" · ".join(certs)), S["t"]))
    lead, ach = _live(d.get("leadership")), _live(d.get("achievements"))
    if lead or ach:
        head("Leadership and Achievements")
        for x in lead:
            row(f"<b>{_esc(x['title'])}</b> | {_esc(x['org'])}", f"{x.get('start')} – {x.get('end')}")
            for b in x.get("bullets", [])[:n_bul]:
                el.append(Paragraph(_esc(b), S["b"], bulletText="•"))
        for a in ach:
            el.append(Paragraph(_esc(a), S["b"], bulletText="•"))
    return el


def build(out: Path, data: dict | None = None) -> Path:
    """Fit on ONE A4 page: shrink detail step by step until it does."""
    from pypdf import PdfReader
    d = data or tailor_data(None)
    out.parent.mkdir(parents=True, exist_ok=True)
    p = config.cfg("profile")
    for n_proj, n_bul, n_cert, size in [(4, 3, 4, 9.2), (4, 3, 3, 8.9), (3, 3, 3, 9.0), (3, 2, 3, 9.0),
                                        (3, 2, 2, 8.7), (2, 2, 2, 8.7), (2, 2, 2, 8.3)]:
        doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=9 * mm,
                                bottomMargin=8 * mm, title=f"{p.get('name')} Resume", author=p.get("name", ""))
        doc.build(_story(d, n_proj, n_bul, n_cert, size))
        if len(PdfReader(str(out)).pages) == 1:
            break
    return out


