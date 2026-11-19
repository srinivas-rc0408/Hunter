"""Government job radar: official notice boards → new tech notices → eligibility.

Govt applications are never auto-submitted (OTR login, OTP, fee payment,
document upload). The radar finds them the day they're posted, checks your
eligibility (SC relaxation, final-year rules, branch wording, GATE paper) and
tells you, with the deadline.
"""
from __future__ import annotations

import re

from .. import config
from ..db import DB, Job
from ..eligibility import check
from ..util import Http
from . import notice, pages

KARNATAKA_WORDS = ("bengaluru", "bangalore", "karnataka", "mysuru", "mysore", "mangaluru", "hubballi",
                   "dharwad", "belagavi", "kalaburagi")


def match_rule(text: str, source_id: str, rules: list[dict]) -> dict | None:
    t = f"{source_id} {text}".lower()
    for r in rules:
        if re.search(r["match"], t):
            return r
    return None


def notice_text(http: Http, link) -> tuple[str, str | None]:
    """The notification's text and its PDF link: the PDF itself, or the notice page plus the advert PDF on it."""
    if link.is_pdf:
        return pages.pdf_text(http, link.href), link.href
    if not http.allowed(link.href):
        return "", None
    try:
        r = http.get(link.href)
    except Exception:
        return "", None
    if r.status_code >= 400:
        return "", None
    if b"%PDF" in r.content[:1024]:
        return pages.pdf_text(http, link.href), link.href
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "lxml")
    for t in soup(["script", "style", "nav", "header", "footer"]):
        t.decompose()
    pdfs = [l for l in pages.extract_links(r.text, link.href) if l.is_pdf and
            re.search(r"advt|advertisement|notification|detailed|ಅಧಿಸೂಚನೆ", l.text + " " + l.href, re.I)]
    body = soup.get_text("\n", strip=True)[:20000]
    return body + ("\n" + pages.pdf_text(http, pdfs[0].href) if pdfs else ""), (pdfs[0].href if pdfs else None)


def _old(text: str, year: int) -> bool:
    """A notice whose text names only past years (archives like 'Advt KP/S/02/2014')."""
    ys = [int(y) for y in re.findall(r"\b(20[0-3]\d)\b", text)]
    return bool(ys) and max(ys) < year


def scan(db: DB, errors: list[str], baseline_new: bool = False) -> list[Job]:
    import datetime as dt
    g = config.cfg("govt")
    prof = config.cfg("profile")
    rel = g.get("relevance", {})
    include, exclude, tech = rel.get("include", []), rel.get("exclude", []), rel.get("tech", [])
    http = Http(pause=1.5, respect_robots=g.get("respect_robots_txt", True))
    year = dt.date.today().year
    out: list[Job] = []
    for src in g.get("sources", []):
        kind = src.get("kind", "central")
        for link in pages.watch(db, http, src["url"], include, exclude, errors, baseline_new):
            text = f"{link.text}. {link.context}"
            is_tech = any(w in text.lower() for w in tech)
            if kind == "aggregator" and not any(w in text.lower() for w in KARNATAKA_WORDS + tuple(tech)):
                continue
            if _old(text, year):
                continue
            body, pdf = notice_text(http, link)
            full = text + "\n" + body
            d = notice.read(full, link.text[:200], kind)
            d["pdf"] = pdf
            if d["released"] and d["released"][:4].isdigit() and int(d["released"][:4]) < year - 1:
                continue                                    # an old notice still linked on the board
            deadline = d["last_date"] or pages.find_deadline(full)
            rule = match_rule(full, src["id"], g.get("rules", []))
            rkind = "state" if kind == "state" else "central"
            v = check(full, rule, prof, rkind, on=check_date(d), deadline=deadline)
            flags = [f"govt:{kind}", f"eligibility:{v.verdict}"] + (["tech"] if is_tech else [])
            ka = d["karnataka_places"] or [w.title() for w in KARNATAKA_WORDS if w in full.lower()][:1]
            loc = ("Karnataka" + (f" ({', '.join(p for p in ka if p != 'Karnataka')})" if set(ka) - {"Karnataka"} else "")
                   if kind == "state" else
                   "All India" + (f" · Karnataka postings: {', '.join(ka)}" if ka else " · Karnataka postings: not stated"))
            if kind != "state" and not ka:
                flags.append("location:check")
            desc = "\n".join(v.lines()) + "\n\n" + (rule or {}).get("note", "") + "\n\n" + full[:6000]
            j = Job(title=link.text[:200], company=src["name"], location=loc, url=link.href,
                    source=f"govt:{src['id']}", description=desc.strip(), kind="govt", deadline=deadline, details=d)
            j.flags = flags
            j.reasons = v.lines()[:6] + ([f"pay: {rule['pay']}"] if rule and rule.get("pay") else [])
            # govt score: tech + eligible first; non-tech posts still appear (you said be flexible)
            j.score = (40 if is_tech else 15) + {"eligible": 40, "check": 25, "ineligible": 0}[v.verdict] + \
                (10 if kind in ("central", "psu") else 5) + (10 if rule else 0)
            out.append(j)
    return out


def check_date(d: dict):
    """Eligibility is judged on the notice's own cut-off date (else the last date)."""
    from ..eligibility import to_date
    return to_date(d.get("age_cutoff") or d.get("last_date"))
