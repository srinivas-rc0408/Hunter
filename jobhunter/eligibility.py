"""Government eligibility: age (with SC relaxation), degree timing, branch, marks, GATE.

The verdict is one of:
  eligible    – nothing in the notice or the known rules blocks you
  check       – probably fine, but something needs your eyes (branch wording,
                degree-in-hand date, a number we could not read)
  ineligible  – a hard rule fails (wrong GATE paper, over age even after relaxation)
Always confirm on the official notification before paying a fee.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

AI_BRANCH_WORDS = ("artificial intelligence", "machine learning", "ai & ml", "ai&ml", "aiml", "ai and ml",
                   "data science", "allied", "equivalent", "any branch", "any discipline", "any engineering",
                   "related discipline", "relevant discipline", "computer science and allied")
CENTRAL_RELAX = {"SC": 5, "ST": 5, "OBC": 3, "GM": 0, "EWS": 0}


@dataclass
class Verdict:
    verdict: str = "eligible"
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def finish(self) -> "Verdict":
        self.verdict = "ineligible" if self.blockers else ("check" if self.risks else "eligible")
        return self

    def lines(self) -> list[str]:
        return [f"✗ {b}" for b in self.blockers] + [f"? {r}" for r in self.risks] + [f"✓ {r}" for r in self.reasons]


def to_date(v) -> dt.date | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def age_on(dob: dt.date, on: dt.date) -> float:
    years = on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))
    return years


def text_age_max(text: str) -> int | None:
    t = text.lower()
    for pat in (r"(?:upper\s+)?age\s*limit[^0-9]{0,60}?(?:\d{2}\s*(?:-|to)\s*)?(\d{2})\s*years",
                r"(?:not\s+(?:be\s+)?(?:more|exceed(?:ing)?)\s+(?:than\s+)?)(\d{2})\s*years",
                r"maximum\s+age[^0-9]{0,30}(\d{2})"):
        m = re.search(pat, t)
        if m:
            v = int(m.group(1))
            if 18 <= v <= 60:
                return v
    return None


def check(text: str, rule: dict | None, profile: dict, kind: str, on: dt.date | None = None,
          deadline: str | None = None) -> Verdict:
    rule = rule or {}
    v = Verdict()
    t = (text or "").lower()
    on = on or to_date(deadline) or dt.date.today()
    dob = to_date(profile.get("date_of_birth"))
    cat = (profile.get("category") or "GM").upper()
    edu = next((e for e in profile.get("education", []) if e.get("level") == "bachelors"), {})
    grad_year = int(edu.get("end") or 0)
    cgpa = float(edu.get("cgpa") or 0)

    # ── age ────────────────────────────────────────────────────────────────
    if dob:
        age = age_on(dob, on)
        if kind == "state":
            limits = rule.get("age_limits_state") or {"GM": 35, "SC": 40, "ST": 40, "Cat-1": 40}
            limit = limits.get(cat, limits.get("GM", 35))
            until = to_date(rule.get("temporary_relaxation_until"))
            extra = rule.get("temporary_relaxation_years", 0) if until and on <= until else 0
            limit_txt = f"{limit}+{extra}" if extra else str(limit)
            limit += extra
        else:
            base = text_age_max(t) or rule.get("age_max")
            limit = base + CENTRAL_RELAX.get(cat, 0) if base else None
            limit_txt = f"{base}+{CENTRAL_RELAX.get(cat, 0)} ({cat})" if base else "?"
        if limit is None:
            v.reasons.append(f"age {age}: no upper limit read; you're young enough for almost every post")
        elif age > limit:
            v.blockers.append(f"age {age} > limit {limit_txt}")
        else:
            v.reasons.append(f"age {age} ≤ limit {limit_txt}")
        if age < 18:
            v.blockers.append("under 18")

    # ── degree timing ──────────────────────────────────────────────────────
    final_no = bool(re.search(r"(?:final[\s-]*year|appearing|final\s+semester)[^.]{0,60}\b(?:not|ineligible|shall not)\b", t))
    final_ok = not final_no and bool(re.search(
        r"final[\s-]*year|appearing|awaiting\s+result|result\s+awaited|final\s+semester", t))
    must_have = rule.get("degree_in_hand") or bool(re.search(
        r"must\s+(?:have\s+)?(?:passed|possess|completed)|should\s+(?:have\s+)?(?:passed|possess)|"
        r"degree\s+(?:in hand|completed)|declared\s+(?:on or )?before", t))
    if edu.get("final_year") and grad_year and on.year <= grad_year:
        if final_no:
            v.blockers.append(f"final-year students not eligible (you graduate {grad_year})")
        elif final_ok and not rule.get("degree_in_hand"):
            v.reasons.append("final-year/appearing candidates allowed")
        elif must_have:
            v.risks.append(f"degree must be in hand; you graduate {grad_year}. Eligible only if your results "
                           f"are out before {deadline or 'the closing date'}")
        else:
            v.risks.append(f"notice doesn't say if final-year students ({grad_year}) can apply. Read the eligibility clause")

    # ── branch ─────────────────────────────────────────────────────────────
    listed = [b.lower() for b in rule.get("branches_listed", [])]
    if rule.get("branch_risk"):
        v.risks.append("branch: " + rule["branch_risk"])
    elif listed and not any(w in " ".join(listed) for w in AI_BRANCH_WORDS):
        v.risks.append(f"branch: notice lists {', '.join(listed[:5])}, not AI&ML")
    elif listed:
        v.reasons.append("last notice listed AI / Data Science as eligible specialisations")
    elif re.search(r"computer science|information technology|\bcse\b", t) and not any(w in t for w in AI_BRANCH_WORDS):
        v.risks.append("branch: text names CS/IT but not AI/ML/allied. Check whether AI&ML is accepted")
    elif any(w in t for w in AI_BRANCH_WORDS[:6]):
        v.reasons.append("AI/ML/Data Science named in eligibility")

    # ── marks ──────────────────────────────────────────────────────────────
    if rule.get("min_cgpa") and cgpa:
        (v.reasons if cgpa >= rule["min_cgpa"] else v.blockers).append(
            f"CGPA {cgpa} vs minimum {rule['min_cgpa']}")
    m = re.search(r"(?:minimum|at least|aggregate)[^.%]{0,40}?(\d{2})\s*%", t)
    if m and cgpa:
        need = int(m.group(1))
        c_ = cat.lower()
        relax = (re.search(rf"(?:{c_}\s*/\s*st|\b{c_}\b|reserved categor\w*)[^.%]{{0,80}}?(\d{{2}})\s*%", t) or
                 re.search(rf"(\d{{2}})\s*%[^.%]{{0,30}}(?:for|in case of|in respect of)\s*(?:the\s*)?(?:{c_}\b|{c_}\s*/\s*st)", t)) \
            if cat in ("SC", "ST") else None
        if relax and int(relax.group(1)) < need:
            v.reasons.append(f"relaxed marks for {cat}: {relax.group(1)}% instead of {need}%")
            need = int(relax.group(1))
        pct = edu.get("percent_equivalent")
        if pct:
            (v.reasons if pct >= need else v.blockers).append(f"{pct}% vs minimum {need}%")
        elif need > 60:
            v.risks.append(f"needs {need}% aggregate. Your CGPA {cgpa} is about {(cgpa - 0.75) * 10:.1f}% on the "
                           "common (CGPA−0.75)×10 formula; use REVA's official conversion certificate")

    # ── GATE ───────────────────────────────────────────────────────────────
    gates = profile.get("exams", {}).get("gate", []) or []
    papers = {g.get("paper", "").upper() for g in gates if g.get("status") in ("registered", "appeared", "qualified")}
    need_gate = rule.get("gate_required") or bool(re.search(r"\bgate\b", t))
    if need_gate:
        ok_papers = {p.upper() for p in rule.get("gate_papers", [])}
        if not ok_papers:
            ok_papers = {p for p in ("CS", "DA", "EC") if re.search(rf"\b{p.lower()}\b", t)}
        if not papers:
            v.blockers.append("needs a GATE score: you have none yet")
        elif ok_papers and not (papers & ok_papers):
            v.blockers.append(f"accepts GATE {'/'.join(sorted(ok_papers))}; you have {'/'.join(sorted(papers))}")
        elif ok_papers:
            if cat in ("SC", "ST"):
                v.reasons.append(f"GATE qualifying mark for {cat} ≈ ⅔ of general (DA 2026: 17.5 vs 26.4)")
            have = [g for g in gates if g.get("paper", "").upper() in ok_papers]
            if all(g.get("score") is None for g in have):
                v.risks.append(f"GATE {'/'.join(sorted(papers & ok_papers))} accepted, but your score arrives with the "
                               "results (19 Mar 2027). Check which GATE years this notice accepts")
            else:
                v.reasons.append("GATE paper accepted")

    # ── state extras ───────────────────────────────────────────────────────
    if kind == "state":
        kn = next((l for l in profile.get("languages", []) if l.get("name") == "Kannada"), {})
        if kn.get("studied_upto"):
            v.reasons.append("Kannada studied in school: Kannada language exam normally exempt")
        if profile.get("category_state", "").lower() == "karnataka" and cat in ("SC", "ST"):
            v.reasons.append(f"{cat} reservation applies (Karnataka-issued certificate)")
            if cat == "SC":
                g = profile.get("sc_subgroup")
                if g:
                    v.reasons.append(f"SC internal quota group {g}: count vacancies in that group's column")
                else:
                    v.risks.append("SC posts are split 6/6/5% into groups A/B/C (2025 Act): set sc_subgroup in "
                                   "config/private.yaml and read your group's column in the vacancy table")
    elif cat in ("SC", "ST") and profile.get("caste_certificate"):
        v.reasons.append(f"{cat} reservation (15% central): use the CENTRAL-format certificate")
    if cat in ("SC", "ST") and re.search(r"(?:fee|fees)[^.]{0,80}(?:exempt|nil|no fee)", t):
        v.reasons.append(f"application fee exempt for {cat}")
    return v.finish()
