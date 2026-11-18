"""Per-job tailoring: keyword gap report, project selection, cover letter.

Truth rules (enforced, not just promised):
  • Resume content is only SELECTED and REORDERED from config/resume.yaml.
  • A cover letter is checked by hunter-cover's verifier: every number must
    exist in your resume, no placeholders, no clichés, the right company.
    A letter that fails is regenerated once, then replaced by a factual template.
"""
from __future__ import annotations

import importlib.util
import re
from functools import lru_cache

import yaml

from . import config, llm
from .util import norm


def resume() -> dict:
    return config.cfg("resume")


def _live(items):
    """Drop entries/bullets still marked TODO."""
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


def resume_text() -> str:
    """Flat text of everything you've claimed (for number verification)."""
    r, p = resume(), config.cfg("profile")
    return yaml.safe_dump({"resume": r, "profile": {k: p.get(k) for k in ("name", "phone", "phone_digits", "email",
                                                                         "education", "internships", "skills", "exams")}},
                          allow_unicode=True)


def keywords(jd: str) -> list[str]:
    prof = config.cfg("profile")
    vocab = {x for g in prof.get("skills", {}).values() for x in (g or [])}
    vocab |= {"Python", "Java", "C++", "SQL", "PyTorch", "TensorFlow", "Keras", "scikit-learn", "pandas", "NumPy",
              "Docker", "Kubernetes", "AWS", "GCP", "Azure", "FastAPI", "Flask", "Django", "React", "Node.js",
              "LLMs", "RAG", "LangChain", "LlamaIndex", "Hugging Face", "Transformers", "NLP", "Computer Vision",
              "OpenCV", "MLflow", "Airflow", "Spark", "Kafka", "Git", "Linux", "REST APIs", "Microservices",
              "CI/CD", "MongoDB", "PostgreSQL", "Redis", "Vector search", "Prompt engineering", "Deep Learning",
              "Machine Learning", "Statistics", "Data Structures", "Algorithms", "TypeScript", "JavaScript",
              "Generative AI", "Fine-tuning", "LoRA", "Agents", "Power BI", "Tableau", "Excel", "Selenium",
              "Playwright", "Go", "Rust", "Next.js", "GraphQL", "Agile", "Unit testing", "pytest"}
    t = jd.lower()
    return sorted({k for k in vocab if re.search(rf"(?<![a-z0-9]){re.escape(k.lower())}(?![a-z0-9])", t)},
                  key=str.lower)


def gap(jd: str) -> dict:
    """What the JD asks vs what your profile/resume can claim."""
    asked = keywords(jd)
    have_text = resume_text().lower()
    have = [k for k in asked if k.lower() in have_text]
    missing = [k for k in asked if k not in have]
    score = round(100 * len(have) / len(asked)) if asked else 0
    return {"match": score, "have": have, "missing": missing}


def pick_projects(jd: str, n: int = 3) -> list[dict]:
    projs = [p for p in _live(resume().get("projects")) if p.get("bullets")]
    t = norm(jd)
    def rel(p):
        terms = [*(p.get("tags") or []), *(p.get("stack") or [])]
        return sum(2 if norm(x) in t else 0 for x in terms) + sum(1 for b in p["bullets"] if any(
            norm(w) in t for w in re.findall(r"[A-Za-z][A-Za-z+.#]{2,}", b)[:12]))
    return sorted(projs, key=rel, reverse=True)[:n]


@lru_cache(maxsize=1)
def _cover_mod():
    f = config.path("skills", "hunter-cover", "cover.py")
    spec = importlib.util.spec_from_file_location("hunter_cover", f)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def verify_cover(text: str, company: str, role: str) -> dict:
    others = [d for d in config.cfg("profile").get("dream_companies", []) if norm(d) != norm(company)]
    return _cover_mod().check(text, company=company, role=role, resume_text=resume_text(), others=others)


SYSTEM = """You write short, specific cover letters for an Indian final-year B.Tech AI & ML student.
Rules: 150-230 words. Greeting "Dear {company} hiring team,". Name the exact role and company.
Use ONLY facts from the FACTS block; never invent numbers, employers, users or results. If a fact is
missing, leave it out. No clichés ("passionate", "team player", "hard-working", "I am writing to").
One proof sentence with a real number from FACTS. Close by asking for a short call. Sign as the name given.
Plain text only."""


def template_cover(job: dict) -> str:
    p, r = config.cfg("profile"), resume()
    projs = pick_projects(job.get("description") or "", 2)
    exp = _live(r.get("experience"))
    lines = [f"Dear {job['company']} hiring team,", "",
             f"I'm applying for the {job['title']} role. I'm a final-year B.Tech student in Artificial Intelligence "
             f"and Machine Learning at REVA University, Bengaluru (CGPA {p['education'][0]['cgpa']}/10, graduating "
             f"{p['education'][0]['end']})."]
    if exp and exp[0].get("bullets"):
        lines += ["", f"At {exp[0]['company']} I worked as {exp[0]['title']}: {exp[0]['bullets'][0].rstrip('.')}."]
    for pr in projs:
        lines += ["", f"{pr['name']}: {pr['bullets'][0].rstrip('.')}."]
    lines += ["", "I can start immediately. Could we set up a short call to discuss the role?", "",
              f"Regards,\n{p['name']}\n{p['phone']} · {p['email']}\n{p['links'].get('github', '')}"]
    return "\n".join(lines)


def cover(job: dict) -> tuple[str, dict, str]:
    """(letter, verifier report, how) where how ∈ {'llm', 'template'}."""
    p = config.cfg("profile")
    if llm.available():
        facts = resume_text()
        # The LLM may be a cloud API: it gets placeholders, never your phone or email.
        facts = facts.replace(str(p.get("phone", "")), "[PHONE]").replace(str(p.get("email", "")), "[EMAIL]")
        if p.get("phone_digits"):
            facts = facts.replace(str(p["phone_digits"]), "[PHONE]")
        user = (f"ROLE: {job['title']}\nCOMPANY: {job['company']}\nNAME: {p['name']}\n"
                f"CONTACT: [PHONE] · [EMAIL]   (write these two tokens exactly; they are filled in later)"
                f"\n\nJOB DESCRIPTION:\n{(job.get('description') or '')[:3500]}"
                f"\n\nFACTS:\n{facts[:5000]}")
        for _ in range(2):
            out = llm.chat(SYSTEM.replace("{company}", job["company"]), user)
            if not out:
                break
            out = out.replace("[PHONE]", str(p.get("phone", ""))).replace("[EMAIL]", str(p.get("email", "")))
            rep = verify_cover(out, job["company"], job["title"])
            if rep["ready"]:
                return out, rep, "llm"
            user += "\n\nYour previous draft failed these checks; fix them: " + "; ".join(
                f"{c['check']}: {c['detail']}" for c in rep["checks"] if c["status"] == "FAIL")
    text = template_cover(job)
    return text, verify_cover(text, job["company"], job["title"]), "template"
