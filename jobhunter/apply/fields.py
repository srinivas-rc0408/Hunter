"""Field understanding: a form label → which of YOUR facts answers it, and how sure.

Every answer carries a `basis`:
  profile   – straight from config/profile.yaml          (basic → auto-submit OK)
  learned   – an answer you typed yourself once, saved   (basic → auto-submit OK)
  consent   – "I agree to the privacy policy" checkboxes (basic → auto-submit OK)
  drafted   – written by the LLM/template (cover letter)  (NOT basic → waits for you)
  none      – unknown / you left it null                  (NOT basic → waits for you)
A form is auto-submitted only if every required field's basis is basic.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

import yaml

from .. import config
from ..util import iso, norm

BASIC = {"profile", "learned", "consent"}


@dataclass
class Answer:
    key: str
    value: Any
    basis: str               # profile | learned | consent | drafted | none
    note: str = ""
    alts: tuple = ()         # other spellings of the same answer (for dropdowns)

    @property
    def basic(self) -> bool:
        return self.basis in BASIC and self.value not in (None, "")


# Order matters: first match wins. Patterns run on the normalised label.
RULES: list[tuple[str, str]] = [
    ("captcha", r"captcha|i m not a robot|verify you are human"),
    ("otp", r"\botp\b|one time password|verification code"),
    ("diversity", r"\brace\b|ethnic|veteran|disabilit|\bcaste\b|religion|pronoun|sexual orientation|"
                  r"reservation category|social category|community category"),
    ("father_name", r"father|guardian s name|guardian name|parent s name|parent name"),
    ("mother_name", r"mother"),
    ("spouse", r"spouse|husband|wife"),
    ("middle_name", r"middle name"),
    ("first_name", r"first name|given name|\bfname\b|forename"),
    ("last_name", r"last name|surname|family name|\blname\b"),
    ("preferred_name", r"preferred name|nick ?name|known as"),
    ("email_confirm", r"(?:confirm|re ?enter|retype|repeat).{0,10}e ?mail"),
    ("alt_email", r"(?:alternate|alternative|secondary|other) e ?mail"),
    ("alt_phone", r"(?:alternate|alternative|secondary|other|landline|emergency) (?:phone|mobile|contact|number)"),
    ("email", r"e ?mail"),
    ("country_code", r"country code|dial code|isd"),
    ("phone", r"phone|mobile|contact (?:no|number)|whatsapp|\bcell\b"),
    ("dob", r"date of birth|\bdob\b|birth ?date|birthday|born on"),
    ("age", r"^age\b|\byour age\b|current age"),
    ("gender", r"gender|\bsex\b"),
    ("salutation", r"salutation|^title$|prefix"),
    ("marital", r"marital"),
    ("nationality", r"nationality|citizenship"),
    ("linkedin", r"linked ?in"),
    ("github", r"git ?hub"),
    ("portfolio", r"portfolio|website|personal (?:site|url|page)|\bblog\b|other (?:link|url)"),
    ("pct10", r"(?:10th|tenth|class x\b|sslc|matric|secondary school|ssc).{0,30}(?:percent|marks|score|cgpa|grade|%)|"
              r"(?:percent|marks|score|%).{0,20}(?:10th|class x\b|sslc|ssc)"),
    ("pct12", r"(?:12th|twelfth|class xii|\bpuc\b|hsc|higher secondary|intermediate|pre university).{0,30}"
              r"(?:percent|marks|score|grade|%)|(?:percent|marks|%).{0,20}(?:12th|class xii|puc|hsc)"),
    ("year10", r"(?:10th|sslc|class x\b|ssc|icse).{0,20}(?:year|passing)"),
    ("year12", r"(?:12th|puc|class xii|hsc|pre university).{0,20}(?:year|passing)"),
    ("board10", r"(?:10th|sslc|class x\b|ssc|matric).{0,20}board|board.{0,20}(?:10th|sslc|class x\b|ssc)"),
    ("board12", r"(?:12th|puc|class xii|hsc|intermediate).{0,20}board|board.{0,20}(?:12th|puc|class xii|hsc)"),
    ("school10", r"(?:10th|sslc|class x\b|ssc).{0,20}(?:school|institution)|school name"),
    ("school12", r"(?:12th|puc|class xii|hsc|pre university).{0,20}(?:college|school|institution)"),
    ("cgpa", r"cgpa|\bgpa\b|\bcpi\b|grade point|aggregate (?:score|marks)|graduation (?:percent|marks|score)|"
             r"(?:b ?tech|degree|ug|graduation) (?:percentage|marks|cgpa)"),
    ("grad_year", r"(?:graduation|passing|pass ?out|completion|degree end)\s*(?:year|date)|year of (?:graduation|passing)|"
                  r"\bbatch\b|expected graduation|graduating year|when do you graduate"),
    ("backlogs", r"backlog|arrear|\bkt\b|active backlogs"),
    ("college", r"college|university|institut|school name|alma mater"),
    ("degree", r"degree|qualification|education level|highest education|course\b"),
    ("branch", r"branch|speciali[sz]|discipline|\bmajor\b|stream|field of study"),
    ("skill_years", r"(?:years?|how many|how long).{0,40}(?:experience|worked).{0,25}(?:with|in|using|on)\s+\w"),
    ("total_exp", r"(?:total|overall|relevant|work|professional)?\s*(?:years? of )?(?:work |professional |total )?experience"
                  r"(?: in years)?$|years of experience$|^experience$|total experience|how many years of (?:work|professional) experience"),
    ("notice", r"notice period|how soon can you join|earliest (?:joining|start)|when can you (?:join|start)|"
               r"start date|joining date|date available|availability"),
    ("current_ctc", r"current (?:ctc|salary|compensation|package|stipend)|present (?:ctc|salary)|last drawn"),
    ("expected_ctc", r"expected (?:ctc|salary|compensation|package|stipend)|salary expectation|desired (?:salary|pay)|"
                     r"stipend expectation|compensation expectation"),
    ("relocate", r"relocat"),
    ("wfo", r"work(?:ing)? from (?:the )?office|on ?site|in ?office|hybrid|commute|days a week in"),
    ("authorized", r"authori[sz]ed to work|legally (?:eligible|authori[sz]ed|allowed)|right to work|eligible to work|work permit"),
    ("sponsorship", r"sponsor|visa"),
    ("hear", r"how did you (?:hear|find|learn|come)|where did you (?:hear|find)|source of (?:application|referral)|referral source"),
    ("referred", r"referred by|referral (?:name|code|email)|employee referral|who referred"),
    ("prev_employed", r"(?:previously|ever|have you) (?:worked|been employed|employed)|former employee|worked (?:here|for us) before"),
    ("intern_months", r"duration|how many months|internship period|available for .{0,10}months"),
    ("languages", r"languages? (?:known|spoken)|which languages"),
    ("laptop", r"laptop|own (?:a )?computer"),
    ("shifts", r"shift|night|rotational|weekend"),
    ("bond", r"\bbond\b|service agreement"),
    ("perm_pincode", r"(?:permanent|native|home).{0,25}(?:pin ?code|postal|zip)"),
    ("perm_district", r"(?:permanent|native|home).{0,20}district|native place|home ?town|district of (?:domicile|birth|residence)"),
    ("perm_state", r"(?:permanent|native|domicile).{0,20}state|state of domicile"),
    ("district", r"\bdistrict\b"),
    ("address", r"\baddress\b|street"),
    ("pincode", r"pin ?code|postal|\bzip\b|post code"),
    ("city", r"current (?:city|location)|\bcity\b|^location$|where are you (?:based|located)|preferred location|town"),
    ("state", r"^state$|\bstate\b|province|region"),
    ("country", r"^country$|country of residence|\bcountry\b"),
    ("full_name", r"^(?:full |your |legal |candidate s |applicant s )?name\b|full name|name as per"),
    ("cover_letter", r"cover letter|why (?:do you want|are you interested|us|this role|should we)|tell us about yourself|"
                     r"motivation|additional information|anything else"),
    ("resume", r"resume|\bcv\b|curriculum vitae|bio ?data"),
    ("consent", r"\bi agree|consent|privacy|terms|acknowledge|i accept|i confirm|i certify|declaration|i understand"),
    ("marketing", r"newsletter|marketing|job alerts|future opportunities|talent (?:pool|community)|keep me updated"),
]
_COMPILED = [(k, re.compile(p)) for k, p in RULES]
NAME_NOT_PERSON = re.compile(r"company|college|university|school|institut|employer|manager|referr|recruiter|project|"
                             r"organi[sz]ation|course|degree|branch|job|role|position|file|user ?name|reference")


def classify(label: str, ftype: str = "text") -> str:
    lab = norm(re.sub(r"%", " percent ", label or "").replace("Class 10", "class x").replace("Class 12", "class xii")
               .replace("class 10", "class x").replace("class 12", "class xii"))
    if not lab:
        return "unknown"
    if ftype == "file":
        return "cover_letter_file" if "cover" in lab else "resume"
    for key, rx in _COMPILED:
        if rx.search(lab):
            if key in ("full_name", "first_name", "last_name") and NAME_NOT_PERSON.search(lab):
                continue
            if key == "consent" and ftype not in ("checkbox", "radio"):
                continue
            if key == "marketing" and ftype != "checkbox":
                continue
            if key == "cover_letter" and ftype not in ("textarea", "text"):
                continue
            return key
    return "unknown"


# ── answers bank (what you typed yourself) ─────────────────────────────────
def _bank_path():
    return config.data_dir() / "answers.yaml"


def load_bank() -> dict:
    f = _bank_path()
    if not f.is_file():
        return {}
    return yaml.safe_load(f.read_text(encoding="utf-8")) or {}


def learn(label: str, value: Any, site: str = "", override: bool = False) -> None:
    """Save an answer you typed. override=True: you corrected the bot on this exact question,
    so your answer wins over the bot's own choice on every future form."""
    key = norm(label)
    if not key or value in (None, "") or classify(label) in ("captcha", "otp", "cover_letter", "diversity"):
        return
    bank = load_bank()
    old = bank.get(key) or {}
    bank[key] = {"answer": value, "label": label, "site": site, "learned_at": iso(),
                 "override": bool(override or old.get("override"))}
    _bank_path().write_text(yaml.safe_dump(bank, allow_unicode=True, sort_keys=True), encoding="utf-8")


def from_bank(label: str) -> Any:
    bank = load_bank()
    key = norm(label)
    if key in bank:
        return bank[key]["answer"]
    words = set(key.split())
    best, best_sim = None, 0.0
    for k, v in bank.items():
        kw = set(k.split())
        if not kw or not words:
            continue
        sim = len(words & kw) / len(words | kw)
        if sim > best_sim:
            best, best_sim = v["answer"], sim
    return best if best_sim >= 0.85 else None


# ── answering ──────────────────────────────────────────────────────────────
_NEVER_GUESS = {"captcha", "otp", "consent", "marketing", "cover_letter", "resume", "diversity"}
_UNDERSTOOD: dict[str, str] = {}


def _understand(label: str, options: list[str] | None) -> str:
    """The LLM only says WHICH known question this is (one key); the value still comes from your profile.
    Anything else it says, or no reply, leaves the question unknown → asked of you."""
    k = norm(label)
    if k not in _UNDERSTOOD:
        from jobhunter import llm
        keys = sorted({r for r, _ in RULES} - _NEVER_GUESS)
        reply = llm.chat("You classify job-application form questions. Reply with exactly one key from the "
                         "list, or 'unknown' if none clearly fits. Never answer the question.",
                         f"Question: {label}\nOptions: {options or []}\nKeys: {', '.join(keys)}",
                         temperature=0, max_tokens=10) or ""
        reply = reply.strip().strip("'\"`.").lower()
        _UNDERSTOOD[k] = reply if reply in keys else "unknown"
    return _UNDERSTOOD[k]


def _degree_yes_no(label: str, edu: dict) -> str | None:
    """Yes/No about your bachelor's, from its end year in your profile. None = can't tell: ask you."""
    lab, end, now = label.lower(), edu.get("end"), dt.date.today().year
    if not end or not re.search(r"degree|bachelor|b\.?\s?tech|\bb\.?e\b|graduat|undergrad", lab) or \
            re.search(r"master|m\.?\s?tech|\bmba\b|ph\.?\s?d|doctor|diploma|class|\b1[02]th\b|puc", lab):
        return None
    end, m = int(end), re.search(r"\b(20\d\d)\b", lab)
    if m:
        y = int(m[1])
        return "Yes" if (end <= y if re.search(r"\bby\b", lab) else end == y) else "No"
    if re.search(r"pursuing|currently (?:enrolled|studying)", lab):
        return "Yes" if end >= now else "No"
    if re.search(r"complet|graduated|\bhold\b|possess", lab):
        return "Yes" if end < now else "No" if end > now else None
    return None


def _unconfirmed(prof: dict) -> set[str]:
    return set(prof.get("unconfirmed") or [])


def _date_fmt(dob: dt.date, placeholder: str, ftype: str) -> str:
    if ftype == "date":
        return dob.isoformat()
    p = (placeholder or "").lower()
    if re.search(r"mm\W*dd\W*yyyy", p):
        return dob.strftime("%m/%d/%Y")
    if re.search(r"yyyy\W*mm\W*dd", p):
        return dob.isoformat()
    sep = "-" if "-" in p else "/"
    return dob.strftime(f"%d{sep}%m{sep}%Y")      # Indian default DD/MM/YYYY


def answer(label: str, ftype: str = "text", placeholder: str = "", options: list[str] | None = None,
           job: dict | None = None) -> Answer:
    prof = config.cfg("profile")
    a = prof.get("answers", {}) or {}
    loc = prof.get("location", {}) or {}
    links = prof.get("links", {}) or {}
    edu = next((e for e in prof.get("education", []) if e.get("level") == "bachelors"), {})
    e12 = next((e for e in prof.get("education", []) if e.get("level") == "class12"), {})
    e10 = next((e for e in prof.get("education", []) if e.get("level") == "class10"), {})
    unconf = _unconfirmed(prof)
    key = classify(label, ftype)

    def P(v, note=""):
        return Answer(key, v, "profile" if v not in (None, "") else "none", note)

    if key not in ("captcha", "otp", "diversity", "resume", "cover_letter", "cover_letter_file", "marketing"):
        fix = load_bank().get(norm(label)) or {}
        if fix.get("override") and fix.get("answer") not in (None, ""):
            return Answer(key, fix["answer"], "learned", "you corrected this before")

    if key == "unknown" and from_bank(label) in (None, ""):
        key = _understand(label, options)

    if key in ("degree", "unknown") and options and pick_option("Yes", options) and pick_option("No", options):
        v = _degree_yes_no(label, edu)
        if v or key == "degree":             # a Yes/No degree question never gets the degree's name
            return P(v, "from your graduation year" if v else "degree question: your call once")
    if key == "unknown":
        v = from_bank(label)
        return Answer(key, v, "learned" if v not in (None, "") else "none")
    if key in ("captcha", "otp"):
        return Answer(key, None, "none", "needs a human")
    if key == "diversity":
        v = (prof.get("voluntary") or {}).get("diversity_category")
        return Answer(key, v, "profile" if v else "none", "voluntary: left for you")
    if key == "first_name":
        return P(prof.get("first_name"))
    if key == "last_name":
        return P(prof.get("last_name"))
    if key == "middle_name":
        return Answer(key, "", "profile", "none")          # blank is the true answer
    if key in ("full_name",):
        return P(prof.get("name"))
    if key == "preferred_name":
        return P(prof.get("preferred_name"))
    if key == "father_name":
        return P(prof.get("father_name"))
    if key == "mother_name":
        return P(prof.get("mother_name"))
    if key == "spouse":
        return Answer(key, None, "none")
    if key in ("email", "email_confirm"):
        return P(prof.get("email"))
    if key == "alt_email":
        return P(prof.get("alt_email"))
    if key == "alt_phone":
        return Answer(key, None, "none", "no alternate number given")
    if key == "country_code":
        cc = str(prof.get("country_code", "+91"))
        land = loc.get("country") or ("India" if cc.endswith("91") else "")
        return Answer(key, cc, "profile", "", alts=tuple(x for x in (f"{land} ({cc})", f"{land} {cc}", land,
                                                                    cc.lstrip("+")) if x.strip()))
    if key == "phone":
        plain = ftype in ("tel", "number") or "10" in placeholder or len(placeholder.replace(" ", "")) == 10
        return P(prof.get("phone_digits") if plain or not placeholder.startswith("+") else prof.get("phone"))
    if key == "dob":
        dob = prof.get("date_of_birth")
        if not dob:
            return P(None)
        dob = dob if isinstance(dob, dt.date) else dt.date.fromisoformat(str(dob))
        return P(_date_fmt(dob, placeholder, ftype))
    if key == "age":
        dob = prof.get("date_of_birth")
        if not dob:
            return P(None)
        dob = dob if isinstance(dob, dt.date) else dt.date.fromisoformat(str(dob))
        t = dt.date.today()
        return P(str(t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day))))
    if key == "gender":
        return P(prof.get("gender"))
    if key == "salutation":
        g = (prof.get("gender") or "").lower()
        return P("Mr." if g == "male" else ("Ms." if g == "female" else None))
    if key == "marital":
        return P(prof.get("marital_status"))
    if key == "nationality":
        return P(prof.get("nationality"))
    if key in ("linkedin", "github", "portfolio"):
        return P(links.get(key))
    if key == "pct10":
        return Answer(key, e10.get("percent"), "profile" if e10.get("percent") else "none")
    if key == "pct12":
        return Answer(key, e12.get("percent"), "profile" if e12.get("percent") else "none")
    if key == "board10":
        r_ = P(e10.get("board"))
        r_.alts = ("ICSE", "CISCE", "Council for the Indian School Certificate Examinations", "Other")
        return r_
    if key == "board12":
        r_ = P(e12.get("board"))
        r_.alts = ("Karnataka PUC", "Karnataka PU Board", "PUC", "Karnataka State Board", "State Board",
                   "Department of Pre-University Education", "KSEAB", "Other")
        return r_
    if key == "school10":
        return P(e10.get("institution"))
    if key == "school12":
        return P(e12.get("institution"))
    if key == "year10":
        v = e10.get("year")
        return Answer(key, v, "none" if "education.2.year" in unconf else "profile",
                      "unconfirmed" if "education.2.year" in unconf else "")
    if key == "year12":
        v = e12.get("year")
        return Answer(key, v, "none" if "education.1.year" in unconf else "profile",
                      "unconfirmed" if "education.1.year" in unconf else "")
    if key == "cgpa":
        lab = norm(label)
        if "percent" in lab or "%" in label:
            v = edu.get("percent_equivalent")
            return Answer(key, v, "profile" if v else "none", "percentage asked; set percent_equivalent")
        return P(edu.get("cgpa"))
    if key == "grad_year":
        if ftype == "date" or re.search(r"mm|month", placeholder.lower()):
            return Answer(key, None, "none", "needs a month")
        return P(str(edu.get("end")) if edu.get("end") else None)
    if key == "backlogs":
        return P(a.get("backlogs"))
    if key == "college":
        return P(edu.get("institution"))
    if key == "degree":
        r_ = P(edu.get("degree"))
        r_.alts = ("B.E./B.Tech", "BE/B.Tech", "Bachelor of Technology", "B.Tech/B.E.", "Bachelor of Engineering")
        return r_
    if key == "branch":
        r_ = P(edu.get("branch"))
        r_.alts = ("AI & ML", "AI&ML", "AIML", "Artificial Intelligence & Machine Learning", "AI and ML",
                   "Computer Science (AI & ML)", "CSE (AI & ML)", "CSE (AIML)")
        return r_
    if key == "total_exp":
        return P(a.get("total_experience_years", "0"))
    if key == "skill_years":
        v = from_bank(label)
        return Answer(key, v, "learned" if v not in (None, "") else "none", "per-skill years: your call once")
    if key == "notice":
        if ftype == "date":
            return P(dt.date.today().isoformat())
        if ftype == "number":
            return P(a.get("notice_period_days"))
        r_ = P(a.get("notice_period"))
        r_.alts = tuple(x for x in (a.get("earliest_joining"),) if x)   # shorter wording, for tight fields
        return r_
    if key == "current_ctc":
        return P(a.get("current_ctc") if ftype == "number" or not options else a.get("current_ctc_text"))
    if key == "expected_ctc":
        lab = norm(label)
        intern = "stipend" in lab or "intern" in norm((job or {}).get("title", "")) or \
            (job or {}).get("job_type") == "internship"
        if intern:
            v = a.get("expected_stipend_month")
            if v is None:
                return P(None)
            return P(str(v) if ftype == "number" or options else f"{v} per month")
        lpa = a.get("expected_ctc_lpa")
        if lpa is None:
            return P(None)
        if re.search(r"\blpa\b|lakh|lac\b|in l\b", lab) or (ftype == "number" and "lpa" in placeholder.lower()):
            return P(str(lpa))
        if ftype == "number" or re.search(r"\binr\b|rupees|annual", lab):
            return P(str(int(float(lpa) * 100000)))
        if options:
            return P(str(lpa))          # dropdown ranges are almost always in LPA
        return P(f"{lpa} LPA")
    if key == "relocate":
        return P(a.get("willing_to_relocate"))
    if key == "wfo":
        return P(a.get("work_from_office"))
    if key == "authorized":
        return P(a.get("authorized_to_work_in_india"))
    if key == "sponsorship":
        return P(a.get("sponsorship_required"))
    if key == "hear":
        return P(a.get("how_did_you_hear"))
    if key == "referred":
        return Answer(key, "", "profile")                  # no referral is the true answer
    if key == "prev_employed":
        return P(a.get("previously_employed_here"))
    if key == "intern_months":
        return P(a.get("available_for_internship_months"))
    if key == "languages":
        return P(", ".join(l["name"] for l in prof.get("languages", []) if l.get("speak")))
    if key == "laptop":
        return P(a.get("have_laptop"))
    if key == "shifts":
        return P(a.get("open_to_shifts"))
    if key == "bond":
        return P(a.get("bond_ok"))
    if key == "address":
        return P(loc.get("address"))
    if key == "pincode":
        return P(loc.get("pincode"))
    perm = loc.get("permanent") or {}
    if key == "perm_pincode":
        return P(perm.get("pincode"))
    if key == "perm_district":
        a_ = P(perm.get("district"))
        a_.alts = tuple(perm.get("district_aliases") or ())
        return a_
    if key == "perm_state":
        return P(perm.get("state"))
    if key == "district":
        return P(loc.get("district"))
    if key == "city":
        return P(loc.get("city"))
    if key == "state":
        return P(loc.get("state"))
    if key == "country":
        return P(loc.get("country"))
    if key == "consent":
        return Answer(key, True, "consent")
    if key == "marketing":
        return Answer(key, False, "consent", "left unticked")
    if key == "cover_letter":
        return Answer(key, None, "drafted")                 # filled later by tailor (not basic)
    if key in ("resume", "cover_letter_file"):
        return Answer(key, None, "profile")                 # path supplied by the filler
    return Answer(key, None, "none")


# ── matching an answer to a <select>/radio option ──────────────────────────
SYN = {
    "yes": ["yes", "y", "true", "i am", "i do", "i will", "willing", "agree", "available"],
    "no": ["no", "n", "false", "i am not", "i do not", "not required", "don t"],
    "b.tech": ["b tech", "btech", "bachelor of technology", "b e b tech", "be b tech", "b e", "bachelor of engineering",
               "bachelors", "bachelor s degree", "bachelor", "undergraduate", "ug", "graduate"],
    "artificial intelligence and machine learning": ["artificial intelligence and machine learning",
               "artificial intelligence machine learning", "ai ml", "aiml", "ai and ml", "ai amp ml",
               "computer science and engineering artificial intelligence", "cse ai ml", "artificial intelligence"],
    "male": ["male", "man", "m"], "female": ["female", "woman", "f"],
    "india": ["india", "in", "+91", "india +91", "india 91"],
    "immediate (available now)": ["immediate", "immediately", "0 days", "15 days or less", "less than 15 days",
                                  "serving notice", "0", "available now"],
    "linkedin": ["linkedin", "linked in", "job board", "online", "social media"],
    "karnataka": ["karnataka", "ka"], "bengaluru": ["bengaluru", "bangalore", "bengaluru bangalore"],
    "reva university, bengaluru": ["reva university", "reva"],
}


SYN_N = {norm(k): v for k, v in SYN.items()}

# synonyms that are only safe as a whole-option match ("Artificial Intelligence"
# must never pick "Artificial Intelligence and Data Science")
EXACT_ONLY = {"artificial intelligence", "bachelor", "bachelors", "graduate", "undergraduate", "online",
              "social media", "job board", "available", "man", "woman", "reva", "computer science and engineering artificial intelligence"}


def pick_option(value: Any, options: list[str]) -> str | None:
    """Choose the option meaning `value`, or None if no confident match."""
    if value is None or not options:
        return None
    v = norm(str(value))
    if isinstance(value, bool):
        v = "yes" if value else "no"
    opts = [(o, norm(o)) for o in options if norm(o) not in ("", "select", "select one", "please select", "choose",
                                                               "select an option", "none selected")]
    for o, n in opts:
        if n == v:
            return o
    if v in ("0", "none", "nil", "not applicable") and any(n in ("no", "yes") for _, n in opts):
        v = "no"
    cands = [norm(c) for c in SYN_N.get(v, [])] + [v]
    for c in cands:
        for o, n in opts:
            if n == c:
                return o
    for c in cands:
        if len(c) < 3 or c in EXACT_ONLY:
            continue
        for o, n in opts:
            if re.search(rf"\b{re.escape(c)}\b", n):
                return o
    if re.fullmatch(r"\d+(\.\d+)?", v):          # numeric value into ranges like "0-1", "8.0 - 8.5"
        x = float(v)
        for o, n in opts:
            m = re.match(r"^(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)", n.replace(" ", " ").replace("  ", " "))
            nums = [float(t) for t in re.findall(r"\d+(?:\.\d+)?", o)]
            if len(nums) >= 2 and nums[0] <= x <= nums[1]:
                return o
            if m and float(m.group(1)) <= x <= float(m.group(2)):
                return o
        for o, n in opts:
            if re.fullmatch(r"\d+(\.\d+)?", n) and float(n) == x:
                return o
    return None
