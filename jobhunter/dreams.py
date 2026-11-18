"""Dream companies: add or remove one with a single command.

  hunt dream add "Zoho"        hunt dream remove "Zoho"        hunt dream list
  Telegram: /dream Zoho        /undream Zoho                   /dreams

A dream company gets +15 score, alerts at a lower bar, jumps the apply queue, and its
careers site is watched directly. The list lives in config/profile.yaml (it's public: company
names only), so the change is committed and pushed for the cloud radar too.
"""
from __future__ import annotations

import re
import subprocess

import yaml

from . import config
from .util import norm


def names() -> list[str]:
    return list(config.cfg("profile").get("dream_companies") or [])


def find(name: str) -> str | None:
    from .score import is_dream
    return is_dream(name) or next((d for d in names() if norm(d) == norm(name)), None)


def _profile_file():
    return config.path("config", "profile.yaml")


def _item(name: str) -> str:
    return "  " + yaml.safe_dump([name], allow_unicode=True, width=200).strip()


def add(name: str) -> tuple[bool, str]:
    name = " ".join((name or "").split())
    if len(name) < 2:
        return False, "type a company name"
    hit = find(name)
    if hit:
        return False, f"{hit} is already a dream company"
    f = _profile_file()
    lines = f.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("dream_companies:"))
    except StopIteration:
        lines += ["dream_companies:"]
        start = len(lines) - 1
    last = start
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("  - "):
            last = i
        elif lines[i].strip() and not lines[i].startswith("  "):
            break
    lines.insert(last + 1, _item(name))
    _write(lines)
    return True, f"★ {name} is now a dream company"


def remove(name: str) -> tuple[bool, str]:
    hit = next((d for d in names() if norm(d) == norm(name)), None) or find(name)
    if not hit:
        return False, f"{name} isn't in your dream list ({', '.join(names()) or 'empty'})"
    lines = _profile_file().read_text(encoding="utf-8").splitlines()
    out, skip_alias, section = [], False, ""
    for l in lines:
        if l and not l[0].isspace() and not l.startswith("#"):
            section = l.split(":")[0].strip()
        item = re.match(r"^  - (.+?)\s*(#.*)?$", l)
        if section == "dream_companies" and item and norm(str(yaml.safe_load(item.group(1)) or "")) == norm(hit):
            continue
        alias = re.match(r"^  ([^\s#-][^:]*):", l)
        if section == "dream_aliases" and alias and norm(alias.group(1).strip("'\"")) == norm(hit):
            skip_alias = True
            continue
        if skip_alias and l.startswith("    "):
            continue
        skip_alias = False
        out.append(l)
    _write(out)
    return True, f"{hit} removed from your dream companies"


def _write(lines: list[str]) -> None:
    f = _profile_file()
    text = "\n".join(lines) + "\n"
    yaml.safe_load(text)                       # never save a broken file
    f.write_text(text, encoding="utf-8")
    config.reload()


def after_change(name: str, added: bool, watch: bool = True, push: bool = True) -> list[str]:
    """Re-score open jobs, watch the company's careers site, and push so the cloud radar knows."""
    notes = []
    from .rescore import rescore
    r = rescore()
    notes.append(f"re-scored {r['total']} open jobs")
    if added and watch:
        try:
            from . import companies
            info = companies.resolve(name)
            notes.append("careers site watched on every scan" if companies.watch(info)
                         else "no careers platform found to watch (LinkedIn/Naukri still covered)")
        except Exception as exc:
            notes.append(f"couldn't look up its careers site ({type(exc).__name__})")
    if push:
        notes.append(git_push(f"dream companies: {'add' if added else 'remove'} {name}"))
    return notes


def git_push(msg: str) -> str:
    home = str(config.home())
    try:
        if subprocess.run(["git", "-C", home, "rev-parse"], capture_output=True).returncode:
            return "not a git repo: the cloud radar won't see it"
        subprocess.run(["git", "-C", home, "add", "config/profile.yaml"], check=True, capture_output=True)
        c = subprocess.run(["git", "-C", home, "commit", "-q", "-m", msg, "--", "config/profile.yaml"],
                           capture_output=True, text=True)
        if c.returncode and "nothing to commit" not in (c.stdout + c.stderr):
            return "saved here; git commit failed: run `git push` yourself"
        p = subprocess.run(["git", "-C", home, "push", "-q"], capture_output=True, text=True, timeout=60)
        return "pushed: the cloud radar uses it from its next run" if p.returncode == 0 else \
            "saved and committed here; push failed (offline?): run `git push` later"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"saved here; git failed ({type(exc).__name__}): run `git push` later"
