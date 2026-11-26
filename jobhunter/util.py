"""Small shared helpers: logging, text normalisation, polite HTTP, robots.txt."""
from __future__ import annotations

import hashlib
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import urlparse

import requests
from rich.console import Console

console = Console()
log = logging.getLogger("jobhunter")    # best-effort steps that failed: warnings to stderr (journal), debug with JH_LOG=DEBUG
IST = timezone(timedelta(hours=5, minutes=30))
UA = "jobhunter/1.0 (personal job alerts; polite, low-frequency)"


def pay_pair(lo, hi) -> tuple[float | None, float | None]:
    """Listed pay as two numbers: 0 / "Competitive" / "best in industry" → None, min > max → swapped."""
    def num(v):
        try:
            v = float(str(v).replace(",", "")) if v is not None and not isinstance(v, bool) else None
        except ValueError:
            return None
        return v if v and v > 0 else None
    lo, hi = num(lo), num(hi)
    return (hi, lo) if lo and hi and lo > hi else (lo, hi)


def now() -> datetime:
    return datetime.now(IST)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(str(s)[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def norm(text: str | None) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9]+", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def short_hash(*parts: str, n: int = 12) -> str:
    h = hashlib.sha1("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()
    return h[:n]


def canonical_url(url: str | None) -> str:
    if not url:
        return ""
    u = urlparse(url)
    q = "&".join(p for p in (u.query or "").split("&")
                 if p and not p.lower().startswith(("utm_", "ref", "trk", "refid", "trackingid", "src")))
    return f"{u.scheme}://{u.netloc.lower()}{u.path.rstrip('/')}" + (f"?{q}" if q else "")


def clip(s: str | None, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


@lru_cache(maxsize=1)
def ca_bundle() -> str:
    """certifi's roots + the public intermediates in jobhunter/certs. Some govt sites (bel-india.in) send the wrong
    intermediate, so a strict client fails where browsers (which fetch it themselves) work. Verification stays on."""
    import tempfile
    from pathlib import Path

    import certifi
    extra = "".join(p.read_text() for p in sorted((Path(__file__).parent / "certs").glob("*.pem")))
    if not extra:
        return certifi.where()
    f = Path(tempfile.gettempdir()) / f"jobhunter-ca-{hashlib.sha1(extra.encode()).hexdigest()[:8]}.pem"
    if not f.exists():
        tmp = f.with_suffix(f".{time.time_ns()}")
        tmp.write_text(Path(certifi.where()).read_text() + "\n" + extra)
        tmp.replace(f)                              # atomic: a parallel scan never reads half a bundle
    return str(f)


class Http:
    """requests.Session with a polite pause, retries on 429/5xx and robots.txt checks."""

    def __init__(self, pause: float = 1.0, timeout: int = 30, respect_robots: bool = True):
        self.s = requests.Session()
        self.s.verify = ca_bundle()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"})
        self.pause, self.timeout, self.respect_robots = pause, timeout, respect_robots
        self._last: dict[str, float] = {}

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        return robots_allows(url)

    def get(self, url: str, **kw) -> requests.Response:
        host = urlparse(url).netloc
        gap = time.time() - self._last.get(host, 0)
        if gap < self.pause:
            time.sleep(self.pause - gap)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                r = self.s.get(url, timeout=self.timeout, **kw)
                self._last[host] = time.time()
                if r.status_code in (429, 502, 503, 504):
                    time.sleep(2 * (attempt + 1))
                    continue
                return r
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
        if last_exc:
            raise last_exc
        return r  # type: ignore[possibly-undefined]


def parse_robots(text: str, agent: str = "jobhunter") -> list[tuple[bool, str]]:
    """The (allow, pattern) rules that apply to us: the group naming our agent, else the "*" group."""
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents_open = False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if not agents_open:
                groups.append(([], []))
                agents_open = True
            groups[-1][0].append(v.lower())
        elif k in ("allow", "disallow") and groups:
            agents_open = False
            groups[-1][1].append((k == "allow", v))
    mine = [r for a, r in groups if any(x != "*" and x in agent.lower() for x in a)]
    return [x for r in (mine or [r for a, r in groups if "*" in a]) for x in r]


def robots_match(rules: list[tuple[bool, str]], path: str) -> bool:
    """Google's rules: * matches anything, $ ends the URL, the longest matching rule wins, Allow wins a tie."""
    best: tuple[int, bool] | None = None
    for allow, pat in rules:
        if not pat:
            continue                                     # "Disallow:" with nothing = allow everything
        end = pat.endswith("$")
        rx = "^" + re.escape(pat.rstrip("$")).replace(r"\*", ".*") + ("$" if end else "")
        if re.match(rx, path):
            cand = (len(pat), allow)
            if best is None or cand[0] > best[0] or (cand[0] == best[0] and allow):
                best = cand
    return True if best is None else best[1]


@lru_cache(maxsize=256)
def _robots(base: str) -> list[tuple[bool, str]] | None:
    try:
        r = requests.get(base + "/robots.txt", timeout=15, headers={"User-Agent": UA})
        if r.status_code >= 400:
            return None          # no robots.txt → allowed
        return parse_robots(r.text)
    except requests.RequestException:
        return None


def robots_allows(url: str) -> bool:
    u = urlparse(url)
    rules = _robots(f"{u.scheme}://{u.netloc}")
    return True if rules is None else robots_match(rules, (u.path or "/") + (f"?{u.query}" if u.query else ""))


# "Application ID: 12345" · "Reference no. REF-88A" · "Your application number is 4471" · "Requisition #R-203"
_CONF = re.compile(r"\b(?:application|applicant|reference|ref|candidate|requisition|confirmation|tracking|submission)"
                   r"\s*(?:id|no\.?|number|num|#|code)\s*(?:is|:|#|-|–)?\s*[:#]?\s*([A-Z0-9][A-Z0-9_/\-]{2,39})", re.I)


def confirmation_id(text: str | None) -> str:
    """The application / reference / candidate / requisition number a confirmation shows, or "" (never made up)."""
    for m in _CONF.finditer(text or ""):
        v = m.group(1).strip("-/_")
        if re.search(r"\d", v) and len(v) >= 3:
            return v
    return ""
