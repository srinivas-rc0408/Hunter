"""Read a govt recruitment notification (page or PDF text, English or Kannada) into card fields.

Every value is read from the text. Anything that can't be read exactly stays None, and the card
says "see notification": no guesses. Checked against real notices (tests/fixtures/govt/).
"""
from __future__ import annotations

import re

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE = r"(\d{1,2})(?:st|nd|rd|th)?[.\-/\s]+(\d{1,2}|[A-Za-z]{3,9})[.\-/,\s]+(\d{4})"
KA_PLACES = ["Bengaluru", "Bangalore", "Mysuru", "Mysore", "Mangaluru", "Mangalore", "Hubballi", "Hubli", "Dharwad",
             "Belagavi", "Belgaum", "Kalaburagi", "Gulbarga", "Ballari", "Bellary", "Vijayapura", "Shivamogga",
             "Tumakuru", "Udupi", "Hassan", "Mandya", "Davanagere", "Raichur", "Bidar", "Karwar", "Chitradurga",
             "Kolar", "Chikkamagaluru", "Kodagu", "Madikeri", "Chamarajanagar", "Yadgir", "Koppal",
             "Gadag", "Haveri", "Bagalkot", "Chikkaballapur", "Karnataka"]


def _ka_places() -> list[str]:
    """The district list plus your own town from private.yaml (kept out of this public file)."""
    from .. import config
    loc = config.cfg("profile").get("location") or {}
    mine = [v for v in (loc.get("city"), loc.get("district"), loc.get("home_district")) if isinstance(v, str) and v and v not in KA_PLACES]
    return KA_PLACES[:-1] + mine + ["Karnataka"]
CATS = {"UR": "UR", "GEN": "UR", "GENERAL": "UR", "SC": "SC", "ST": "ST", "OBC": "OBC", "EWS": "EWS", "PWD": "PWD",
        "PWBD": "PWD"}
SC_WORDS = r"(?:\bSC\b(?!\s*/\s*ST)|\bS\.C\.|ಪ\.ಜಾ\.|ಪರಿಶಿಷ್ಟ\s*ಜಾತಿ)"


def _norm(text: str) -> str:
    """OCR'd Kannada writes the anusvara as the digit ೦ ("ದಿನಾ೦ಕ"); Kannada numerals → 0-9."""
    t = re.sub(r"(?<=[ಀ-೿])೦|೦(?=[ಀ-೥೰-೿])", "ಂ", text or "")
    return t.translate(str.maketrans("೦೧೨೩೪೫೬೭೮೯", "0123456789"))


def iso(d: str, mo: str, y: str) -> str | None:
    m = int(mo) if mo.isdigit() else MONTHS.get(mo[:3].lower())
    try:
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}" if m and 1 <= int(m) <= 12 and 1 <= int(d) <= 31 else None
    except (TypeError, ValueError):
        return None


def _date(t: str, pats: list[str]) -> str | None:
    for p in pats:
        for m in re.finditer(p + DATE, t, re.I):
            if " ".join(t[max(0, m.start() - 20):m.start()].split()).lower().endswith(("from the", "before the")):
                continue                    # "one year from the last date … i.e. 20.07.2027" is not the last date
            v = iso(*m.groups()[-3:])
            if v:
                return v
    return None


def _money(s: str) -> int:
    return int(s.replace(",", ""))


def _inr(n: int) -> str:
    s = str(n)
    return s if len(s) <= 3 else re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", s[:-3]) + "," + s[-3:]


def pay(t: str) -> str | None:
    """'Level 10, ₹56,100–1,77,500' exactly as listed: pay levels and pay-scale ranges found in the text."""
    levels = list(dict.fromkeys(re.findall(r"\b(?:pay\s*)?level[\s\-]*(\d{1,2})\b", t, re.I)))
    scales = []
    for a, b in re.findall(r"(?:Rs\.?|₹|INR|ರೂ\.?)?\s*(\d{1,3}(?:,\d{2,3})+|\d{4,6})\s*(?:/-)?\s*(?:-|–|to)\s*(?:Rs\.?|₹)?\s*"
                           r"(\d{1,3}(?:,\d{2,3})+|\d{4,6})(?![\d,]*\s*(?:years|yrs|ವರ್ಷ))", t):
        lo, hi = _money(a), _money(b)
        if 10000 <= lo < hi <= 400000 and 1.3 <= hi / lo <= 4.5:      # a pay scale, not a year span or phone no.
            s = f"₹{_inr(lo)}–{_inr(hi)}"
            if s not in scales:
                scales.append(s)
    basic = list(dict.fromkeys(_money(x) for x in re.findall(r"basic\s*pay\s*[:\-]?\s*(?:Rs\.?|₹)\s*([\d,]{5,9})", t, re.I)))
    parts = ([f"Level {', '.join(levels)}"] if levels else []) + scales[:3]
    if basic and not scales:
        parts.append("basic " + " / ".join(f"₹{_inr(b)}" for b in basic[:3]))
    return ", ".join(parts) or None


def vacancies(t: str) -> tuple[int | None, int | None]:
    """(total, SC) from 'Total 33', 'ಒಟ್ಟು ಹುದ್ದೆಗಳ ಸಂಖ್ಯೆ 06', or a category table (columns SC ST OBC EWS UR,
    or rows 'ಪ.ಜಾ. 01 01'). Table rows must add up, or the number isn't used."""
    total = sc = None
    lines = t.splitlines()
    for i, line in enumerate(lines):                     # form A: a header row of categories, number rows below
        heads = [CATS[w.upper()] for w in re.findall(r"\b(UR|GEN(?:ERAL)?|SC|ST|OBC|EWS|PWB?D)\b", line, re.I)]
        if len(heads) < 3 or "SC" not in heads:
            continue
        tot = s = 0
        for row in lines[i + 1:i + 30]:
            nums = [int(x) for x in re.findall(r"(?<![\d.])(\d{1,4})(?![\d.%])", row)]
            if len(nums) >= len(heads) + 1:
                n, cats = nums[-len(heads) - 1], nums[-len(heads):]
                if sum(cats) == n:                      # "Fitter 17 2 1 4 1 9": 2+1+4+1+9 = 17
                    tot, s = tot + n, s + cats[heads.index("SC")]
        if tot:
            total, sc = tot, s
            break
    if sc is None:                                      # form B: category rows, last number = that row's total
        groups = re.findall(SC_WORDS + r"\s*((?:(?:\d{1,4}|-)\s+){0,6}\d{1,4})(?=\s|$)", t)
        if groups:
            sc = sum(int(g.split()[-1]) for g in groups)
    m = (re.search(r"\btotal\s*(?:no\.?\s*of\s*)?(?:posts|vacancies)?\s*[:\-]?\s*(\d{1,5})\b(?!\s*(?:marks|%))", t, re.I)
         or re.search(r"ಒಟ್ಟು\s*ಹುದ್ದೆಗಳ\s*ಸಂಖ್ಯೆ\s*[:\-]?\s*(\d{1,5})", t)
         or re.search(r"\b(\d{1,5})\s*(?:posts|vacancies)\b", t, re.I))
    if m and int(m.group(1)) > 0:
        total = int(m.group(1))
    return total, sc


def fee_sc(t: str) -> str | None:
    """The SC application fee as written: '₹0 (exempt)', '₹500', or None."""
    for m in re.finditer(r"(?:fee|ಶುಲ್ಕ)", t, re.I):
        w = t[max(0, m.start() - 250):m.start() + 700]
        if re.search(r"(?:not\s+applicable\s+for|exempted?\s+for|no\s+fee\s+for)\s+(?:the\s+)?(?:sc\s*/\s*st|scheduled\s*caste)", w, re.I):
            return "₹0 (exempt)"            # "GEN/EWS/OBC (not applicable for SC/ST) pay a fee of Rs.200"
        w = t[m.start():m.start() + 700]
        e = re.search(r"(?:sc\s*/\s*st|scheduled\s*caste|ಪರಿಶಿಷ್ಟ\s*ಜಾತಿ)[^.\n]{0,90}?(exempt\w*|nil\b|no\s+fee|not\s+required\s+to\s+pay|"
                      r"(?:rs\.?|₹|ರೂ\.?)\s*([\d,]+))", w, re.I) or \
            re.search(r"(exempt\w*|nil\b|no\s+fee)[^.\n]{0,60}(?:sc\s*/\s*st|scheduled\s*caste)", w, re.I)
        if e:
            amt = e.groups()[1] if len(e.groups()) > 1 else None        # the amount group, when the match has one
            return f"₹{_inr(_money(amt))}" if amt and _money(amt) else "₹0 (exempt)"
    return None


def ages(t: str) -> tuple[int | None, int | None]:
    """(general upper age, SC upper age) when the notice states them once, else None."""
    gen = re.search(r"(?:upper\s+)?age\s*limit[^0-9\n]{0,40}(\d{2})\s*years|maximum\s+age\s*(?:limit)?\s*(?:is|of|:)?\s*(\d{2})\s*years|"
                    r"ಸಾಮಾನ್ಯ\s*ಅರ್ಹತೆ[^\n]{0,40}?(\d{2})\s*ವರ್ಷ", t, re.I)
    sc = re.search(r"(?:sc\s*/\s*st|scheduled\s*caste)[^.\n]{0,60}?(?:upper\s+age|age)[^.\n]{0,30}?(\d{2})\s*years|"
                   r"ಪರಿಶಿಷ್ಟ\s*ಜಾತಿ[^\n]{0,80}?(\d{2})\s*ವರ್ಷ", t, re.I)
    val = lambda m: next((int(x) for x in m.groups() if x), None) if m else None   # noqa: E731
    g, s = val(gen), val(sc)
    return (g if g and 18 <= g <= 60 else None), (s if s and 18 <= s <= 65 else None)


def read(text: str, title: str = "", kind: str = "central") -> dict:
    t = _norm(text)
    released = (_date(t[:1500], [r"(?:advt\.?|advertisement)\s*(?:no\.?)?[^\n]{0,80}?dated\s*[:\-]?\s*",
                                 r"(?:^|\n)\s*dated?\s*[:\-]\s*", r"ದಿನಾಂಕ\s*[:\-]\s*"])
                or _date(t, [r"(?:^|\n)\s*dated?\s*[:\-]\s*", r"\bw\.e\.f\.?\s*", r"date\s+of\s+(?:issue|publication|notification|release)[^0-9\n]{0,15}",
                             r"(?:advt\.?|advertisement)\s*(?:no\.?)?[^\n]{0,80}?dated\s*[:\-]?\s*"]))
    last = _date(t, [r"(?:last|closing)\s*date[^0-9\n]{0,60}?", r"ಕ[ೊೂ]ನೆಯ\s*ದಿನಾಂಕ\s*[:\-]?\s*",
                     r"(?:registration|application|site)[^\n]{0,60}?\btill\s+(?:\d{1,2}[.:]\d{2}\s*(?:hrs|hours)?\s*(?:on\s+)?)?",
                     r"(?:on\s+or\s+before|not\s+later\s+than)[^0-9\n]{0,20}"])
    exam = _date(t, [r"(?:date\s+of\s+(?:the\s+)?(?:written\s+|online\s+|computer[\s\-]+based\s+)?(?:exam(?:ination)?|test|cbt)|"
                     r"exam(?:ination)?\s+date|(?:cbt|written\s+test)\s+(?:on|date))[^0-9\n]{0,30}", r"ಪರೀಕ್ಷೆಯ?\s*ದಿನಾಂಕ\s*[:\-]?\s*"])
    cutoff = _date(t, [r"\bas\s+on\s+(?:the\s+)?"])
    if not cutoff and (re.search(r"as\s+on\s+(?:the\s+)?(?:closing|last)\s+date", t, re.I) or
                       re.search(r"ಕ[ೊೂ]ನೆಯ\s*ದಿನಾಂಕದಂದು[^\n]{0,60}\d{2}\s*ವರ್ಷ", t)):
        cutoff = last
    total, sc = vacancies(t)
    age_gen, age_sc = ages(t)
    post = re.search(r"(?:for\s+the\s+(?:following\s+)?posts?\s+of|recruitment\s+(?:to|for)\s+the\s+posts?\s+of)\s*([^\n.;:]{3,100})", t, re.I)
    dept = re.search(r"((?:Ministry|Department)\s+of\s+[A-Z][A-Za-z&, ]{2,60}?)\s*(?:\n|\(|,|$)", t[:500])   # letterhead only
    place = re.search(r"(?:place\s+of\s+posting|job\s+location|\bposting)\s*[:\-]\s*([^\n]{3,140})", t, re.I)
    place_txt = place.group(1).strip(" .,;") if place else None
    where = t[place.start():place.start() + 200] if place else (t if kind == "state" else "")   # a list may wrap lines
    places = _ka_places()
    ka = sorted({p for p in places if re.search(rf"\b{p}\b", where, re.I)}, key=places.index)
    urls = [("https://" + u if u.startswith("www.") else u).rstrip(".,;)") for u in re.findall(r"(?:https?://|\bwww\.)[^\s)>\]\"'“”]+", t)]
    near = [u for u in urls if re.search(r"(?:apply|online|registration|website|career|ವೆಬ್)[^\n]{0,80}" + re.escape(u.removeprefix("https://") if "www." in u else u), t, re.I)]
    return {"post": post.group(1).strip() if post else (title or None), "department": dept.group(1).strip() if dept else None,
            "place": place_txt, "karnataka_places": ka, "pay": pay(t), "vacancies": total, "sc_vacancies": sc,
            "age_limit": age_gen, "age_limit_sc": age_sc, "age_cutoff": cutoff, "fee_sc": fee_sc(t),
            "released": released, "last_date": last, "exam_date": exam, "apply_url": (near or urls or [None])[0]}
