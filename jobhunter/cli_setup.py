"""`hunt doctor`, `setup`, `telegram-id`, `advantages`, `calendar`, `export-hunter`. Commands are wired up in cli.main."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys

from . import config
from .cli import _console


# ── setup / health ─────────────────────────────────────────────────────────
PENDING_KEYS = ["location.pincode", "gender", "date_of_birth", "answers.expected_ctc_lpa",
                "answers.expected_stipend_month", "sc_subgroup", "marital_status"]


def cmd_doctor(a):
    from . import llm
    from .config import env, get
    c = _console()
    ok = lambda b: "[green]✓[/]" if b else "[red]✗[/]"
    prof = config.cfg("profile")
    c.rule("JobHunter doctor")
    c.print(f"{ok(sys.version_info >= (3, 10))} Python {sys.version.split()[0]}")
    for mod in ("jobspy", "playwright", "yaml", "bs4", "pdfplumber", "reportlab", "rich"):
        try:
            __import__(mod)
            c.print(f"{ok(True)} {mod}")
        except ImportError:
            c.print(f"{ok(False)} {mod}: pip install -r requirements.txt")
    missing = [k for k in PENDING_KEYS if get(prof, k) in (None, "")]
    c.print(f"{ok(not missing)} profile: " + ("complete" if not missing else "missing " + ", ".join(missing)))
    c.print(f"{ok(config.path('config', 'private.yaml').is_file())} config/private.yaml (personal facts, never committed)")
    resume_ok = config.path("config", "resume.pdf").is_file()
    filled = "TODO" not in config.path("config", "resume.yaml").read_text()
    c.print(f"{ok(resume_ok or filled)} resume: " + ("config/resume.pdf" if resume_ok else "") +
            (" + resume.yaml filled" if filled else " (resume.yaml still has TODOs: tailoring uses only filled bullets)"))
    ap = config.cfg("search").get("apply", {})
    if ap.get("resume", "own") == "own":
        from .apply.runner import my_resume
        r = my_resume()
        c.print(f"{ok(bool(r))} applications upload: " + (f"{r.name} (your own PDF)" if r else
                "nothing yet: hunt resume --use FILE, or send the PDF to the Telegram bot"))
    c.print(f"{ok(True)} apply mode: {ap.get('mode', 'check')}"
            + (" (you read each filled form and press Submit; fixes are learned)" if ap.get("mode", "check") == "check" else ""))
    c.print(f"{ok(bool(env('TELEGRAM_BOT_TOKEN') and env('TELEGRAM_CHAT_ID')))} Telegram alerts")
    c.print(f"{ok(bool(env('SMTP_USER') and env('SMTP_PASS')))} email digest")
    rows = llm.status()
    c.print(f"{ok(llm.available())} LLM (optional; tried in order, templates if none): " + (" → ".join(
        f"{r['prov']} " + (f"[green]{r['model']}[/]" + (f" ({r['keys']} keys)" if r['keys'] > 1 else "")
                           if r["ok"] else f"[dim]{r['note']}[/]") for r in rows) or "LLM_PROVIDER=none"))
    c.print(f"{ok(bool(env('STATE_KEY')))} STATE_KEY (cloud state + backups). Keep a copy off this laptop")
    c.print(f"{ok(bool(shutil.which('gh')))} GitHub CLI (gh) for secrets and workflow control")
    if shutil.which("systemctl"):
        for u in ("jobhunter-review.service", "jobhunter-bot.service", "jobhunter-sync.timer", "jobhunter-scan.timer",
                  "jobhunter-morning.timer", "jobhunter-backup.timer"):
            r = subprocess.run(["systemctl", "--user", "is-active", u], capture_output=True, text=True)
            c.print(f"{ok(r.stdout.strip() == 'active')} {u} ({r.stdout.strip() or 'missing'})"
                    + ("" if r.stdout.strip() == "active" else ": run scripts/install-services.sh"))
    from .morning import cloud_heartbeat
    hb, hrs = cloud_heartbeat()
    c.print(f"{ok(hrs is not None and hrs <= 1.5)} {hb}")
    from .ops import backup_dir
    bk = sorted(backup_dir().glob("jobhunter-*.tar.gz.enc"))
    c.print(f"{ok(bool(bk))} backups: {len(bk)} in {backup_dir()}" + (f" (newest {bk[-1].name})" if bk else
                                                                      ": run `hunt backup`"))
    prof_dir = config.path(config.cfg("search").get("apply", {}).get("browser_profile_dir", "data/browser-profile"))
    c.print(f"{ok(prof_dir.exists())} browser profile (run `hunt login linkedin` and `hunt login naukri`)")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            b.close()
        c.print(f"{ok(True)} Chromium for Playwright")
    except Exception as exc:
        c.print(f"{ok(False)} Chromium: run `python -m playwright install chromium` ({str(exc)[:60]})")
    if a.test_alert:
        from .notify import telegram
        c.print(f"{ok(telegram('✅ JobHunter is connected. Alerts will arrive here.'))} test Telegram message")


def cmd_telegram_id(a):
    import requests

    from .config import env
    tok = env("TELEGRAM_BOT_TOKEN")
    if not tok:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env first (from @BotFather).")
    r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=20).json()
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name") for u in r.get("result", []) if "message" in u}
    if not chats:
        sys.exit("Send any message to your bot in Telegram first, then run this again.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


def cmd_advantages(a):
    """Your advantages as a checklist (SC, Karnataka, Kannada, GATE)."""
    c = _console()
    prof = config.cfg("profile")
    c.rule("Your advantages: use every one")
    items = config.cfg("advantages").get("advantages") or config.cfg("govt").get("advantages", [])
    if not items:
        sys.exit("config/advantages.yaml not found")
    for i, x in enumerate(items, 1):
        c.print(f"[bold]{i:>2}. {x['title']}[/]")
        c.print(f"    {x['detail']}")
        c.print(f"    [green]→ {x['action']}[/]")
        c.print(f"    [dim]{x['status']}[/]")
    todo = []
    if not prof.get("sc_subgroup"):
        todo.append("set sc_subgroup (A/B/C) in config/private.yaml")
    if not (prof.get("education") or [{}])[0].get("percent_equivalent"):
        todo.append("get REVA's CGPA→% conversion certificate, then set percent_equivalent")
    if todo:
        c.print("\n[bold yellow]Open items:[/] " + "; ".join(todo))


def cmd_calendar(a):
    c = _console()
    today = dt.date.today()
    for e in sorted(config.cfg("govt").get("calendar", []), key=lambda e: str(e["date"])):
        d = e["date"] if isinstance(e["date"], dt.date) else dt.date.fromisoformat(str(e["date"]))
        if d < today - dt.timedelta(days=7):
            continue
        days = (d - today).days
        c.print(f"{d}  [{'red' if days < 15 else 'white'}]{days:>4}d[/]  {e['event']}  [dim]({e['status']})[/]")


def cmd_export_hunter(a):
    """Write hunter/profile.json + hunter/pipeline.json so the hunter-* skills share this data."""
    from .db import DB
    prof = config.cfg("profile")
    hd = config.home() / "hunter"
    hd.mkdir(exist_ok=True)
    edu = next((e for e in prof["education"] if e.get("level") == "bachelors"), {})
    e10 = next((e for e in prof["education"] if e.get("level") == "class10"), {})
    e12 = next((e for e in prof["education"] if e.get("level") == "class12"), {})
    ans = prof.get("answers", {})
    hp = {
        "name": prof["name"], "first_name": prof["first_name"], "email": prof["email"], "phone": prof["phone"],
        "location": f"{prof['location']['city']}, {prof['location']['state']}",
        "target_roles": prof["targets"]["roles"], "seniority": "entry", "years_experience": 0,
        "skills": [s for g in prof["skills"].values() for s in g],
        "locations": ["Bengaluru, KA", "Mysuru, KA", "Mangaluru, KA"], "remote": prof["targets"].get("remote_ok", True),
        "relocate": False, "exclude_titles": prof["targets"]["exclude_title_words"],
        "dream_companies": prof.get("dream_companies", []), "links": {
            "linkedin": prof["links"].get("linkedin"), "github": prof["links"].get("github"),
            "website": prof["links"].get("portfolio")},
        "work_authorization": {"country": "IN", "authorized": True, "needs_sponsorship": False},
        "salary_floor": prof["targets"].get("min_ctc_inr_year"), "currency": "INR",
        "resume_path": "config/resume.pdf",
        "current": {"company": None, "title": "B.Tech student", "since": "2023-08"},
        "education": [{"school": edu.get("institution"), "degree": f"{edu.get('degree')} {edu.get('branch')}",
                       "year": edu.get("end"), "cgpa": edu.get("cgpa"), "tenth_percent": e10.get("percent"),
                       "twelfth_percent": e12.get("percent"), "backlogs": edu.get("backlogs")}],
        "answers": {"start_date": ans.get("earliest_joining"), "notice_period": ans.get("notice_period"),
                    "relocation": ans.get("willing_to_relocate"), "onsite": ans.get("work_from_office"),
                    "how_did_you_hear": ans.get("how_did_you_hear"), "previously_employed": ans.get("previously_employed_here"),
                    "current_ctc": ans.get("current_ctc_text"), "salary_expectation": ans.get("expected_ctc"),
                    "why_this_company": None, "why_leaving": None, "referral": None, "pronouns": None},
        "self_identification": {"_note": "Voluntary; never answered for you.", "gender": None,
                                "race_ethnicity": None, "veteran_status": None, "disability_status": None},
    }
    (hd / "profile.json").write_text(json.dumps(hp, indent=2, ensure_ascii=False), encoding="utf-8")
    stage = {"applied": "applied", "interview": "interview", "offer": "offer", "rejected": "rejected", "ghosted": "applied",
             "needs_you": "saved", "alerted": "saved", "new": "saved", "queued": "saved", "filled": "saved"}
    db = DB()
    roles = []
    for j in db.query(status=list(stage), kind="private", min_score=a.min, limit=500):
        applied = next((e["at"][:10] for e in db.events(j["id"]) if e["kind"] == "applied"), None)
        st = stage[j["status"]]
        if st in ("applied", "interview", "offer") and not applied:
            applied = (j.get("status_at") or j["found_at"])[:10]
        roles.append({"id": j["id"], "company": j["company"], "title": j["title"], "url": j["url"], "stage": st,
                      "applied": applied, "next_step": None, "next_date": None,
                      "notes": f"{j['location']}. Fit {j['score']} from jobhunter."})
    pf = hd / "pipeline.json"
    old = json.loads(pf.read_text()) if pf.is_file() else {"roles": [], "contacts": [], "threads": []}
    keep = {r["id"]: r for r in old.get("roles", [])}
    for r in roles:
        keep[r["id"]] = {**keep.get(r["id"], {}), **{k: v for k, v in r.items() if v is not None or k not in keep.get(r["id"], {})}}
    old["roles"] = list(keep.values())
    pf.write_text(json.dumps(old, indent=2, ensure_ascii=False), encoding="utf-8")
    _console().print(f"hunter/profile.json + hunter/pipeline.json ({len(old['roles'])} roles) written")


def cmd_setup(a):
    """First-run helper: copies .env, checks Chromium, prints what's left."""
    c = _console()
    home = config.home()
    env = home / ".env"
    if not env.exists():
        shutil.copy(home / ".env.example", env)
        c.print("created .env from .env.example: add your Telegram token there")
    if a.browser:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    cmd_doctor(argparse.Namespace(test_alert=False))
    c.print("\nNext: `hunt login linkedin` · `hunt login naukri` · `hunt scan` · `hunt apply --dry-run`")
