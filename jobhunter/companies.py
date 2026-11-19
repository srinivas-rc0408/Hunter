"""Company search: type part of a name → pick from suggestions → every listing, live.

suggest(q)      names from your watchlist, the company directory, companies already in your
                job database, and Wikidata (free, no key), plus "search as typed"
resolve(...)    finds the company's website, careers page and hiring platform (ATS):
                1. known boards (watchlist / companies.yaml / cache)
                2. careers page links → Greenhouse, Lever, Ashby, Workday, SmartRecruiters,
                   Workable, Recruitee, Keka, Darwinbox, Zoho Recruit (+ schema.org JobPosting)
                3. probing each platform's public API with name-derived slugs (verified, not guessed:
                   a slug only counts if the platform answers with this company's jobs/name)
                4. on your laptop, JavaScript-only careers pages are rendered with Playwright
listings(...)   ATS listings + LinkedIn/Naukri/Indeed results for that company + jobs already
                in your database, deduplicated, scored, newest first, with Karnataka fit marked
links(...)      referral and research links: REVA alumni there, recruiters, reviews, salaries
watch(...)      add the company to config/watchlist.yaml so every scan includes it
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import os
import re
from urllib.parse import quote_plus, urlparse

import requests
import yaml

from . import config
from .db import DB, Job, job_id
from .score import score
from .sources import ats_more
from .util import iso, norm

UA = {"User-Agent": ats_more.UA, "Accept": "application/json, text/html;q=0.9"}
TIMEOUT = 10
BUSINESS = re.compile(r"compan|business|corporat|conglomerat|startup|software|technolog|enterprise|firm|bank|"
                      r"manufactur|multinational|subsidiar|consult|services|it |electronics|semiconductor|"
                      r"e-commerce|ecommerce|fintech|platform|airline|automotive|pharmaceutical|telecom|retail|"
                      r"public sector undertaking|psu|organisation|organization|agency|research institute|laborator",
                      re.I)
SUFFIX = re.compile(r"\b(?:pvt|private|ltd|limited|inc|llc|llp|corp|corporation|co|company|technologies|technology|"
                    r"tech|solutions|software|services|systems|labs|india|global|group|the)\b")


def cname(s: str | None) -> str:
    return " ".join(SUFFIX.sub(" ", norm(s)).split())


def same_company(a: str | None, b: str | None) -> bool:
    x, y = cname(a), cname(b)
    if not x or not y:
        return False
    return x == y or (len(x) >= 4 and (x in y.split() or f" {x} " in f" {y} ")) or \
        (len(y) >= 4 and f" {y} " in f" {x} ")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    return s


S = _session()


def _get(url, **kw):
    return S.get(url, timeout=kw.pop("timeout", TIMEOUT), allow_redirects=True, **kw)


def _post(url, body):
    return S.post(url, json=body, timeout=TIMEOUT)


# ── data files ─────────────────────────────────────────────────────────────
def directory() -> list[dict]:
    return config.cfg("company_directory").get("companies", []) or []


def watchlist_path():
    return config.path("config", "watchlist.yaml")


def watchlist() -> list[dict]:
    f = watchlist_path()
    if not f.is_file():
        return []
    return (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("companies", []) or []


def _cache_path():
    return config.data_dir() / "company_cache.json"


def _cache() -> dict:
    try:
        return json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return {}


def _cache_put(key: str, val: dict) -> None:
    c = _cache()
    c[key] = {**val, "_at": iso()}
    _cache_path().write_text(json.dumps(c, indent=1, default=str))


# ── suggestions ────────────────────────────────────────────────────────────
def wikidata_search(q: str, limit: int = 8) -> list[dict]:
    try:
        r = _get("https://www.wikidata.org/w/api.php", params={
            "action": "wbsearchentities", "search": q, "language": "en", "type": "item", "limit": 20, "format": "json"})
        rows = r.json().get("search", [])
    except (requests.RequestException, ValueError):
        return []
    return parse_wikidata_search(rows)[:limit]


def parse_wikidata_search(rows: list[dict]) -> list[dict]:
    out = []
    for x in rows:
        desc = x.get("description") or ""
        if BUSINESS.search(desc) and not re.search(r"film|album|song|novel|deity|goddess|episode|footballer|actor|"
                                                   r"politician|village|river|species", desc, re.I):
            out.append({"name": x.get("label"), "wikidata": x.get("id"), "hint": desc, "source": "wikidata"})
    return out


def suggest(q: str, limit: int = 12, online: bool = True) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    nq = norm(q)
    seen, out = set(), []

    def add(item):
        k = cname(item["name"])
        if k and k not in seen:
            seen.add(k)
            out.append(item)

    def hit(name, aliases=()):
        names = [norm(name)] + [norm(a) for a in aliases]
        return any(n.startswith(nq) or f" {nq}" in f" {n}" or nq in n.replace(" ", "") for n in names)

    for c in watchlist():
        if hit(c["company"]):
            add({"name": c["company"], "source": "watchlist", "hint": f"watched · {c.get('type')}"})
    for c in config.cfg("companies").get("boards", []) or []:
        if hit(c["company"]):
            add({"name": c["company"], "source": "boards", "hint": f"{c['type']} board"})
    for c in directory():
        if hit(c["name"], c.get("aliases", [])):
            add({"name": c["name"], "domain": c.get("domain"), "source": "directory", "hint": c.get("note") or c.get("domain")})
    try:
        for name, n in DB().c.execute(
                "SELECT company, COUNT(*) FROM jobs WHERE kind='private' AND LOWER(company) LIKE ? GROUP BY company "
                "ORDER BY COUNT(*) DESC LIMIT 15", (f"%{q.lower()}%",)):
            add({"name": name, "source": "your jobs", "hint": f"{n} job(s) already found"})
    except Exception:
        pass
    if online and len(out) < limit:
        for w in wikidata_search(q):
            add(w)
    out = out[: limit - 1]
    out.append({"name": q, "source": "as typed", "hint": "search exactly this name"})
    return out


# ── resolve: website → careers page → ATS boards ───────────────────────────
def wikidata_details(qid: str) -> dict:
    try:
        r = _get("https://www.wikidata.org/w/api.php", params={
            "action": "wbgetentities", "ids": qid, "props": "claims|descriptions|labels", "languages": "en", "format": "json"})
        e = r.json()["entities"][qid]
    except (requests.RequestException, ValueError, KeyError):
        return {}

    def claim(p):
        try:
            return e["claims"][p][0]["mainsnak"]["datavalue"]["value"]
        except (KeyError, IndexError, TypeError):
            return None
    emp = claim("P1128")
    inc = claim("P571")
    return {"name": e.get("labels", {}).get("en", {}).get("value"), "website": claim("P856"),
            "description": e.get("descriptions", {}).get("en", {}).get("value"),
            "employees": str(emp.get("amount", "")).lstrip("+") if isinstance(emp, dict) else None,
            "founded": (inc.get("time", "")[1:5] if isinstance(inc, dict) else None)}


def slug_candidates(name: str, domain: str | None = None) -> list[str]:
    base = cname(name) or norm(name)
    words = base.split()
    full = norm(name).split()
    c = ["".join(words), "-".join(words), "".join(full), "-".join(full)]
    if words:
        c.append(words[0])
    if domain:
        c.insert(0, urlparse(domain if "//" in domain else "//" + domain).netloc.split(".")[-2] if "." in domain else domain)
    out = []
    for s in c:
        s = re.sub(r"[^a-z0-9-]", "", s.lower())
        if len(s) >= 3 and s not in out:
            out.append(s)
    return out[:5]


def _probe(kind: str, slug: str, name: str) -> dict | None:
    """Return a board only if the platform answers with THIS company (never a guess)."""
    try:
        if kind == "greenhouse":
            r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
            if r.status_code == 200 and same_company(r.json().get("name"), name):
                return {"type": "greenhouse", "token": slug}
        elif kind == "lever":
            r = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1")
            if r.status_code == 200 and isinstance(r.json(), list) and r.json() and \
                    norm(slug).replace(" ", "") == cname(name).replace(" ", ""):
                return {"type": "lever", "token": slug}
        elif kind == "ashby":
            r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            if r.status_code == 200 and r.json().get("jobs") is not None and \
                    norm(slug).replace(" ", "") == cname(name).replace(" ", ""):
                return {"type": "ashby", "token": slug}
        elif kind == "smartrecruiters":
            r = _get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1")
            c = (r.json().get("content") or [{}])[0] if r.status_code == 200 else {}
            if c and same_company((c.get("company") or {}).get("name"), name):
                return {"type": "smartrecruiters", "token": (c.get("company") or {}).get("identifier", slug)}
        elif kind == "workable":
            r = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
            if r.status_code == 200 and same_company(r.json().get("name"), name):
                return {"type": "workable", "token": slug}
        elif kind == "keka":
            r = _get(f"https://{slug}.keka.com/careers/api/organization/default/careerportalinfo")
            if r.status_code == 200 and same_company((r.json() or {}).get("name"), name):
                return {"type": "keka", "token": slug}
        elif kind == "darwinbox":
            host = f"{slug}.darwinbox.in"
            r = _post(f"https://{host}/ms/candidateapi/job/alljobs", {"page": 1, "limit": 1})
            if r.status_code == 200 and r.json().get("status") == "success" and \
                    norm(slug).replace(" ", "") == cname(name).replace(" ", ""):
                return {"type": "darwinbox", "host": host, "token": slug}
        elif kind == "recruitee":
            r = _get(f"https://{slug}.recruitee.com/api/offers/")
            offers = r.json().get("offers") if r.status_code == 200 else None
            if offers and same_company(offers[0].get("company_name") or slug, name):
                return {"type": "recruitee", "token": slug}
    except (requests.RequestException, ValueError, AttributeError, IndexError):
        return None
    return None


PROBE_KINDS = ["greenhouse", "lever", "ashby", "smartrecruiters", "workable", "keka", "darwinbox", "recruitee"]


def careers_pages(website: str) -> list[str]:
    u = urlparse(website if "//" in website else "https://" + website)
    host = u.netloc.removeprefix("www.")
    return [f"https://{u.netloc}/careers", f"https://{u.netloc}/careers/", f"https://{u.netloc}/jobs",
            f"https://careers.{host}", f"https://{u.netloc}/join-us", f"https://{u.netloc}/work-with-us",
            f"https://jobs.{host}", f"https://{u.netloc}/company/careers", f"https://{u.netloc}"]


def _render(url: str) -> str:
    """JavaScript-rendered HTML (laptop only; skipped in the cloud)."""
    if os.environ.get("CI") or os.environ.get("JOBHUNTER_NO_BROWSER"):
        return ""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) Chrome/130 Safari/537.36")
            pg.goto(url, wait_until="networkidle", timeout=30000)
            h = pg.content()
            b.close()
            return h
    except Exception:
        return ""


def resolve(name: str, domain: str | None = None, wikidata: str | None = None, refresh: bool = False,
            probe: bool = True, render: bool = True) -> dict:
    key = cname(name) or norm(name)
    if not refresh:
        hit = _cache().get(key)
        if hit and hit.get("_at", "") >= (dt.datetime.now() - dt.timedelta(days=14)).isoformat():
            return hit
    info = {"name": name, "domain": domain, "website": None, "description": None, "employees": None, "founded": None,
            "wikidata": wikidata, "boards": [], "careers_url": None, "jsonld": [], "notes": []}
    for c in watchlist() + [dict(b, company=b["company"]) for b in config.cfg("companies").get("boards", []) or []]:
        if same_company(c["company"], name):
            info["boards"].append({k: v for k, v in c.items() if k not in ("company", "checked", "added")})
    for d in directory():
        if same_company(d["name"], name) or any(same_company(a, name) for a in d.get("aliases", [])):
            info["domain"] = info["domain"] or d.get("domain")
            info["careers_url"] = d.get("careers")
    if wikidata:
        w = wikidata_details(wikidata)
        info.update({k: v for k, v in w.items() if v and k != "name"})
    website = info.get("website") or (f"https://{info['domain']}" if info.get("domain") else None)
    info["website"] = website
    if not info["boards"] and (website or info.get("careers_url")):
        urls = ([info["careers_url"]] if info.get("careers_url") else []) + (careers_pages(website) if website else [])
        for u in urls:
            try:
                r = _get(u, headers={"Accept": "text/html"}, timeout=7)
            except requests.RequestException:
                continue
            if r.status_code >= 400 or "html" not in r.headers.get("content-type", "html"):
                continue
            boards = ats_more.detect_boards(r.text, r.url)
            ld = ats_more.parse_jsonld(r.text, r.url)
            spa = len(re.findall(r"<a\s", r.text, re.I)) < 15 or re.search(r'id="(?:root|app|__next)"|__NEXT_DATA__|ng-version', r.text)
            if not boards and not ld and render and spa:
                html = _render(r.url)
                boards, ld = ats_more.detect_boards(html, r.url), ats_more.parse_jsonld(html, r.url)
            if boards or ld:
                info["careers_url"] = r.url
                info["boards"] += boards
                info["jsonld"] = ld
                break
    if not info["boards"] and probe:
        jobs = [(k, s) for s in slug_candidates(name, info.get("domain")) for k in PROBE_KINDS]
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            for b in ex.map(lambda ks: _probe(ks[0], ks[1], name), jobs):
                if b and not any(x["type"] == b["type"] for x in info["boards"]):
                    info["boards"].append(b)
        if not info["boards"]:
            info["notes"].append("no public hiring platform found: showing LinkedIn/Naukri/Indeed results only")
    _cache_put(key, info)
    return info


# ── listings ───────────────────────────────────────────────────────────────
def _board_listings(info: dict, errors: list[str]) -> list[dict]:
    out = []
    for b in info.get("boards", []):
        try:
            rows = ats_more.fetch(b, _get, _post, info["name"])
            for r in rows:
                r["company"] = r.get("company") or info["name"]
            out += rows
        except Exception as exc:
            errors.append(f"{b['type']} {b.get('token') or b.get('host')}: {type(exc).__name__}")
    return out + list(info.get("jsonld") or [])


def _boards_search(name: str, errors: list[str], sites=("linkedin", "naukri", "indeed")) -> list[dict]:
    try:
        from jobspy import scrape_jobs
    except ImportError:
        return []
    from .sources.boards import row_to_job
    out = []
    for site in sites:
        try:
            df = scrape_jobs(site_name=[site], search_term=f'"{name}"' if site == "indeed" else name, location="India",
                             results_wanted=40, hours_old=24 * 30, country_indeed="India", verbose=0,
                             linkedin_fetch_description=False, description_format="markdown")
        except Exception as exc:
            errors.append(f"{site}: {type(exc).__name__}")
            continue
        for rec in (df.to_dict("records") if df is not None else []):
            j = row_to_job(rec)
            if j and same_company(j.company, name):
                out.append(ats_more.L(title=j.title, company=j.company, location=j.location, url=j.url,
                                      posted=j.posted_at, job_type=j.job_type, remote=j.is_remote,
                                      description=j.description, source=site, pay_min=j.pay_min, pay_max=j.pay_max,
                                      currency=j.currency, pay_period=j.pay_period, pay_estimate=j.pay_estimate))
    return out


def to_job(l: dict) -> Job:
    return Job(title=l["title"], company=l["company"], location=l.get("location") or "", url=l.get("apply_url") or l["url"],
               source=l.get("source") or "company", description=l.get("description") or "", posted_at=l.get("posted"),
               job_type=l.get("job_type"), is_remote=bool(l.get("remote")), pay_min=l.get("pay_min"),
               pay_max=l.get("pay_max"), currency=l.get("currency"), pay_period=l.get("pay_period"),
               pay_estimate=l.get("pay_estimate"), deadline=l.get("deadline"))


def listings(info: dict, boards_search: bool = True, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    errors: list[str] = []
    rows = _board_listings(info, errors)
    if boards_search:
        rows += _boards_search(info["name"], errors)
    db = DB()
    for j in db.query(kind="private", limit=500, order="found_at DESC"):
        if same_company(j["company"], info["name"]):
            rows.append(ats_more.L(title=j["title"], company=j["company"], location=j["location"], url=j["url"],
                                   posted=j.get("posted_at") or j["found_at"][:10], job_type=j.get("job_type"),
                                   remote=j.get("is_remote"), description=j.get("description") or "",
                                   source=",".join(j.get("sources") or []) or "db"))
    merged: dict[str, dict] = {}
    for r in rows:
        if not r.get("title"):
            continue
        k = job_id(r["company"] or info["name"], r["title"], r.get("location"))
        if k in merged:
            m = merged[k]
            m["sources"] = sorted(set(m["sources"]) | {r["source"]})
            if r["source"] not in ("linkedin", "naukri", "indeed", "db") and r.get("url"):
                m["apply_url"] = r.get("apply_url") or r["url"]       # prefer the company's own link
            m["posted"] = min(x for x in [m.get("posted"), r.get("posted")] if x) if (m.get("posted") or r.get("posted")) else None
            if len(r.get("description") or "") > len(m.get("description") or ""):
                m["description"] = r["description"]
            continue
        r = dict(r, id=k, sources=[r["source"]])
        merged[k] = r
    out = []
    for r in merged.values():
        j = score(to_job(r), today)
        cur = db.get(r["id"])
        days = (today - dt.date.fromisoformat(r["posted"])).days if r.get("posted") else None
        out.append(dict(r, score=j.score if j.status != "filtered" else 0, fit=j.status != "filtered",
                        why=(j.reasons or [""])[0], karnataka=bool(re.search(
                            "|".join(map(re.escape, config.cfg("search").get("karnataka_places", ["karnataka"]))),
                            (r.get("location") or "").lower())) or (r.get("remote") and "india" in (r.get("location") or "india").lower()),
                        status=cur["status"] if cur else None, days_ago=days, hot=days is not None and days <= 1,
                        description=(r.get("description") or "")[:1500]))
    out.sort(key=lambda r: (r.get("posted") or "0000"), reverse=True)
    return {"company": info, "listings": out, "errors": errors, "at": iso(),
            "counts": {"total": len(out), "karnataka": sum(1 for r in out if r["karnataka"]),
                       "fit": sum(1 for r in out if r["fit"]), "new_24h": sum(1 for r in out if r["hot"])}}


def track(listing_rows: list[dict]) -> list[str]:
    """Put chosen listings into the job database (scored) so `hunt apply` / Review Center can use them."""
    db = DB()
    ids = []
    for r in listing_rows:
        j = score(to_job(r))
        if j.status == "filtered":           # you chose it yourself: keep it, but say why it's outside your rules
            j.status, j.reasons = "new", [f"chosen manually ({j.reasons[0] if j.reasons else 'outside filters'})"]
            j.score = max(j.score, 40)
        db.upsert(j)
        db.set_score(j.id, j.score, j.reasons, j.flags, j.dream, "new")
        db.set_status(j.id, "queued", "picked in company search")
        ids.append(j.id)
    return ids


# ── referral & research links (no scraping of people: links you open yourself) ──
def links(info: dict) -> dict:
    n = info["name"]
    q = quote_plus
    prof = config.cfg("profile")
    college = next((e.get("institution", "").split(",")[0] for e in prof.get("education", [])
                    if e.get("level") == "bachelors"), "REVA University")
    return {
        "Alumni from your college there (best referral source)":
            f"https://www.linkedin.com/search/results/people/?keywords={q(college + ' ' + n)}",
        "Recruiters / talent team": f"https://www.linkedin.com/search/results/people/?keywords={q(n + ' recruiter India')}",
        "Engineers in Bengaluru": f"https://www.linkedin.com/search/results/people/?keywords={q(n + ' engineer Bengaluru')}",
        "LinkedIn company page": f"https://www.linkedin.com/search/results/companies/?keywords={q(n)}",
        "AmbitionBox reviews & fresher salaries": f"https://www.ambitionbox.com/search?CompanyName={q(n)}",
        "Glassdoor interviews": f"https://www.glassdoor.co.in/Search/results.htm?keyword={q(n)}",
        "Interview questions (Google)": f"https://www.google.com/search?q={q(n + ' interview questions fresher software engineer')}",
        "Careers page": info.get("careers_url") or (info.get("website") or ""),
    }


def watch(info: dict) -> bool:
    """Add the company's boards (or a careers-page watcher) to config/watchlist.yaml."""
    f = watchlist_path()
    data = (yaml.safe_load(f.read_text(encoding="utf-8")) if f.is_file() else None) or {"companies": []}
    have = data.setdefault("companies", [])
    added = False
    for b in info.get("boards") or []:
        row = {"company": info["name"], **{k: v for k, v in b.items() if k in ("type", "token", "host", "tenant", "site")},
               "added": dt.date.today().isoformat()}
        if not any(same_company(x["company"], info["name"]) and x.get("type") == row["type"] for x in have):
            have.append(row)
            added = True
    if not info.get("boards") and info.get("careers_url"):
        if not any(same_company(x["company"], info["name"]) for x in have):
            have.append({"company": info["name"], "type": "page", "url": info["careers_url"],
                         "added": dt.date.today().isoformat()})
            added = True
    if added:
        f.write_text("# Companies you chose in company search. Every scan includes them.\n" +
                     yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return added
