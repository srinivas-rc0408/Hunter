"""Interview prep pack, one Telegram screen, sent when you mark /interview_<id>.

What the company does (Wikidata), the JD's top 5 skills, 5 likely questions (3 on those skills, 2 behavioral from
skills/hunter-interview's bank) and, for each, which of YOUR projects to talk about. Projects come only from
config/resume.yaml; when none uses a skill it says so instead of inventing one."""
from __future__ import annotations

import html
import importlib.util
import json
import re

from . import config
from .tailor import _live, keywords, pick_projects


def top_skills(jd: str, n: int = 5) -> list[str]:
    t = (jd or "").lower()
    count = lambda k: len(re.findall(rf"(?<![a-z0-9]){re.escape(k.lower())}(?![a-z0-9])", t))
    return sorted(keywords(jd or ""), key=lambda k: -count(k))[:n]


ALIASES = {"generative ai": ["genai", "llm"], "llms": ["llm"], "agents": ["agentic"], "rag": ["retrieval"]}


def _uses(p: dict, skill: str) -> bool:
    text = " ".join([*(p.get("stack") or []), *(p.get("tags") or []), *(p.get("bullets") or [])]).lower().replace("-", " ")
    return any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", text)
               for w in [skill.lower().replace("-", " "), *ALIASES.get(skill.lower(), [])])


def _behavioral(company: str, n: int = 2) -> list[str]:
    spec = importlib.util.spec_from_file_location("hunter_star", config.path("skills", "hunter-interview", "star.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    bank = json.loads(config.path("skills", "hunter-interview", "questions.json").read_text(encoding="utf-8"))
    return [q["question"] for q in m.pick(bank, company, round_="behavioral", count=n, include_any=True)] or \
           [q["question"] for q in m.pick(bank, company, count=n, include_any=True)]


def _what_they_do(company: str) -> str:
    try:
        from .companies import about
        return (about({"name": company}) or {}).get("description") or ""
    except Exception:                       # offline / Wikidata down: the pack still goes out
        return ""


def pack(j: dict) -> str:
    e = lambda s: html.escape(str(s or ""), quote=False)
    projs = [p for p in _live(config.cfg("resume").get("projects")) if p.get("bullets")]
    skills = top_skills(j.get("description") or "")
    best = pick_projects(j.get("description") or j["title"], n=2)
    L = [f"🎤 <b>Interview prep · {e(j['title'][:70])} · {e(j['company'])}</b>"]
    does = _what_they_do(j["company"])
    L.append(f"🏢 {e(does)}" if does else f"🏢 what they do: not found, read their site (/company {e(j['company'])})")
    L.append("🧰 <b>Top skills in the JD:</b> " + (e(", ".join(skills)) if skills else "none recognised: read the JD"))
    used = lambda s: next((p for p in projs if _uses(p, s)), None)
    qs = [(f"How have you used {s}? Walk me through one project.", used(s), s) for s in skills[:3]]
    qs += [(q, best[i % len(best)] if best else None, None) for i, q in enumerate(_behavioral(j["company"], 5 - len(qs)))]
    L.append("<b>❓ Likely questions → your project</b>")
    for i, (q, p, s) in enumerate(qs, 1):
        say = (f"→ {e(p['name'])}" if p else
               f"→ no project in your resume names {e(s)}" + (f"; closest: {e(best[0]['name'])} (only if it really used it)"
                                                               if best else "") + ": else say so honestly" if s else
               "→ no project in your resume fits: use a real story of yours")
        L.append(f"{i}. {e(q)}\n   {say}")
    return "\n".join(L)
