"""A pasted link → which company it is, read from the link or the page itself.

ATS links (Greenhouse, Lever, Ashby, Workday …) name the board in the URL. LinkedIn job links are looked up in our DB
first (we usually scanned them), else LinkedIn's public job page. Any other page: its own og:site_name / JSON-LD
Organization / <title>. A page that can't be read says so (read=False, why=…): nothing is filled in from memory.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

from .sources import ats_more

URL = re.compile(r"^(?:https?://|www\.)\S+$|^(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?$", re.I)
BLOCKED = re.compile(r"cf-browser-verification|challenge-platform|captcha|access denied|attention required|"
                     r"request blocked|are you a robot|enable javascript and cookies", re.I)
ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "smartrecruiters.com", "workable.com",
             "keka.com", "darwinbox.in", "recruitee.com", "zohorecruit")


def is_url(text: str) -> bool:
    return bool(URL.match((text or "").strip()))


def _norm(url: str) -> str:
    url = url.strip().rstrip(").,")
    return url if "://" in url else "https://" + url


def _title_case(slug: str) -> str:
    return " ".join(w.capitalize() if w.islower() else w for w in re.split(r"[-_ ]+", slug) if w)


def _meta(html: str, prop: str) -> str:
    m = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)', html or "", re.I) \
        or re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{re.escape(prop)}["\']', html or "", re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def _org(html: str) -> dict:
    """The first JSON-LD Organization (name, description, founded, address) on the page."""
    for blob in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html or "", re.S | re.I):
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if isinstance(it, dict) and str(it.get("@type", "")).lower() in ("organization", "corporation", "localbusiness"):
                return it
    return {}


def site_name(html: str, host: str) -> str:
    name = _meta(html, "og:site_name") or _org(html).get("name") or ""
    if not name:
        t = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
        parts = [p.strip() for p in re.split(r"\s[|\-–—:]\s", t.group(1)) if p.strip()] if t else []
        label = host.removeprefix("www.").split(".")[0].lower()
        name = next((p for p in parts if label in re.sub(r"[^a-z0-9]", "", p.lower())), parts[-1] if parts else "")
    return re.sub(r"\s+", " ", name).strip()


def _linkedin_job_id(u) -> str | None:
    m = re.search(r"/jobs/view/(?:[^/]*?-)?(\d{6,})", u.path) or re.search(r"(\d{6,})", parse_qs(u.query).get("currentJobId", [""])[0])
    return m.group(1) if m else None


def identify(url: str, get, db=None) -> dict:
    """{name, domain, kind, read, why, facts: [(label, value, source_url)], job_id}. `get(url, **kw)` → response."""
    url = _norm(url)
    u = urlparse(url)
    host = u.netloc.lower().removeprefix("www.")
    out = {"url": url, "name": "", "domain": None, "kind": "site", "read": True, "why": "", "facts": [], "job_id": None}
    if host.endswith("linkedin.com"):
        jid = _linkedin_job_id(u)
        if jid:
            out["kind"] = "linkedin-job"
            if db is not None:
                row = db.c.execute("SELECT id, company FROM jobs WHERE url LIKE ? OR urls LIKE ? LIMIT 1",
                                   (f"%/jobs/view/{jid}%", f"%/jobs/view/{jid}%")).fetchone()
                if row:
                    out.update(name=row[1], job_id=row[0])
                    return out
            page = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}"
            r = _fetch(get, page, out)
            if r is not None:
                m = re.search(r'class="topcard__org-name-link[^"]*"[^>]*>\s*([^<]+?)\s*<', r.text) \
                    or re.search(r"^(.+?) hiring .+? in .+?\| LinkedIn", _meta(r.text, "og:title") or "")
                if m:
                    out["name"] = m.group(1).strip()
                else:
                    out.update(read=False, why="LinkedIn showed a login wall instead of the job")
            return out
        m = re.match(r"/(?:company|school)/([^/?#]+)", u.path)
        if m:
            out.update(kind="linkedin-company", name=_title_case(m.group(1)))
            out["facts"].append(("LinkedIn page", url, url))
            return out
        out.update(read=False, why="not a LinkedIn job or company link")
        return out
    if any(h in host for h in ATS_HOSTS):
        boards = ats_more.detect_boards(url, url)
        if boards:
            b = boards[0]
            out.update(kind="ats", boards=boards, name=_title_case(b.get("tenant") or b.get("token") or host.split(".")[0]))
            r = _fetch(get, url, out)                   # the board page usually names the company properly
            if r is not None:
                better = _meta(r.text, "og:site_name") or _org(r.text).get("name")
                if better and better.lower() not in ("greenhouse", "lever", "workday", "ashby"):
                    out["name"] = better
            out["read"] = True                          # the board itself is known from the URL, even if the page blocks us
            return out
    out["domain"] = host
    r = _fetch(get, url, out)
    if r is None:
        out["name"] = _title_case(host.split(".")[0])
        out["facts"].append(("Name", f"{out['name']} (from the web address only)", url))
        return out
    org = _org(r.text)
    out["name"] = site_name(r.text, host) or _title_case(host.split(".")[0])
    out["boards"] = ats_more.detect_boards(r.text, r.url)
    desc = _meta(r.text, "og:description") or _meta(r.text, "description") or org.get("description") or ""
    if desc:
        out["facts"].append(("What it says it does", desc[:300], r.url))
    if org.get("foundingDate"):
        out["facts"].append(("Founded", str(org["foundingDate"])[:10], r.url))
    addr = org.get("address") or {}
    if isinstance(addr, dict) and addr.get("addressLocality"):
        out["facts"].append(("Address on its site", ", ".join(str(addr[k]) for k in ("addressLocality", "addressRegion",
                                                                                      "addressCountry") if addr.get(k)), r.url))
    return out


def _fetch(get, url: str, out: dict):
    """The response, or None with out['read']=False and why in plain words."""
    try:
        r = get(url, headers={"Accept": "text/html"}, timeout=10)
    except Exception as exc:
        out.update(read=False, why=f"couldn't read {url} ({type(exc).__name__})")
        return None
    if r.status_code >= 400:
        out.update(read=False, why=f"couldn't read {url} (HTTP {r.status_code}" +
                   (": the site blocks automated reading)" if r.status_code in (401, 403, 429, 503) else ")"))
        return None
    if BLOCKED.search(r.text[:20000]) and len(re.findall(r"<a\s", r.text, re.I)) < 5:
        out.update(read=False, why=f"couldn't read {url} (it shows a bot check instead of the page)")
        return None
    return r
