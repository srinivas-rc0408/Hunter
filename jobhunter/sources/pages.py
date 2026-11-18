"""Page watcher: fetch any page, pull out its links, report the new relevant ones.

Used for career pages without an API (Aditya Birla, Birlasoft) and for every
government notice board. The first visit to a page only records a baseline
(old notices are not news); each later visit reports links never seen before.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..db import DB
from ..util import Http, canonical_url, norm

DATE_PATTERNS = [
    r"(?:last|closing|end)\s*date[^0-9]{0,40}(\d{1,2})[./\-\s](\d{1,2}|[a-z]{3,9})[./\-\s,]*(\d{2,4})",
    r"(?:on or before|till|until|upto|up to)[^0-9]{0,20}(\d{1,2})[./\-\s](\d{1,2}|[a-z]{3,9})[./\-\s,]*(\d{2,4})",
]
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


@dataclass
class Link:
    page: str
    href: str
    text: str
    context: str = ""
    is_pdf: bool = False


def extract_links(html: str, base: str) -> list[Link]:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absu = canonical_url(urljoin(base, href))
        text = " ".join(a.get_text(" ", strip=True).split())
        parent = a.find_parent(["tr", "li", "p", "div", "td"])
        ctx = " ".join(parent.get_text(" ", strip=True).split())[:400] if parent else text
        if not text:
            text = a.get("title") or absu.rsplit("/", 1)[-1]
        key = (absu, text)
        if key in seen:
            continue
        seen.add(key)
        out.append(Link(base, absu, text[:300], ctx, absu.lower().split("?")[0].endswith(".pdf")))
    return out


def relevant(link: Link, include: list[str], exclude: list[str]) -> bool:
    t = " " + norm(link.text + " " + link.context) + " "
    raw = (link.text + " " + link.context).lower()
    def hit(word: str) -> bool:
        w = word.lower()
        if not w.isascii():
            return w in raw
        return f" {norm(w)} " in t
    if any(hit(w) for w in exclude):
        return False
    return any(hit(w) for w in include) if include else True


def find_deadline(text: str) -> str | None:
    t = text.lower()
    for pat in DATE_PATTERNS:
        m = re.search(pat, t)
        if not m:
            continue
        d, mo, y = m.groups()
        mo_i = int(mo) if mo.isdigit() else MONTHS.get(mo[:3])
        if not mo_i:
            continue
        y_i = int(y) + (2000 if len(y) == 2 else 0)
        try:
            return f"{y_i:04d}-{int(mo_i):02d}-{int(d):02d}"
        except ValueError:
            continue
    return None


def watch(db: DB, http: Http, url: str, include: list[str], exclude: list[str],
          errors: list[str], baseline_new: bool = False) -> list[Link]:
    """New relevant links on `url` since the last visit."""
    if not http.allowed(url):
        errors.append(f"{urlparse(url).netloc}: robots.txt disallows bots → check manually: {url}")
        return []
    try:
        r = http.get(url)
    except Exception as exc:
        errors.append(f"{urlparse(url).netloc}: unreachable ({type(exc).__name__})")
        return []
    if r.status_code >= 400:
        errors.append(f"{urlparse(url).netloc}: HTTP {r.status_code}")
        return []
    first = not db.page_known(url)
    fresh = []
    for link in extract_links(r.text, url):
        is_new = db.link_is_new(url, link.href, link.text)
        if is_new and (not first or baseline_new) and relevant(link, include, exclude):
            fresh.append(link)
    if first and not baseline_new:
        errors.append(f"{urlparse(url).netloc}: baseline saved (new notices from the next scan on)")
    return fresh


def pdf_text(http: Http, url: str, max_pages: int = 6) -> str:
    """First pages of a notification PDF as text ('' if anything fails)."""
    try:
        import io

        import pdfplumber
        r = http.get(url)
        if r.status_code >= 400 or b"%PDF" not in r.content[:1024]:
            return ""
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages[:max_pages])
    except Exception:
        return ""
