"""Grounding: a model-written sentence survives only if its facts are in what the tools returned.

check(reply, sources) finds numbers (₹, LPA, %, years), dates, skills and company names in each sentence of the
reply; a sentence with one that no source contains becomes "not found". Numbers match loosely
(12,000 = 12000 = 12k; 3 LPA = 3 lakh = 3,00,000), dates by day and month (2026-11-30 = 30 Nov).
"""
from __future__ import annotations

import re

from . import config

GONE = "not found"
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
DATES = [(re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b"), lambda m: (int(m[2]), int(m[3]))),
         (re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](20\d\d)\b"), lambda m: (int(m[2]), int(m[1]))),    # Indian DD/MM/YYYY
         (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)? (?:of )?{MON}(?:,? 20\d\d)?", re.I), lambda m: (MONTHS[m[2].lower()], int(m[1]))),
         (re.compile(rf"\b{MON} (\d{{1,2}})(?:st|nd|rd|th)?\b(?:,? 20\d\d)?", re.I), lambda m: (MONTHS[m[1].lower()], int(m[2])))]
NUM = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(k\b|lpa\b|lakhs?\b|lacs?\b|l\b|crores?\b|cr\b)?(?![\w])", re.I)
UNIT = {"k": 1e3, "l": 1e5, "lpa": 1e5, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "cr": 1e7, "crore": 1e7, "crores": 1e7}
KNOWN = {"zoho", "infosys", "tcs", "wipro", "hcl", "accenture", "cognizant", "capgemini", "deloitte", "ibm", "google",
         "microsoft", "amazon", "flipkart", "swiggy", "zomato", "meesho", "razorpay", "phonepe", "paytm", "freshworks",
         "nvidia", "intel", "oracle", "sap", "adobe", "salesforce", "uber", "ola", "byju's", "tech mahindra", "mphasis",
         "birlasoft", "ltimindtree", "mindtree", "persistent", "juspay", "cred", "sarvam", "krutrim", "fractal"}
LEAD = {"the", "this", "that", "these", "your", "you", "it", "its", "is", "a", "an", "in", "at", "for", "and", "but",
        "yes", "no", "i", "my", "we", "they", "if", "so", "also", "from", "with", "as", "on", "to", "of"}
CAPS = re.compile(r"\b[A-Z][\w&'.-]*(?: (?:[A-Z][\w&'.-]*|&|of))* [A-Z][\w&'.-]*\b")


def _dates(text: str) -> tuple[set, str]:
    """(month, day) of every date, and the text with the dates blanked (so '30 Nov' isn't also a number)."""
    out = set()
    for rx, md in DATES:
        for m in rx.finditer(text):
            try:
                out.add(md(m))
            except (KeyError, ValueError):
                pass
        text = rx.sub(" ", text)
    return out, text


def _nums(text: str) -> set[float]:
    out = set()
    for m in NUM.finditer(text):
        try:
            v = float(m[1].replace(",", ""))
        except ValueError:
            continue
        out.add(v * UNIT.get((m[2] or "").lower(), 1))
    return out


def _skills(text: str) -> set[str]:
    from .tailor import keywords
    return {k.lower() for k in keywords(text)}


def _companies(text: str) -> set[str]:
    """Known company names, and capitalised multi-word names ('Gen Labs', 'Acme AI Labs')."""
    low = text.lower()
    known = {*KNOWN, *(d.lower() for d in config.cfg("profile").get("dream_companies") or [])}
    out = {k for k in known if re.search(rf"(?<![\w]){re.escape(k)}(?![\w])", low)}
    for m in CAPS.finditer(text):
        words = m[0].split()
        while words and words[0].lower() in LEAD:
            words = words[1:]
        if len(words) >= 2:
            out.add(" ".join(words).lower())
    return out


def check(reply: str, sources: list[str]) -> str:
    """The reply with every sentence that states an unsourced number, date, skill or company replaced."""
    src = "\n".join(str(s) for s in sources or [])
    src = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), src)     # JSON's ₹ → ₹ (else '20b912,000')
    sdates, rest = _dates(src)
    snums, low = _nums(rest) | _nums(src), src.lower()
    sskills = _skills(src)
    parts = re.split(r"((?<=[.!?])\s+|\n+)", reply or "")
    out = []
    for i, s in enumerate(parts):
        if i % 2 or not s.strip():
            out.append(s)
            continue
        d, plain = _dates(s)
        bad = (d - sdates or _nums(plain) - snums or _skills(s) - sskills
               or {c for c in _companies(s) if c not in low})
        out.append(GONE + ("." if s.rstrip()[-1:] in ".!?" else "") if bad else s)
    return "".join(out)
