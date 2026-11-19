"""Maintenance without Claude: encrypted backups, restore, self-update.

Backups hold everything git doesn't: data/jobs.db, data/answers.yaml, covers,
tailored resumes, config/private.yaml, config/advantages.yaml, docs/private/,
hunter/, and .env. Encrypted with AES-256 (openssl, key = STATE_KEY from .env).
Stored in ~/jobhunter-backups (outside the repo); the newest 8 are kept.
KEEP A COPY OF STATE_KEY OUTSIDE THE LAPTOP (phone notes / password manager):
without it no backup and no cloud state can be decrypted.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from . import config
from .config import env
from .util import console

INCLUDE = ["data/jobs.db", "data/answers.yaml", "data/state.json", "data/covers", "data/resumes",
           "config/private.yaml", "config/advantages.yaml", "config/resume.pdf", "docs/private", "hunter", ".env"]


def backup_dir() -> Path:
    d = Path(env("JOBHUNTER_BACKUP_DIR") or (Path.home() / "jobhunter-backups"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _openssl(args: list[str], key: str) -> None:
    subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", *args, "-pass", "env:JH_KEY"],
                   check=True, capture_output=True, env={**os.environ, "JH_KEY": key})


def backup(keep: int = 8) -> Path:
    key = env("STATE_KEY")
    if not key:
        sys.exit("STATE_KEY missing in .env: run `hunt secrets` once first")
    home = config.home()
    out = backup_dir() / f"jobhunter-{dt.datetime.now():%Y%m%d-%H%M}.tar.gz.enc"
    with tempfile.TemporaryDirectory() as td:
        tar = Path(td) / "b.tar.gz"
        with tarfile.open(tar, "w:gz") as tf:
            for rel in INCLUDE:
                p = home / rel
                if rel == "data/jobs.db" and p.exists():      # a consistent copy even while the bot/scan writes
                    import sqlite3
                    snap = Path(td) / "jobs.db"
                    src, dst = sqlite3.connect(p, timeout=60), sqlite3.connect(snap)
                    try:
                        src.backup(dst)
                    finally:
                        src.close()
                        dst.close()
                    p = snap
                if p.exists():
                    tf.add(p, arcname=rel)
        _openssl(["-salt", "-in", str(tar), "-out", str(out)], key)
    os.chmod(out, 0o600)
    old = sorted(backup_dir().glob("jobhunter-*.tar.gz.enc"))[:-keep]
    for f in old:
        f.unlink()
    console.print(f"backup: {out} ({out.stat().st_size // 1024} KB) · keeping newest {keep}")
    return out


def restore(path: str, key: str | None = None) -> None:
    key = key or env("STATE_KEY")
    if not key:
        sys.exit("Need STATE_KEY (from your phone notes): STATE_KEY=... hunt restore <file>")
    home = config.home()
    with tempfile.TemporaryDirectory() as td:
        tar = Path(td) / "b.tar.gz"
        try:
            _openssl(["-d", "-in", path, "-out", str(tar)], key)
        except subprocess.CalledProcessError:
            sys.exit("Wrong STATE_KEY or damaged backup.")
        with tarfile.open(tar) as tf:
            for m in tf.getmembers():
                if m.name.startswith("/") or ".." in Path(m.name).parts:
                    sys.exit(f"refusing unsafe path in backup: {m.name}")
            if (home / "data" / "jobs.db").exists():
                (home / "data" / "jobs.db").rename(home / "data" / f"jobs.db.before-restore-{dt.datetime.now():%H%M%S}")
            tf.extractall(home, filter="data") if sys.version_info >= (3, 12) else tf.extractall(home)
    console.print(f"restored into {home}. Run `hunt doctor`.")


def update(run_tests: bool = False) -> None:
    """git pull + reinstall deps + refresh Chromium (+ optional tests)."""
    home = str(config.home())
    py = sys.executable
    steps = [["git", "-C", home, "pull", "--ff-only"],
             [py, "-m", "pip", "install", "-q", "-e", home],
             [py, "-m", "playwright", "install", "chromium"]]
    if run_tests:
        steps.append([py, "-m", "unittest", "discover", "-s", "tests", "-t", "tests"])
    for s in steps:
        console.print(f"[dim]$ {' '.join(s[-4:])}[/]")
        r = subprocess.run(s, cwd=home)
        if r.returncode:
            sys.exit(f"step failed ({r.returncode}): fix the error above, then run `hunt update` again")
    console.print("[green]up to date[/]. Restart services: systemctl --user restart jobhunter-review jobhunter-bot")
