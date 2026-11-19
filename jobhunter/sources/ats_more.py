"""More hiring platforms (ATS), incl. the ones Indian companies use most.

Each platform has a pure parser (JSON/HTML in → listings out, unit-tested on real
responses) and a fetch function. A listing is a plain dict:
  {title, company, location, url, apply_url, posted (YYYY-MM-DD|None), job_type,
   remote, description, source, pay_min, pay_max, currency, pay_period}

Platform          public endpoint (no login)                                    seen at
greenhouse/lever/ashby/workday → hunter-scout's tested parsers
smartrecruiters   api.smartrecruiters.com/v1/companies/{id}/postings             Bosch, Visa, IKEA
workable          apply.workable.com/api/v1/widget/accounts/{slug}?details=true  startups
recruitee         {slug}.recruitee.com/api/offers/                               startups
keka              {tenant}.keka.com/careers/api/jobs/default/active              many Indian mid-size firms
darwinbox         POST {tenant}.darwinbox.in/ms/candidateapi/job/alljobs         large Indian employers
zohorecruit       {tenant}.zohorecruit.in/jobs/Careers (JSON in #jobs input)    Indian SMEs
successfactors    {host}/search/?q=&locationsearch=India&startrow=N (server HTML) Birlasoft, many large employers
abg               careers.adityabirla.com/job-search (rendered) → PeopleStrong   Aditya Birla Group (dream)
jsonld            any careers page with schema.org JobPosting                    company sites
"""
from __future__ import annotations

import datetime as dt
import html as _html
import json
import re
from typing import Callable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..util import pay_pair

UA = "JobHunter/1.1 (personal job search; contact via GitHub srinivas-rc0408/Hunter)"


def _text(h: str | None, n: int = 4000) -> str:
    if not h:
        return ""
    t = BeautifulSoup(h, "lxml").get_text(" ", strip=True) if "<" in h else h
    return re.sub(r"\s+", " ", _html.unescape(t)).strip()[:n]


def _day(v) -> str | None:
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)):            # epoch seconds or ms
        v = v / 1000 if v > 1e11 else v
        return dt.datetime.utcfromtimestamp(v).date().isoformat()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(v))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _jtype(*vals) -> str | None:
    t = " ".join(str(v or "") for v in vals).lower()
    if "intern" in t:
        return "internship"
    if "contract" in t or "temporary" in t or "freelance" in t:
        return "contract"
    if "full" in t or "permanent" in t:
        return "fulltime"
    if "part" in t:
        return "parttime"
    return None


def L(**kw) -> dict:
    base = {"title": "", "company": "", "location": "", "url": "", "apply_url": "", "posted": None, "job_type": None,
            "remote": False, "description": "", "source": "", "pay_min": None, "pay_max": None, "currency": None,
            "pay_period": None, "pay_estimate": None, "deadline": None}
    base.update({k: v for k, v in kw.items() if v is not None or k in base})
    base["title"] = (base["title"] or "").strip()
    base["pay_min"], base["pay_max"] = pay_pair(base["pay_min"], base["pay_max"])   # "Competitive" → None
    if not (base["pay_min"] or base["pay_max"]):
        base["currency"] = base["pay_period"] = base["pay_estimate"] = None
    if not base["apply_url"]:
        base["apply_url"] = base["url"]
    return base


# ── parsers ────────────────────────────────────────────────────────────────
def parse_smartrecruiters(data: dict, company: str = "") -> list[dict]:
    out = []
    for p in data.get("content", []):
        loc = p.get("location") or {}
        cid = (p.get("company") or {}).get("identifier", "")
        place = ", ".join(x for x in [str(loc.get("city") or "").title(), loc.get("region") or "",
                                      {"in": "India"}.get(str(loc.get("country", "")).lower(), str(loc.get("country", "")).upper())] if x)
        out.append(L(title=p.get("name"), company=(p.get("company") or {}).get("name") or company, location=place,
                     url=f"https://jobs.smartrecruiters.com/{cid}/{p.get('id')}", posted=_day(p.get("releasedDate")),
                     job_type=_jtype((p.get("typeOfEmployment") or {}).get("label")),
                     remote=bool(loc.get("remote")), source="smartrecruiters",
                     description=f"Experience level: {(p.get('experienceLevel') or {}).get('label', '')}"))
    return out


def parse_workable(data: dict, slug: str, company: str = "") -> list[dict]:
    out = []
    for j in data.get("jobs", []):
        place = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        out.append(L(title=j.get("title"), company=data.get("name") or company, location=place,
                     url=j.get("url") or f"https://apply.workable.com/{slug}/j/{j.get('shortcode')}/",
                     apply_url=j.get("application_url"), posted=_day(j.get("published_on") or j.get("created_at")),
                     job_type=_jtype(j.get("employment_type")), remote=bool(j.get("telecommuting")),
                     description=_text(j.get("description")), source="workable"))
    return out


def parse_recruitee(data: dict, company: str = "") -> list[dict]:
    out = []
    for o in data.get("offers", []):
        out.append(L(title=o.get("title"), company=company or o.get("company_name"),
                     location=o.get("location") or ", ".join(x for x in [o.get("city"), o.get("country")] if x),
                     url=o.get("careers_url"), apply_url=o.get("careers_apply_url"), posted=_day(o.get("published_at")),
                     job_type=_jtype(o.get("employment_type_code")), remote=bool(o.get("remote")),
                     description=_text(o.get("description")), source="recruitee"))
    return out


def parse_keka(data: list, tenant: str, company: str = "") -> list[dict]:
    out = []
    base = f"https://{tenant}.keka.com/careers"
    for p in data if isinstance(data, list) else []:
        locs = p.get("jobLocations") or []
        place = "; ".join(", ".join(x for x in [l.get("city"), l.get("state"), l.get("countryName")] if x) for l in locs)
        sal = p.get("salaryRange") or {}
        out.append(L(title=p.get("title"), company=company, location=place, url=f"{base}/jobdetails/{p.get('id')}",
                     posted=_day(p.get("publishedOn")), job_type={1: "internship", 2: "fulltime", 3: "parttime",
                                                                  4: "contract"}.get(p.get("jobType")) or _jtype(p.get("jobType")),
                     description=(f"Experience: {p['experience']}. " if p.get("experience") else "") + _text(p.get("description")),
                     source="keka", pay_min=sal.get("min") if isinstance(sal, dict) else None,
                     pay_max=sal.get("max") if isinstance(sal, dict) else None,
                     currency=sal.get("currency") if isinstance(sal, dict) else None))
    return out


def parse_darwinbox(data: dict, host: str, company: str = "") -> list[dict]:
    out = []
    for p in data.get("data", []):
        place = ", ".join(p.get("officelocations_without_area") or []) or p.get("country") or ""
        out.append(L(title=p.get("title"), company=company, location=place + (", India" if place and "india" not in place.lower() else ""),
                     url=f"https://{host}/ms/candidatev2/main/careers/jobDetails/{p.get('id')}", posted=_day(p.get("posted_on")),
                     job_type=_jtype(p.get("emp_type_name")), remote=bool(p.get("is_remote")),
                     description=_text(p.get("jd")), source="darwinbox"))
    return out


def parse_zoho(html_text: str, host: str, company: str = "") -> list[dict]:
    soup = BeautifulSoup(html_text, "lxml")
    inp = soup.find(id="jobs")
    if not inp or not inp.get("value"):
        return []
    try:
        rows = json.loads(inp["value"])
    except ValueError:
        return []
    out = []
    for o in rows:
        if o.get("Publish") is False:
            continue
        out.append(L(title=o.get("Posting_Title"), company=company,
                     location=", ".join(x for x in [o.get("City"), o.get("State"), o.get("Country") or o.get("Country1")] if x),
                     url=f"https://{host}/jobs/Careers/{o.get('id')}", posted=_day(o.get("Date_Opened")),
                     job_type=_jtype(o.get("Job_Type")), remote=bool(o.get("Remote_Job")), source="zohorecruit",
                     description=_text(o.get("Job_Description"))))
    return out


def parse_jsonld(html_text: str, page_url: str) -> list[dict]:
    """schema.org JobPosting blocks on any page (many company career sites have them)."""
    soup = BeautifulSoup(html_text, "lxml")
    out = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if not isinstance(it, dict) or "JobPosting" not in str(it.get("@type")):
                continue
            locs = it.get("jobLocation") or []
            locs = locs if isinstance(locs, list) else [locs]
            place = "; ".join(", ".join(x for x in [(l.get("address") or {}).get("addressLocality"),
                                                    (l.get("address") or {}).get("addressRegion"),
                                                    (l.get("address") or {}).get("addressCountry")] if isinstance(x, str) and x)
                              for l in locs if isinstance(l, dict))
            sal = ((it.get("baseSalary") or {}).get("value") or {}) if isinstance(it.get("baseSalary"), dict) else {}
            sal = sal if isinstance(sal, dict) else {"value": sal}          # "value": 50000 or a QuantitativeValue
            unit = str(sal.get("unitText") or "").upper()
            out.append(L(title=it.get("title"), company=(it.get("hiringOrganization") or {}).get("name", ""),
                         location=place, url=urljoin(page_url, it.get("url") or page_url), posted=_day(it.get("datePosted")),
                         job_type=_jtype(it.get("employmentType")), remote="TELECOMMUTE" in str(it.get("jobLocationType", "")),
                         description=_text(it.get("description")) + (f" Apply by {it['validThrough'][:10]}."
                                                                     if it.get("validThrough") else ""),
                         source="careers-site", pay_min=sal.get("minValue") or sal.get("value"),
                         pay_max=sal.get("maxValue") or sal.get("value"),
                         pay_period={"YEAR": "year", "MONTH": "month", "WEEK": "week", "DAY": "day",
                                     "HOUR": "hour"}.get(unit),
                         currency=(it.get("baseSalary") or {}).get("currency") if isinstance(it.get("baseSalary"), dict) else None))
    return out


MONTH = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _human_day(text: str | None) -> str | None:
    """'Nov 7, 2026' / 'Mon Nov 16 2026' / '16 Nov 2026' → '2026-11-07'."""
    t = (text or "").lower()
    m = re.search(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", t) or None
    if m and m.group(1) in MONTH:
        return f"{int(m.group(3)):04d}-{MONTH[m.group(1)]:02d}-{int(m.group(2)):02d}"
    m = re.search(r"(\d{1,2})\s+([a-z]{3})[a-z]*\.?,?\s+(\d{4})", t)
    if m and m.group(2) in MONTH:
        return f"{int(m.group(3)):04d}-{MONTH[m.group(2)]:02d}-{int(m.group(1)):02d}"
    return _day(text)


def parse_successfactors(html_text: str, host: str, company: str = "") -> tuple[list[dict], int | None]:
    """SAP SuccessFactors (jobs2web) search page → (listings, total results)."""
    soup = BeautifulSoup(html_text, "lxml")
    out = []
    for r in soup.select("tr.data-row"):
        a = r.select_one("a.jobTitle-link")
        if not a:
            continue
        loc = r.select_one("td.colLocation .jobLocation") or r.select_one(".jobLocation")
        date = r.select_one("td.colDate .jobDate") or r.select_one(".jobDate")
        dept = r.select_one(".jobDepartment")
        place = re.sub(r"\s+", " ", loc.get_text(" ", strip=True)) if loc else ""
        place = re.sub(r",\s*IN$", ", India", place.title().replace("India - ", "").replace(" In", " IN"))
        out.append(L(title=a.get_text(" ", strip=True), company=company, location=place,
                     url=urljoin(f"https://{host}/", a.get("href", "")), posted=_human_day(date.get_text() if date else None),
                     description=f"Department: {dept.get_text(strip=True)}" if dept and dept.get_text(strip=True) else "",
                     source="successfactors"))
    lab = soup.select_one(".paginationLabel")
    total = None
    if lab:
        m = re.search(r"of\s+([\d,]+)", lab.get_text(" ", strip=True))
        total = int(m.group(1).replace(",", "")) if m else None
    return out, total


def parse_abg_cards(html_text: str) -> list[dict]:
    """Aditya Birla Group careers search (rendered) → listings; apply goes to PeopleStrong."""
    soup = BeautifulSoup(html_text, "lxml")
    out = []
    for c in soup.select("div.result-card"):
        a = c.select_one(".job-title a")
        if not a:
            continue
        code = (c.select_one(".apply-info p") or a).get_text(strip=True).lstrip("#")
        if not re.match(r"^[A-Z]{2,5}\d+$", code):
            code = a.get("href", "").rstrip("/").rsplit("/", 1)[-1]
        data = {re.sub(r":$", "", (p.find("span").get_text(strip=True) if p.find("span") else "")).lower():
                p.get_text(" ", strip=True).split(":", 1)[-1].strip() for p in c.select(".job-data p")}
        exp = data.get("experience", "")
        place = (c.select_one(".job-location") or a).get_text(" ", strip=True)
        posted = c.select_one(".job-date")
        out.append(L(title=a.get_text(" ", strip=True), company="Aditya Birla Group", location=place,
                     url=urljoin("https://careers.adityabirla.com/", a.get("href", "")),
                     apply_url=f"https://abgcareers.peoplestrong.com/job/detail/{code}",
                     posted=_human_day(posted.get_text(" ", strip=True) if posted else None),
                     job_type=_jtype(exp), source="aditya-birla",
                     description="; ".join(f"{k.title()}: {v}" for k, v in data.items() if v)))
    return out


def parse_abg_detail(html_text: str) -> dict:
    """One ABG job page: the server-rendered JSON with closing date and full description."""
    blob = ""
    for m in re.finditer(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html_text, re.S):
        try:
            blob += json.loads('"' + m.group(1) + '"')
        except ValueError:
            continue
    i = blob.find('"jobCode"')
    if i < 0:
        return {}
    start = blob.rfind('{"id":', 0, i)
    try:
        obj, _ = json.JSONDecoder().raw_decode(blob[start:])
    except ValueError:
        return {}
    desc = obj.get("jobDescription") or ""
    if isinstance(desc, str) and desc.startswith("$"):       # RSC reference: the text chunk right after "<ref>:T<len>,"
        ref = desc[1:]
        m = re.search(rf"(?:^|\n){re.escape(ref)}:T([0-9a-f]+),", blob)
        # T<hex> is the chunk's length in UTF-8 bytes
        desc = blob[m.end():].encode("utf-8")[: int(m.group(1), 16)].decode("utf-8", "ignore") if m else ""
    return {"title": obj.get("jobTitle"), "code": obj.get("jobCode"), "posted": _day(obj.get("jobPostedDate")),
            "closes": _day(obj.get("jobClosureDate")), "apply_url": obj.get("jobDetailUrl"),
            "location": (obj.get("locationHierarchyComplete") or "").replace(">", ", "),
            "experience": obj.get("expRange"), "org": (obj.get("organizationUnitComplete") or "").replace(">", " › "),
            "description": _text(desc)}


def render_html(url: str, load_more: int = 15) -> str:
    """Rendered HTML of a JavaScript careers page (Playwright). Clicks 'Load more'/'Next' a few
    times so long lists come through. Returns '' if Playwright/Chromium isn't available."""
    import os
    if os.environ.get("JOBHUNTER_NO_BROWSER"):
        return ""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36")
            pg.goto(url, wait_until="networkidle", timeout=45000)
            html = pg.content()
            for _ in range(load_more):
                btn = pg.locator("button:has-text('Load More'), button:has-text('Load more'), button:has-text('View More'), "
                                 "button:has-text('Show More'), a:has-text('Load More')").first
                if not btn.count() or not btn.is_visible():
                    break
                btn.click()
                pg.wait_for_timeout(1800)
                new = pg.content()
                if len(new) <= len(html):
                    break
                html = new
            b.close()
            return html
    except Exception:
        return ""


# ── detection: which ATS does a careers page link to? ──────────────────────
DETECT = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("workday", re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)")),
    ("recruitee", re.compile(r"([a-z0-9-]+)\.recruitee\.com")),
    ("keka", re.compile(r"([a-z0-9-]+)\.keka\.com/careers")),
    ("darwinbox", re.compile(r"([a-z0-9-]+\.darwinbox\.(?:in|com))")),
    ("zohorecruit", re.compile(r"([a-z0-9-]+\.zohorecruit\.(?:in|com))")),
]
SKIP_TOKENS = {"embed", "api", "www", "jobs", "careers", "static", "assets", "cdn", "js", "v1"}


def detect_boards(html_text: str, page_url: str = "") -> list[dict]:
    found, seen = [], set()
    host = re.sub(r"^https?://", "", page_url or (html_text or "").split(" ", 1)[0]).split("/")[0].lower()
    if "careers.adityabirla.com" in (html_text or "") or host == "careers.adityabirla.com":
        found.append({"type": "abg", "url": "https://careers.adityabirla.com/job-search", "token": "abg"})
        seen.add(("abg", "abg", None))
    if re.search(r"rmkcdn\.successfactors\.com|jobs2web|class=\"jobTitle-link\"", html_text or "") and host:
        found.append({"type": "successfactors", "host": host, "token": host.split(".")[1] if host.count(".") >= 2 else host})
        seen.add(("successfactors", found[-1]["token"], host))
    for kind, rx in DETECT:
        for m in rx.finditer(html_text or ""):
            if kind == "workday":
                b = {"type": "workday", "tenant": m.group(1), "host": f"{m.group(1)}.{m.group(2)}.myworkdayjobs.com",
                     "site": m.group(3), "token": m.group(1)}
            elif kind in ("darwinbox", "zohorecruit"):
                h = m.group(1).lower()
                if kind == "zohorecruit" and h.split(".")[0] in SKIP_TOKENS and host:
                    h = host                  # "careers.zohorecruit.com/…/rss" on careers.zohocorp.com: the board is this page
                b = {"type": kind, "host": h, "token": h.split(".")[0]}
            else:
                tok = m.group(1).strip(".")
                if tok.lower() in SKIP_TOKENS:
                    continue
                b = {"type": kind, "token": tok}
            key = (b["type"], b.get("token"), b.get("host"))
            if key not in seen:
                seen.add(key)
                found.append(b)
    return found


# ── fetching (live) ────────────────────────────────────────────────────────
def fetch(board: dict, http_get: Callable, http_post: Callable, company: str = "") -> list[dict]:
    """Live listings for one board. http_get(url) -> requests.Response; http_post(url, json) -> Response."""
    t = board["type"]
    tok = board.get("token")
    if t == "smartrecruiters":
        out, off = [], 0
        while off < 500:
            r = http_get(f"https://api.smartrecruiters.com/v1/companies/{tok}/postings?limit=100&offset={off}")
            if r.status_code != 200:
                break
            d = r.json()
            out += parse_smartrecruiters(d, company)
            off += 100
            if off >= d.get("totalFound", 0):
                break
        return out
    if t == "workable":
        r = http_get(f"https://apply.workable.com/api/v1/widget/accounts/{tok}?details=true")
        return parse_workable(r.json(), tok, company) if r.status_code == 200 else []
    if t == "recruitee":
        r = http_get(f"https://{tok}.recruitee.com/api/offers/")
        return parse_recruitee(r.json(), company) if r.status_code == 200 else []
    if t == "keka":
        r = http_get(f"https://{tok}.keka.com/careers/api/jobs/default/active")
        return parse_keka(r.json(), tok, company) if r.status_code == 200 else []
    if t == "darwinbox":
        out = []
        for page in range(1, 11):
            r = http_post(f"https://{board['host']}/ms/candidateapi/job/alljobs", {"page": page, "limit": 50})
            if r.status_code != 200:
                break
            d = r.json()
            rows = parse_darwinbox(d, board["host"], company)
            if not rows:
                break
            out += rows
            if len(out) >= int(d.get("job_counts") or 0):
                break
        return out
    if t == "zohorecruit":
        r = http_get(f"https://{board['host']}/jobs/Careers")
        return parse_zoho(r.text, board["host"], company) if r.status_code == 200 else []
    if t == "successfactors":
        out, start, total = [], 0, None
        loc = board.get("location", "India")
        while start < board.get("max_rows", 400):
            r = http_get(f"https://{board['host']}/search/?q=&locationsearch={loc}&startrow={start}")
            if r.status_code != 200:
                break
            rows, tot = parse_successfactors(r.text, board["host"], company)
            total = total or tot
            out += rows
            start += 25
            if not rows or (total and start >= total):
                break
        return out
    if t == "abg":
        html = render_html(board.get("url", "https://careers.adityabirla.com/job-search"))
        rows = parse_abg_cards(html)
        for r in rows[: board.get("details", 25)]:       # closing date + full description (server-rendered pages)
            try:
                d = parse_abg_detail(http_get(r["url"]).text)
            except Exception:
                d = {}
            if d:
                r["description"] = (r["description"] + ". " if r["description"] else "") + (d.get("description") or "")
                r["apply_url"] = d.get("apply_url") or r["apply_url"]
                r["deadline"] = d.get("closes")
                r["posted"] = d.get("posted") or r["posted"]
        return rows
    if t == "workday":
        from .ats import scout
        sc = scout()
        out, seen = [], set()
        url = f"https://{board['host']}/wday/cxs/{board['tenant']}/{board['site']}/jobs"
        for off in range(0, 200, 20):
            r = http_post(url, {"appliedFacets": {}, "limit": 20, "offset": off, "searchText": ""})
            if r.status_code != 200:
                break
            rows = sc.from_workday(board, r.json(), dt.date.today())
            if not rows:
                break
            for j in rows:
                if j["id"] not in seen:
                    seen.add(j["id"])
                    out.append(_from_scout(j))
        return out
    if t in ("greenhouse", "lever", "ashby"):
        from .ats import scout
        sc = scout()
        f = sc.Fetcher(pause=0.3)
        b = {"company": company or tok, "type": t, "token": tok}
        return [_from_scout(j) for j in sc.fetch_board(f, b, dt.date.today(), ["engineer", "intern", "data", "ai"], details=6)]
    return []


def _from_scout(j: dict) -> dict:
    pay = j.get("pay") or {}
    return L(title=j["title"], company=j.get("company", ""), location=j.get("location") or "", url=j.get("url") or "",
             posted=j.get("posted"), job_type=_jtype(j["title"]), remote=bool(j.get("remote")),
             description=j.get("_text") or "", source=j.get("source") or "ats", pay_min=pay.get("min"),
             pay_max=pay.get("max"), currency=pay.get("currency"), pay_period=pay.get("period"))
