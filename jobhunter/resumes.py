"""Your own resume PDF: the file every application uploads (config/resume.pdf).

Set it with `hunt resume --use FILE` or by sending the PDF to your Telegram bot.
The previous file is kept in data/resume-history/ (newest 10).
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from . import config

MAX_BYTES = 5 * 1024 * 1024


def check(data: bytes) -> tuple[bool, str, int]:
    """(ok, message, pages): a real PDF, under 5 MB, 1-3 pages, with readable text."""
    if not data.startswith(b"%PDF"):
        return False, "that's not a PDF", 0
    if len(data) > MAX_BYTES:
        return False, f"too big ({len(data) // 1024} KB): most job sites accept up to 5 MB", 0
    try:
        import io

        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        pages = len(r.pages)
        text = "".join((p.extract_text() or "") for p in r.pages[:2])
    except Exception as exc:
        return False, f"the PDF can't be read ({type(exc).__name__})", 0
    if not 1 <= pages <= 3:
        return False, f"{pages} pages: a fresher resume should be 1-2 pages", pages
    if len(text.strip()) < 200:
        return False, "no text inside (a scanned image?): applicant tracking systems can't read it", pages
    return True, "ok", pages


def use(src: Path | None = None, data: bytes | None = None) -> tuple[bool, str]:
    if data is None:
        if not src or not src.is_file():
            return False, f"no such file: {src}"
        data = src.read_bytes()
    ok, why, pages = check(data)
    if not ok:
        return False, why
    dst = config.path("config", "resume.pdf")
    hist = config.data_dir() / "resume-history"
    if dst.is_file():
        if dst.read_bytes() == data:
            return True, f"that's already your resume ({pages} pages)"
        hist.mkdir(exist_ok=True)
        shutil.copyfile(dst, hist / time.strftime("resume-%Y%m%d-%H%M%S.pdf"))
        for old in sorted(hist.glob("resume-*.pdf"))[:-10]:
            old.unlink()
    dst.write_bytes(data)
    s = config.cfg("search").get("apply", {}).get("resume", "own")
    from .apply.runner import my_resume
    up = my_resume()
    return True, (f"saved ({pages} pages, {len(data) // 1024} KB). Every application now uploads it as {up.name}."
                  + ("" if s == "own" else "  Note: config/search.yaml apply.resume is 'tailored': set it to 'own'."))
