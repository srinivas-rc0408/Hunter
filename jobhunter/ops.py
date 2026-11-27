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
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from . import config
from .config import env
from .util import console

INCLUDE = ["data/jobs.db", "data/answers.yaml", "data/state.json", "data/covers", "data/resumes",
           "config/private.yaml", "config/advantages.yaml", "config/resume.pdf", "docs/private", "hunter", ".env"]


def backup_dir() -> Path:
    d = Path(env("JOBHUNTER_BACKUP_DIR") or (Path.home() / "jobhunter-backups"))
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


SECRET_FILES = (".env", "config/private.yaml")        # your keys and personal facts
SECRET_GLOBS = ("*.db", "*.db-wal", "*.db-shm", "*.db-journal", "*.enc", "answers.yaml", "questions.yaml", "state.json")
NEVER_COMMIT = (".env", "config/private.yaml", "data/", "docs/private/")


def _secret_paths() -> list[Path]:
    home, data = config.home(), config.data_dir()
    found = [home / f for f in SECRET_FILES if (home / f).is_file()]
    return found + sorted({p for g in SECRET_GLOBS for p in data.glob(g) if p.is_file()})


def secure() -> list[str]:
    """chmod 600 the secrets (.env, private.yaml, the databases and their journals, answers, encrypted state),
    700 data/ (browser logins, resumes, covers) and the backup folder. Returns what it had to fix."""
    fixed = []
    for p, mode in [(config.data_dir(), 0o700), (backup_dir(), 0o700)] + [(p, 0o600) for p in _secret_paths()] + \
            [(b, 0o600) for b in backup_dir().glob("jobhunter-*.tar.gz.enc")]:
        try:
            if p.stat().st_mode & 0o777 != mode:
                p.chmod(mode)
                fixed.append(f"{p.name} → {mode:o}")
        except OSError:                          # not ours (a shared mount): reported by secrets_problems
            continue
    return fixed


def _git(*args: str) -> str | None:
    """git output in the JobHunter folder, or None when it isn't a git checkout (the cloud runner's copy, a zip)."""
    try:
        r = subprocess.run(["git", "-C", str(config.home()), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def secrets_problems() -> list[str]:
    """Everything wrong with how secrets are kept: readable by others, tracked or ever committed, a backup
    that isn't encrypted. Empty = all good."""
    out = []
    for p in _secret_paths() + [config.data_dir()]:
        mode = p.stat().st_mode & 0o777
        if mode & 0o077:
            out.append(f"{p.relative_to(config.home()) if p.is_relative_to(config.home()) else p} is {mode:o} (others can read it)")
    tracked = _git("ls-files", "--", *NEVER_COMMIT)
    if tracked is not None:
        bad = [f for f in tracked.split() if f != "data/.gitkeep"]
        if bad:
            out.append("tracked by git: " + ", ".join(bad[:5]))
        hist = _git("log", "--all", "--format=", "--name-only", "--", *NEVER_COMMIT) or ""
        ever = sorted({f for f in hist.split() if f != "data/.gitkeep"})
        if ever:
            out.append("in git history (rewrite it, then rotate those keys): " + ", ".join(ever[:5]))
    for b in backup_dir().glob("jobhunter-*.tar.gz.enc"):
        with open(b, "rb") as fh:
            if fh.read(8) != b"Salted__":         # openssl enc -salt output always starts with it
                out.append(f"backup {b.name} is not encrypted")
    return out


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
        old_mask = os.umask(0o077)              # the encrypted file is owner-only from its first byte
        try:
            _openssl(["-salt", "-in", str(tar), "-out", str(out)], key)
        finally:
            os.umask(old_mask)
    os.chmod(out, 0o600)
    old = sorted(backup_dir().glob("jobhunter-*.tar.gz.enc"))[:-keep]
    for f in old:
        f.unlink()
    console.print(f"backup: {out} ({out.stat().st_size // 1024} KB) · keeping newest {keep}")
    return out


def tidy(db) -> int:
    """Weekly: delete data/screens files older than 7 days (sent applications kept), data/logs files
    older than 14 days, and VACUUM jobs.db. Returns how many screen files went."""
    from .apply.runner import prune_screens
    n = prune_screens(db)
    cutoff = time.time() - 14 * 86400
    for f in (config.data_dir() / "logs").glob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
    db.c.commit()                 # VACUUM fails inside an open transaction
    db.c.execute("VACUUM")
    return n


REPO = Path(__file__).resolve().parent.parent
REQ_FILES = (REPO / "requirements.txt", REPO / "requirements-radar.txt", REPO / "pyproject.toml")


def _vtuple(v: str) -> tuple:
    # ponytail: numeric parts only, pre-release tags ignored (PyPI's info.version is the latest stable)
    return tuple(int(x) for x in re.findall(r"\d+", v))


def update_check(packages=("python-jobspy", "playwright"), run=subprocess.run, req_files=REQ_FILES) -> dict:
    """Try each newer library in a throwaway --target dir against both test suites; install it and
    rewrite its pin only if they pass, so an upstream release can never break scans."""
    from importlib.metadata import version
    import requests
    py, out = sys.executable, {}
    for pkg in packages:
        r = out[pkg] = {"installed": version(pkg), "latest": None, "action": "up to date"}
        try:
            r["latest"] = requests.get(f"https://pypi.org/pypi/{pkg}/json", timeout=15).json()["info"]["version"]
            if _vtuple(r["latest"]) <= _vtuple(r["installed"]):
                continue
            spec = f"{pkg}=={r['latest']}"
            with tempfile.TemporaryDirectory(prefix="jh-upd-") as td:
                run([py, "-m", "pip", "install", "-q", "--target", td, spec], check=True)
                env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [td, str(REPO), os.environ.get("PYTHONPATH")]))}
                if pkg == "playwright":           # a new playwright needs its own Chromium build for the tests
                    run([py, "-m", "playwright", "install", "chromium"], env=env, check=True)
                failed = next((t for t in (run([py, "-m", "unittest", "discover"], cwd=REPO / "tests", env=env,
                                               capture_output=True, text=True),
                                           run([py, "-m", "unittest", "discover", "-s", "tests/hunter"], cwd=REPO,
                                               env=env, capture_output=True, text=True)) if t.returncode), None)
            if failed:
                tail = (failed.stderr or failed.stdout or "").strip().splitlines()
                r["action"] = f"kept (tests failed: {tail[-1] if tail else failed.returncode})"
                continue
            run([py, "-m", "pip", "install", "-q", spec], check=True)
            if pkg == "playwright":
                run([py, "-m", "playwright", "install", "chromium"], check=True)
            pin = re.compile(rf"(?im)^(\s*\"?){re.escape(pkg)}==[^\s\",]+")
            for f in map(Path, req_files):
                if f.exists():
                    f.write_text(pin.sub(lambda m: f"{m[1]}{spec}", f.read_text()))
            r["action"] = "upgraded"
        except Exception as e:  # noqa: BLE001 - one bad package must not stop the report
            r["action"] = f"error: {e}"
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
