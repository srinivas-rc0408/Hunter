"""How the chat assistant reads text without the model: the fast-path rules (regexes, typo and Kannada-English
normalising, sheet edits), the search vocabulary, your skills and a posting's role summary.
assistant.py re-exports every name here."""
from __future__ import annotations

import re

from . import config
from .db import DB


# ── search vocabulary (also used by the search tools) ──────────────────────
ALIASES = {"ml": ["ml", "machine learning"], "ai": ["ai", "artificial intelligence"],
           "genai": ["genai", "gen ai", "generative ai", "llm", "large language"], "llm": ["llm", "large language"],
           "nlp": ["nlp", "natural language"], "cv": ["computer vision", "cv"], "ds": ["data science", "data scientist"],
           "intern": ["intern", "internship"], "fresher": ["fresher", "entry level", "graduate", "0-1", "0 - 1"]}
STOP = {"job", "jobs", "role", "roles", "opening", "openings", "position", "positions", "in", "at", "for", "the",
        "a", "an", "of", "and", "posted", "this", "week", "today", "new", "any", "find", "me", "show", "near",
        "with", "companies", "company", "product", "best", "top"}
KARNATAKA = ["karnataka", "bengaluru", "bangalore", "mysuru", "mysore", "mangaluru", "mangalore", "hubli",
             "hubballi", "belagavi", "belgaum", "udupi", "manipal", "davangere", "shivamogga", "tumakuru"]
CITY = {"bengaluru": ["bengaluru", "bangalore"], "bangalore": ["bengaluru", "bangalore"], "mysuru": ["mysuru", "mysore"],
        "mysore": ["mysuru", "mysore"], "mangaluru": ["mangaluru", "mangalore"], "karnataka": KARNATAKA}


FAST = [
    (re.compile(r"^(?:what|which) (?:jobs? )?(?:did|have) i appl(?:y|ied)(?: to| for)?(?: so far)?\s*(this week|today|this month)?\??$|"
                r"^(?:my )?(?:applications?|applied)(?: to)? (this week|today|this month)\??$", re.I),
     lambda m: ("applied", {"filter": (m.group(1) or m.group(2) or "").replace("this ", "")})),
    (re.compile(r"^(?:please )?(?:send|give|share|show)(?: me)? (?:the |my )?(?:excel |tracker )?(?:sheet|tracker|excel)\.?$", re.I),
     lambda m: ("sheet_send", {})),
    (re.compile(r"^(?:any |are there )?(?:any )?govt?(?:ernment)? (?:jobs?|posts?|notices?) (?:closing|ending|with deadlines?) "
                r"(?:soon|this week)\??$", re.I),
     lambda m: ("govt", {"closing_within_days": 7})),
    (re.compile(r"^mark ([0-9a-f]{6,16}) (?:as )?(\w+)\.?$", re.I), lambda m: ("set_status", {"id": m.group(1), "status": m.group(2)})),
    (re.compile(r"^(?:i )?(?:have )?applied (?:to |for )?([0-9a-f]{6,16})\.?$|^([0-9a-f]{6,16}) (?:ge )?applied\.?$", re.I),
     lambda m: ("set_status", {"id": m.group(1) or m.group(2), "status": "applied"})),
    (re.compile(r"^(?:i )?got (?:an )?interview(?: call)? (?:for|from|at|with) ([0-9a-f]{6,16})\W*$", re.I),
     lambda m: ("set_status", {"id": m.group(1), "status": "interview"})),
    (re.compile(r"^([0-9a-f]{6,16}) rejected me\W*$|^(?:i )?got rejected (?:by|from|at) ([0-9a-f]{6,16})\W*$", re.I),
     lambda m: ("set_status", {"id": m.group(1) or m.group(2), "status": "rejected"})),
    (re.compile(r"^(?:skip|hide|not interested in) ([0-9a-f]{6,16})\.?$", re.I), lambda m: ("skip", {"id": m.group(1)})),
    (re.compile(r"^(?:show )?(?:my )?(?:stats|numbers|progress)\??$|^how am i doing\??$", re.I), lambda m: ("stats", {})),
    (re.compile(r"^(?:is|would) ([0-9a-f]{6,16}) (?:be )?a (?:good |strong )?(?:fit|match)(?: for me)?\??$|"
                r"^(?:explain|how do i fit|do i fit|fit for|fit|am i (?:a )?fit for|how well do i (?:fit|match)|"
                r"should i apply (?:to|for)) ([0-9a-f]{6,16})\s*\??$", re.I),
     lambda m: ("explain_job", {"id": m.group(1) or m.group(2)})),
    (re.compile(r"^what (?:does|do|would) (?:the )?(.+?) (?:job|role|posting|one|opening) (?:need|want|ask for|require)"
                r"(?: that)? i (?:don'?t|do not|dont) have\??$|"
                r"^what (?:am i|i'?m) (?:missing|lacking) for (?:the )?(.+?)(?: job| role)?\??$", re.I),
     lambda m: ("explain_job", {"id": m.group(1) or m.group(2)})),
]
SALARY = re.compile(r"^(?:roughly |approximately |about )?how much (?:would|does|do|will|can|might) (?:a |an )?"
                    r"(fresher |freshers |intern |entry[- ]level |new grad )?(.+?) (?:get|earn|make|be paid)(?: paid)?"
                    r"(?: as a fresher)? (?:at|in|with) (.+?)\??$|"
                    r"^(?:what(?:'s| is) the )?(?:salary|pay|stipend|ctc) (?:for|of) (?:a |an )?(fresher )?(.+?) at (.+?)\??$", re.I)
TOP_ROLE = re.compile(r"^(?:show me |give me )?(?:the )?(?:top|best) (\d{1,2}) (.+?) (?:roles?|jobs?|openings?|positions?|internships?)"
                      r"(?: (?:at|in|for|from|with) (.+?))?\??$", re.I)
Q = "['\"“”‘’]"
SHEET = [
    (re.compile(rf"^(?:please )?add (?:a |one )?(?:new )?column (?:called |named )?{Q}?(.+?){Q}?"
                r"(?: (?:in|to|on) (?:the |my )?(?:sheet|tracker|excel))?\.?$", re.I),
     lambda m: ("sheet_add_column", {"name": m.group(1)})),
    (re.compile(r"^(?:remove|hide|delete|drop) ([0-9a-f]{6,16}) from (?:the |my )?(?:sheet|tracker|excel)\.?$", re.I),
     lambda m: ("sheet_remove_row", {"id": m.group(1)})),
    (re.compile(r"^(?:show|unhide) (?:the )?hidden(?: rows?)?(?: (?:in|on) (?:the )?(?:sheet|tracker))?\.?$|^unhide all\.?$", re.I),
     lambda m: ("sheet_show_hidden", {})),
]
# "put X in <column> for <id>": the LAST " in " splits value from column, so "asked Rahul for referral" stays whole
PUT = re.compile(r"^(?:please )?(?:put|write|set|add|fill(?: in)?|enter) (.+) (?:in|into|under|as|to) (?:the |my )?(.+?)"
                 r"(?: column)?(?: (?:for|on|of) (?:job )?)([0-9a-f]{6,16})\.?$", re.I)
NOTE = re.compile(rf"^(?:please )?(?:add|write|put) (?:a |this )?note {Q}?(.+?){Q}? (?:to|for|on) (?:job )?([0-9a-f]{{6,16}})\.?$|"
                  rf"^(?:add (?:a )?)?note (?:for|on|to) (?:job )?([0-9a-f]{{6,16}}):? {Q}?(.+?){Q}?$", re.I)


def _put(db: DB, t: str) -> tuple[str, dict] | None:
    """Sheet writes without the model: a sheet column (yours or standard) → sheet_set; "note"/"add a note" → add_note."""
    from . import tracker
    if m := NOTE.match(t):
        return "add_note", {"id": m.group(2) or m.group(3), "text": m.group(1) or m.group(4)}
    if not (m := PUT.match(t)):
        return None
    value, column = m.group(1).strip(" '\"“”‘’"), m.group(2).strip(" '\"“”‘’")
    quoted = bool(re.match(Q, m.group(1).strip()))
    on_sheet = tracker.column_name(db, column) or column.lower() in {h.lower() for h in tracker.H + tracker.TAIL}
    return ("sheet_set", {"id": m.group(3), "column": column, "value": value}) if on_sheet or quoted else None


# ── the common asks, typos and Kannada-English included, without the model (tests/evals/chat.jsonl) ──
ROLE = {"python", "java", "javascript", "react", "node", "sql", "ml", "ai", "genai", "llm", "llms", "nlp", "cv", "ds",
        "data", "scientist", "science", "analyst", "analytics", "engineer", "developer", "intern", "interns",
        "internship", "internships", "fresher", "freshers", "trainee", "machine", "learning", "deep", "computer",
        "vision", "generative", "backend", "frontend", "fullstack", "full", "stack", "django", "fastapi", "flask",
        "langgraph", "langchain", "rag", "mlops", "devops", "cloud", "software", "research", "sde", "pytorch",
        "tensorflow", "opencv", "agentic", "agents", "automation", "testing", "embedded", "web", "mobile", "android"}
CITIES = {"blr": "Bengaluru", "bengaluru": "Bengaluru", "bangalore": "Bengaluru", "mysuru": "Mysuru", "mysore": "Mysuru",
          "mangaluru": "Mangaluru", "mangalore": "Mangaluru", "karnataka": "Karnataka", "hyderabad": "Hyderabad",
          "chennai": "Chennai", "pune": "Pune", "mumbai": "Mumbai", "delhi": "Delhi", "india": "India", "remote": "Remote"}
NOUN = r"(?:jobs?|roles?|openings?|positions?|vacanc(?:y|ies)|opportunit(?:y|ies)|posts?|notices?|notifications?)"
BOARDS = r"kpsc|kea|kptcl|kpcl|bescom|upsc|ssc|ibps|rrb|isro|drdo|bel|hal|nic"      # a board name alone means govt
WORDS = ROLE | set(CITIES) | set(BOARDS.split("|")) | {"jobs", "roles", "openings", "positions", "government", "govt", "notices", "closing",
                              "salary", "stipend", "today", "week", "posted", "search", "show", "find", "applied"}
KANNADA = [(r"\bee vaara(?:da)?\b", "this week"), (r"\b(?:ivattu|indu)\b", "today"), (r"\b(?:yaavudu|yavudu|yaava)\b", "which"),
           (r"\bfit (?:aagutta|agutta|aagutte|agutte)\b", "fit"), (r"\b(?:sambala|sambla)\b", "salary"),
           (r"\bhosa\b", "new"), (r"\b(?:kelasa|kelsa)\b", "jobs"), (r"\bapply (?:maadide|madide|maadidini|madidini)\b", "applied"),
           (r"\b(?:ide|ideya|idya|nanage|nange|estu|eshtu|sigutte|kodtare|beku|swalpa|torsu|thorisu)\b", ""),
           (r"\b(\w+) alli\b", r"in \1")]


def _norm(t: str) -> str:
    """Lower case, Kannada words → English, role/city typos → the word ('pyhton devloper blr' → 'python developer blr')."""
    import difflib
    n = re.sub(r"[?!.,]+(?=\s|$)", " ", (t or "").lower())
    for rx, to in KANNADA:
        n = re.sub(rx, to, n)
    out = []
    for w in n.split():
        if len(w) >= 4 and w.isalpha() and w not in WORDS and w not in STOP:
            w = (difflib.get_close_matches(w, WORDS, 1, 0.75) or [w])[0]
        out.append(w)
    return " ".join(out)


def _when(n: str) -> tuple[str, int | None]:
    """'posted this week' / 'today' / 'in the last 3 days' → days; the rest of the message."""
    for rx, d in ((r"\b(?:posted )?(?:this|last|past) week\b", 7), (r"\b(?:posted )?today\b", 1)):
        if re.search(rx, n):
            return re.sub(rx, " ", n), d
    if m := re.search(r"\b(?:posted )?(?:in )?(?:the )?(?:last|past) (\d{1,2}) days\b", n):
        return n[:m.start()] + " " + n[m.end():], int(m.group(1))
    return n, None


def _search(n: str) -> tuple[str, dict, str] | None:
    """'find ML intern jobs in Bengaluru posted this week', 'any genai roles at zoho', 'search mlops'."""
    n, days = _when(n)
    where = None
    if m := re.search(rf"(?:\b(?:in|at|near|around|from) )?\b({'|'.join(CITIES)})\b", n):
        where, n = CITIES[m.group(1)], n[:m.start()] + " " + n[m.end():]
    n = " ".join(n.split())
    verb = re.match(r"^(?:can you |please |hey )*(?:find|search(?: for)?|look for|show|list|get|give)(?: me)?(?: some| any| all)? ", n)
    n = re.sub(r"^(?:can you |please |hey )*(?:(?:find|search(?: for)?|look for|show|list|get|give)(?: me)?|any|are there(?: any)?|"
               r"which|what are the|new)(?: some| any| all| new)? ", "", n)
    company = ""
    if m := re.search(r" (?:at|with|in) ([a-z][\w&.-]*)$", n):
        company, n = m.group(1), n[:m.start()]
    n = re.sub(r"( for freshers?| for me| available| open)+$", "", n)
    if m := re.fullmatch(rf"(.+?) (internships?|{NOUN})", n):
        role = m.group(1) + (" intern" if m.group(2).startswith("internship") and "intern" not in m.group(1) else "")
    elif verb or re.search(r"\binterns?(?:hips?)?$", n):
        role = re.sub(r"\binternships?$", "intern", n)
    else:
        return None
    if not role or not set(role.split()) & ROLE or not set(role.split()) <= ROLE | {"and", "or", "/", "a", "an"} | set(ALIASES):
        return None
    args: dict = {"query": " ".join([role, company]).strip(), "location": "India" if company and not where else where or "Karnataka"}
    if days:
        args["days"] = days
    return "search_jobs", args, ""


def _govt(n: str) -> tuple[str, dict, str] | None:
    """'govt jobs closing this week', 'karnataka govt notices', 'which govt job this week' (ee vaara yaavudu govt job ide)."""
    board = re.search(rf"\b({BOARDS})\b", n)
    if not re.search(r"\bgov(?:t|ernment)\b", n) and not board:
        return None
    args: dict = {}
    if m := re.search(r"\b(?:closing|ending|last date|deadlines?)(?: (?:soon|this week|in (\d{1,2}) days|within (\d{1,2}) days))?\b", n):
        args["closing_within_days"] = int(m.group(1) or m.group(2) or 7)
        n = n[:m.start()] + " " + n[m.end():]
    if m := (board or re.search(r"\b(karnataka|central|state|psu|bank|railway)\b", n)):
        args["filter"] = m.group(1)
        n = n[:m.start()] + " " + n[m.end():]
    rest = re.sub(rf"\b(?:gov(?:t|ernment)|{NOUN}|any|are|there|show|me|list|which|what|the|in|new|this week|soon|for)\b", " ", n)
    return ("govt", args, "") if not rest.strip() else None


SALARY2 = re.compile(r"^(?:what(?:'s| is) )?(?:the )?(?:salary|pay|stipend|ctc) (?:for|of) (?:a |an )?(.+?) (?:in|at) (.+?)$|"
                     r"^(?:what(?:'s| is) )?(?:the |a |an )?(.+?) (?:salary|pay|stipend|ctc)(?: for (?:a )?freshers?)?(?: (?:in|at) (.+?))?$")


def _salary(n: str) -> tuple[str, dict, str] | None:
    if m := re.fullmatch(r"how much (?:does|will|would) ([0-9a-f]{6,16}) pay", n):
        return "salary_estimate", {"role": "", "id": m.group(1)}, ""
    if not (m := SALARY2.match(n)):
        return None
    role, at = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4) or "")
    if not set(role.split()) & ROLE:
        return None
    city = CITIES.get(at.split(",")[0].strip())
    return "salary_estimate", {"role": role, "company": "" if city or not at else at, "location": city or "Bengaluru"}, ""


def _more_fast(n: str) -> tuple[str, dict, str] | None:
    words = set(n.split())
    if words <= {"top", "best", "show", "me", "the", "give", "open", "jobs", "job", "roles", "my"} | {str(i) for i in range(1, 21)} \
            and words & {"top", "best"}:
        return "top_jobs", {"n": int(next((w for w in n.split() if w.isdigit()), 5))}, ""
    if words <= {"today", "which", "new", "jobs", "job", "any", "show", "me", "posted", "what", "are", "the", "s", "latest",
                 "what's", "today's", "in"} and words & {"today", "today's"}:
        return "today", {}, ""
    return None


def _fast(db: DB, t: str) -> tuple[str, dict, str] | None:
    """(tool, args, note) when the intent is obvious; None → the model decides. Same rules for reply() and route()."""
    if m := SALARY.match(t):                           # "how much would a fresher AI engineer get at Zoho?"
        lvl, role, at = (m.group(1), m.group(2), m.group(3)) if m.group(2) else (m.group(4), m.group(5), m.group(6))
        role = (role + " intern") if (lvl or "").strip().lower() == "intern" and "intern" not in role.lower() else role
        where = at if at.lower().split(",")[0].strip() in CITY else "Bengaluru"
        company = "" if at.lower().split(",")[0].strip() in CITY else at
        return "salary_estimate", {"role": role, "company": company, "location": where}, ""
    if m := TOP_ROLE.match(t):                         # "top 5 GenAI roles at product companies"
        where, note = "Karnataka", ""
        extra = (m.group(3) or "").strip()
        if extra.lower().split(",")[0].strip() in CITY or extra.lower() in ("india", "remote", "anywhere"):
            where = extra
        elif extra:
            note = f"I can't tell “{extra}” from the postings: these are the best {m.group(2)} roles."
        return "search_jobs", {"query": m.group(2), "location": where, "limit": int(m.group(1))}, note
    if hit := _put(db, t):
        return (*hit, "")
    for rx, make in SHEET + FAST:
        if m := rx.match(t):
            return (*make(m), "")
    n = _norm(t)
    for rx, make in FAST:                             # the same rules after Kannada words and typos are fixed
        if m := rx.match(n):
            return (*make(m), "")
    return _govt(n) or _salary(n) or _more_fast(n) or _search(n)


# ── reading a posting ───────────────────────────────────────────────────────
def my_skills() -> list[str]:
    """Your skills: config skills + every stack/tag in resume.yaml (nothing else)."""
    out: list[str] = []
    for g in (config.cfg("profile").get("skills") or {}).values():
        out += list(g or [])
    r = config.cfg("resume")
    for item in [*(r.get("experience") or []), *(r.get("projects") or [])]:
        out += list(item.get("stack") or []) + list(item.get("tags") or [])
    seen, res = set(), []
    for s in out:
        if str(s).lower() not in seen:
            seen.add(str(s).lower())
            res.append(str(s))
    return res


def _same(a: str, b: str) -> bool:
    from .prep import ALIASES as PA
    na, nb = re.sub(r"[^a-z0-9+#]", "", a.lower()).rstrip("s"), re.sub(r"[^a-z0-9+#]", "", b.lower()).rstrip("s")
    return na == nb or nb in [re.sub(r"[^a-z0-9+#]", "", x) for x in PA.get(a.lower(), [])]


ROLE_HEADS = [r"about the role", r"role overview", r"job summary", r"position summary", r"role summary",
              r"what you(?:'ll| will) do", r"your role", r"the role", r"key responsibilities",
              r"(?<!social )responsibilities", r"job description"]       # best first


def role_summary(desc: str) -> str:
    """What the job is: the text after a 'Responsibilities' / 'About the role' heading, else the first sentences
    that aren't about the company. Words from the posting only."""
    if not desc:
        return "not in the posting"
    m = next((m for h in ROLE_HEADS if (m := re.search(rf"\b{h}\b", desc, re.I))), None)
    if m:
        part = desc[m.end():].lstrip(" :-–•")
    else:
        sents = re.split(r"(?<=[.!?])\s+", desc)
        part = " ".join(x for x in sents if not re.search(r"\babout (?:us|the company)\b|\bwe are a\b|^about\b", x, re.I))
    part = part[:350]
    return (part.rsplit(" ", 1)[0] + "…") if len(part) == 350 else part or "not in the posting"
