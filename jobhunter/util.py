"""Small shared helpers: logging, text normalisation, polite HTTP, robots.txt."""
from __future__ import annotations

import hashlib
import re
import time
import unicodedata
import urllib.robotparser
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import urlparse

import requests
from rich.console import Console

console = Console()
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


class Http:
    """requests.Session with a polite pause, retries on 429/5xx and robots.txt checks."""

    def __init__(self, pause: float = 1.0, timeout: int = 30, respect_robots: bool = True):
        self.s = requests.Session()
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


@lru_cache(maxsize=256)
def _robots(base: str) -> urllib.robotparser.RobotFileParser | None:
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = requests.get(base + "/robots.txt", timeout=15, headers={"User-Agent": UA})
        if r.status_code >= 400:
            return None          # no robots.txt → allowed
        rp.parse(r.text.splitlines())
        return rp
    except requests.RequestException:
        return None


def robots_allows(url: str) -> bool:
    u = urlparse(url)
    rp = _robots(f"{u.scheme}://{u.netloc}")
    return True if rp is None else rp.can_fetch(UA, url)
