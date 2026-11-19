"""Self-health: the checks behind `hunt doctor` and /health, alerts that arrive by themselves, the weekly report.

Each check is (ok, what, exact fix command). `watch()` runs every 30 min (jobhunter-health.timer) and sends a
Telegram alert the first time a problem appears, then nothing until it is fixed, then one "✅ fixed" note.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import shutil
import subprocess
import time
from collections import Counter

from . import config
from .config import env, get
from .notify import telegram

UNITS = ("jobhunter-bot.service", "jobhunter-review.service", "jobhunter-scan.service", "jobhunter-sync.service",
         "jobhunter-morning.service", "jobhunter-backup.service")
ALWAYS_ON = ("jobhunter-review.service", "jobhunter-bot.service")
TIMERS = ("jobhunter-sync.timer", "jobhunter-scan.timer", "jobhunter-morning.timer", "jobhunter-backup.timer",
          "jobhunter-health.timer", "jobhunter-weekly.timer")
MIN_FREE_GB = 2


def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


# ── flags other parts of the bot raise (e.g. "LinkedIn shows you as logged out") ──
def _flags_file():
    return config.data_dir() / "health.json"


def _load() -> dict:
    try:
        return json.loads(_flags_file().read_text())
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    try:
        _flags_file().write_text(json.dumps(st, indent=1))
    except OSError:
        pass


def set_flag(name: str, on: bool) -> None:
    st = _load()
    flags = st.setdefault("flags", {})
    if on:
        flags.setdefault(name, time.time())
    else:
        flags.pop(name, None)
    _save(st)


# ── the checks (hunt doctor + /health) ──
def _unit(unit: str) -> tuple[str, int]:
    """(active state, restarts since it was started)."""
    try:
        out = subprocess.run(["systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "NRestarts"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return "unknown", 0
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return kv.get("ActiveState", "unknown"), int(kv.get("NRestarts") or 0)


def checks(live: bool = True) -> list[tuple[bool, str, str]]:
    """Every check, in plain words. live=False reads the cloud radar result the watcher saved (fast, for /health)."""
    from . import llm
    from .cli import PENDING_KEYS
    prof = config.cfg("profile")
    rows: list[tuple[bool, str, str]] = []
    missing = [k for k in PENDING_KEYS if get(prof, k) in (None, "")]
    rows.append((not missing, "your profile: " + ("complete" if not missing else "missing " + ", ".join(missing)),
                 "fill them in config/private.yaml"))
    rows.append((config.path("config", "private.yaml").is_file(), "config/private.yaml (your personal facts)",
                 "cp config/private.example.yaml config/private.yaml, then fill it"))
    from .apply.runner import my_resume
    r = my_resume()
    rows.append((bool(r), f"resume every application uploads: {r.name}" if r else "no resume to upload",
                 "hunt resume --use FILE.pdf, or send the PDF to the Telegram bot"))
    rows.append((bool(env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID")), "Telegram alerts",
                 "set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"))
    mail = bool(env("SMTP_USER") and env("SMTP_PASS"))
    rows.append((True, "email digest: " + ("on" if mail else "off (optional: Telegram already gets everything)"), ""))
    rows.append((llm.available(), "AI helper (optional: templates are used without it)", "hunt llm --test"))
    rows.append((bool(env("STATE_KEY")), "STATE_KEY for backups and the cloud radar", "set STATE_KEY in .env"))
    rows.append((bool(shutil.which("gh")), "GitHub CLI (gh)", "sudo pacman -S github-cli && gh auth login"))
    if shutil.which("systemctl"):
        for u in ALWAYS_ON + TIMERS:
            state, _ = _unit(u)
            rows.append((state == "active", f"{u}: {state}",
                         f"systemctl --user restart {u}" if state == "failed" else "scripts/install-services.sh"))
    rd = _load().get("radar") or {}
    if live or not rd.get("at"):
        hb, hrs = _heartbeat()
    else:
        hb, hrs = rd.get("text") or "cloud radar: not checked yet", rd.get("hrs")
        if rd.get("at") and hrs is not None:
            hrs += (time.time() - rd["at"]) / 3600
            hb += f" (checked {(time.time() - rd['at']) / 60:.0f} min ago)"
    rows.append((hrs is not None and hrs <= 1.5, hb, "gh workflow enable radar && gh workflow run radar"))
    from .ops import backup_dir
    bk = sorted(backup_dir().glob("jobhunter-*.tar.gz.enc"))
    rows.append((bool(bk), f"backups: {len(bk)}" + (f", newest {bk[-1].name}" if bk else ""), "hunt backup"))
    prof_dir = config.path(config.cfg("search").get("apply", {}).get("browser_profile_dir", "data/browser-profile"))
    rows.append((prof_dir.exists(), "bot browser profile (LinkedIn/Naukri logins)", "hunt login linkedin && hunt login naukri"))
    for site in ("linkedin", "naukri"):
        if f"{site}_logged_out" in _load().get("flags", {}):
            rows.append((False, f"{site.title()} logged the bot out", f"hunt login {site}"))
    free = shutil.disk_usage(config.home()).free / 1e9
    rows.append((free >= MIN_FREE_GB, f"disk: {free:.1f} GB free", "free some space (old screenshots are cleaned weekly)"))
    return rows


def as_telegram(rows: list[tuple[bool, str, str]]) -> str:
    bad = [r for r in rows if not r[0]]
    head = "<b>🩺 Health: all good ✅</b>" if not bad else f"<b>🩺 Health: {len(bad)} problem{'s' * (len(bad) > 1)}</b>"
    lines = [f"{'✓' if ok else '✗'} {_e(what)}" + ("" if ok else f"\n   fix: <code>{_e(fix)}</code>") for ok, what, fix in rows]
    return head + "\n" + "\n".join(lines)


# ── problems worth an alert ──
def _units() -> dict:
    out = {}
    for u in UNITS:
        state, restarts = _unit(u)
        short = u.removesuffix(".service")
        if state == "failed":
            out[f"unit:{u}"] = (f"{short} failed", f"journalctl --user -u {short} -n 30 ; systemctl --user restart {short}")
        elif restarts >= 3:
            out[f"unit:{u}"] = (f"{short} restarted {restarts} times", f"journalctl --user -u {short} -n 50")
    return out


def _gh(*args: str) -> str:
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30,
                              cwd=str(config.home())).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _heartbeat() -> tuple[str, float | None]:
    """The cloud radar's last run, saved so /health can answer without a network call."""
    from .morning import cloud_heartbeat
    hb, hrs = cloud_heartbeat()
    _save({**_load(), "radar": {"text": hb, "hrs": hrs, "at": time.time()}})
    return hb, hrs


def _radar() -> dict:
    _, hrs = _heartbeat()
    try:
        runs = [r for r in json.loads(_gh("run", "list", "--workflow", "radar", "-L", "2", "--json",
                                         "conclusion,status") or "[]") if r.get("status") == "completed"]
    except ValueError:
        runs = []
    fix = "gh run list --workflow radar -L 5 ; gh workflow enable radar && gh workflow run radar"
    if len(runs) == 2 and all(r.get("conclusion") == "failure" for r in runs):
        return {"radar": ("the last 2 cloud radar runs failed", fix)}
    if hrs is None or hrs > 6:
        return {"radar": ("no cloud radar run for 6 h" + (f" (last {hrs:.0f} h ago)" if hrs else ""), fix)}
    return {}


def _logins() -> dict:
    return {k: (f"{k.split('_')[0].title()} logged the bot out (seen while applying)", f"hunt login {k.split('_')[0]}")
            for k in _load().get("flags", {}) if k.endswith("_logged_out")}


def _llm() -> dict:
    from . import llm
    since = llm._state().get("_down_since")
    if since and time.time() - since >= 24 * 3600:
        return {"llm": ("every AI provider has failed for 24 h. Applying still works: cover letters and answers "
                        "use templates and your files", "hunt llm --test")}
    return {}


def _disk() -> dict:
    free = shutil.disk_usage(config.home()).free / 1e9
    return {"disk": (f"disk almost full: {free:.1f} GB free", "free some space; data/screens older than 7 days is cleaned")} \
        if free < MIN_FREE_GB else {}


def problems() -> dict:
    out = {}
    for f in (_units, _radar, _logins, _llm, _disk):
        try:
            out.update(f())
        except Exception as exc:              # one broken check must not hide the others
            out[f"check:{f.__name__}"] = (f"health check {f.__name__} crashed: {type(exc).__name__}", "hunt doctor")
    return out


def watch() -> list[str]:
    """Alert each new problem once; when it's gone, say so once. Returns the messages sent."""
    st = _load()
    known = st.get("alerted", {})
    now = problems()
    sent = []
    for k, (what, fix) in now.items():
        if k not in known:
            msg = f"⚠️ <b>JobHunter: {_e(what)}</b>\nfix: <code>{_e(fix)}</code>"
            if telegram(msg):
                sent.append(msg)
            known[k] = what
    for k in [k for k in known if k not in now]:
        msg = f"✅ fixed: {_e(known.pop(k))}"
        if telegram(msg):
            sent.append(msg)
    st = _load()
    st["alerted"] = known
    _save(st)
    return sent


def unit_failed(unit: str) -> None:
    """Called by systemd (OnFailure=) the moment a service fails."""
    st = _load()
    key = f"unit:{unit}"
    short = unit.removesuffix(".service")
    if key not in st.setdefault("alerted", {}):
        telegram(f"⚠️ <b>JobHunter: {_e(short)} failed</b>\n"
                 f"fix: <code>journalctl --user -u {_e(short)} -n 30 ; systemctl --user restart {_e(short)}</code>")
        st["alerted"][key] = f"{short} failed"
        _save(st)


# ── Sunday 21:00 ──
def weekly(db, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    since = (dt.datetime.combine(today, dt.time()) - dt.timedelta(days=7)).isoformat()
    apps = [a for a in db.applications(limit=2000) if (a.get("at") or "") >= since and not a.get("dry")]
    sent = [a for a in apps if a.get("outcome") == "applied"]
    ev = lambda kind: db.c.execute("SELECT COUNT(DISTINCT job_id) FROM events WHERE kind=? AND at>=?", (kind, since)).fetchone()[0]
    names = {}
    for a in sent:
        j = db.get(a["job_id"])
        names[a["job_id"]] = j["company"] if j else "?"
    top = Counter(names.values()).most_common(5)
    fails = Counter(((a.get("why") or "unknown").strip().splitlines() or ["unknown"])[0][:90]
                    for a in apps if a.get("outcome") == "failed").most_common(5)
    L = [f"<b>📊 Your week · {today - dt.timedelta(days=7):%d %b} to {today:%d %b}</b>",
         f"Applied {len(sent)} · Interviews {ev('interview')} · Offers {ev('offer')} · Rejected {ev('rejected')}"]
    if top:
        L.append("<b>Applied to most</b>\n" + "\n".join(f"• {_e(c)} ({n})" for c, n in top))
    if fails:
        L.append("<b>Failures and why</b>\n" + "\n".join(f"• {n}× {_e(w)}" for w, n in fails))
    else:
        L.append("No failed applications this week ✅")
    return "\n\n".join(L)
