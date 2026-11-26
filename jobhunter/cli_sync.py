"""`hunt sync`, `mail`, `sheet`, `secrets`, `rescore`, `dream`, `llm`: data in and out. Commands are wired up in cli.main."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from . import config
from .cli import _console


# ── sync with the cloud radar (GitHub Actions) ─────────────────────────────
def cmd_sync(a):
    """Pull the cloud radar's encrypted jobs.db and merge it into yours."""
    from .config import env
    from .db import DB
    home = config.home()
    key = env("STATE_KEY")
    quiet = getattr(a, "quiet", False)
    if not key:
        if not quiet:
            _console().print("[yellow]STATE_KEY missing in .env (run `hunt secrets`)[/]")
        return
    enc, tmp = config.data_dir() / "cloud.db.enc", config.data_dir() / "cloud.db"
    try:
        subprocess.run(["git", "-C", str(home), "fetch", "-q", "origin", "radar-state"], check=True, timeout=60)
        enc.write_bytes(subprocess.run(["git", "-C", str(home), "show", "origin/radar-state:jobs.db.enc"], check=True,
                                       capture_output=True, timeout=60).stdout)
        subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", "env:STATE_KEY",
                        "-in", str(enc), "-out", str(tmp)], check=True, timeout=60, capture_output=True,
                       env={**__import__("os").environ, "STATE_KEY": key})
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        if not quiet:
            _console().print(f"[yellow]no cloud state yet or wrong STATE_KEY ({exc})[/]")
        return
    added, updated = DB().merge_from(tmp)
    tmp.unlink(missing_ok=True)
    _console().print(f"synced from cloud radar: +{added} jobs, {updated} status updates")
    _mail_and_sheet(quiet=True)


def _mail_and_sheet(quiet: bool = True) -> None:
    """After every sync: read new application emails (if set up) and rewrite the Excel tracker."""
    from . import mail, tracker
    if all(mail.creds()):
        try:
            r = mail.sync(quiet=quiet)
            if r.get("error"):
                _console().print(f"[yellow]email check: {r['error']}[/]")
            elif r and not quiet:
                _console().print(r)
        except Exception as exc:
            _console().print(f"[yellow]email check failed: {type(exc).__name__}: {str(exc)[:150]}[/]")
    tracker.refresh()


def cmd_mail(a):
    from . import mail
    c = _console()
    if not all(mail.creds()):
        c.print("Set up once: Google Account → Security → 2-Step Verification → App passwords → create one "
                "named JobHunter.\nThen add to .env:\n  GMAIL_USER=you@gmail.com\n  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx")
        return
    if a.reset:
        from .db import DB
        db = DB(); mail.ensure(db); n = db.c.execute("DELETE FROM mail").rowcount
        db.c.execute("DELETE FROM mail_state"); db.c.commit()
        c.print(f"forgot {n} sorted emails: they'll be re-read and re-sorted now. To remove old Gmail labels, "
                "delete the 'JobHunter' label in Gmail (your emails stay).")
    r = mail.sync(days=a.days, label=not a.no_label, quiet=False)
    if r.get("error"):
        c.print(f"[red]✗ {r['error']}[/]")
        return
    c.print(r if r else "no new application emails")
    from .db import DB
    from .tracker import numbers
    n = numbers(DB())
    c.print(f"Applications: {n['Applied']} · waiting {n['Waiting']} · interviews {n['Interviews']} · offers {n['Offers']} · "
            f"rejected {n['Rejected']} · response rate {n['Response rate']} · interview rate {n['Interview rate']}")
    from rich.markup import escape
    import re as _re
    c.print(escape(_re.sub(r"<[^>]+>", "", mail.summary_text(DB(), 7))))


def cmd_sheet(a):
    from . import tracker
    p = tracker.write()
    _console().print(f"[green]✓[/] {p}")
    if a.png:
        for f in tracker.preview(Path(a.png).expanduser()):
            _console().print(f"[green]✓[/] {f}")
    if a.open:
        subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cmd_secrets(a):
    """Create STATE_KEY (saved to .env) and print/push the GitHub secrets the cloud radar needs."""
    import base64
    import secrets as _s

    from .config import env
    home = config.home()
    envf = home / ".env"
    if not envf.exists():
        shutil.copy(home / ".env.example", envf)
    key = env("STATE_KEY")
    if not key:
        key = _s.token_urlsafe(32)
        with envf.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# encrypts the cloud radar's state (also a GitHub secret)\nSTATE_KEY={key}\n")
        _console().print("STATE_KEY generated and saved to .env")
    priv = config.path("config", "private.yaml")
    if not priv.is_file():
        sys.exit("config/private.yaml missing: copy config/private.example.yaml and fill it")
    vals = {"STATE_KEY": key, "PRIVATE_YAML_B64": base64.b64encode(priv.read_bytes()).decode()}
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_USER", "SMTP_PASS", "DIGEST_TO"):
        if env(k):
            vals[k] = env(k)
    if a.push:
        if not shutil.which("gh"):
            sys.exit("GitHub CLI not found: `sudo pacman -S github-cli`, then `gh auth login`")
        for k, v in vals.items():
            subprocess.run(["gh", "secret", "set", k, "--body", v], cwd=str(home), check=True)
            _console().print(f"✓ secret {k}")
        _console().print("Done. Run the workflow once: `gh workflow run radar -f mode=full`")
    else:
        _console().print("Secrets to add at GitHub → repo → Settings → Secrets and variables → Actions:")
        for k in vals:
            _console().print(f"  {k}")
        _console().print("Easiest: `hunt secrets --push` (uses the GitHub CLI). Values aren't printed here on purpose.")


def cmd_rescore(a):
    """Re-score every open job with the current rules (after an update or a profile change)."""
    from .rescore import rescore
    r = rescore()
    c = _console()
    for t, co, why in r["dropped"][:40]:
        c.print(f"[red]✗[/] {t[:60]} · {co} → {why}")
    c.print(f"re-scored {r['total']} jobs; {r['changed']} scores changed, {len(r['dropped'])} dropped by the new rules")


def cmd_dream(a):
    from . import dreams
    c = _console()
    if a.action == "list" or not a.name:
        ds = dreams.names()
        c.print("★ dream companies: " + (", ".join(ds) if ds else "none") +
                "\nadd one: hunt dream add \"Company\"   remove: hunt dream remove \"Company\"")
        return
    name = " ".join(a.name)
    ok, msg = (dreams.add if a.action == "add" else dreams.remove)(name)
    c.print(("[green]✓[/] " if ok else "[yellow]·[/] ") + msg)
    if ok:
        for n in dreams.after_change(name, a.action == "add", push=not a.no_push):
            c.print("  " + n)


def cmd_llm(a):
    """Show the LLM chain; --test sends one tiny request through every key and model."""
    import time as _t

    from . import llm
    from rich.table import Table
    c = _console()
    rows = llm.status()
    t = Table(title="LLM chain (auto tries these in order)")
    for col in ("#", "provider", "model now", "fallbacks", "keys", "state"):
        t.add_column(col)
    for i, r in enumerate(rows, 1):
        t.add_row(str(i), r["prov"], r["model"] or "-", ", ".join(r["fallbacks"]) or "-",
                  str(r["keys"]) + (f" ({r['dead']} refused)" if r["dead"] else ""),
                  ("[green]ready[/] " if r["ok"] else "[red]off[/] ") + r["note"])
    c.print(t)
    if a.models:
        for p in llm._providers():
            c.print(f"[bold]{p['prov']}[/] live chat models (best first): " + ", ".join(llm.ranked(p["prov"], p["ids"])[:25]))
    if not a.test:
        c.print("[dim]hunt llm --test  checks every key with one tiny request (uses ~1 request per key)[/]")
        return
    for p in llm._providers():
        prov = p["prov"]
        model = p["models"][0]
        for k in (llm.keys(prov) or [None]):
            t0 = _t.time()
            c.print(f"[dim]… {prov} key {llm.mask(k)} ({model}), up to 30 s[/]")
            code, text = llm._post(p, model, k, "Reply with exactly: OK", "Say OK.", 0, 2000, timeout=30)
            if code in (404, 408, 410):            # retired or overloaded: find the first model that answers
                for alt in p["models"][1:]:
                    c2, t2 = llm._post(p, alt, k, "Reply with exactly: OK", "Say OK.", 0, 2000, timeout=30)
                    c.print(f"  [dim]{model}: {'timed out' if code == 408 else 'retired'}; {alt}: "
                            f"{'works' if c2 == 200 and t2 else c2}[/]")
                    if c2 == 200 and t2:
                        llm._remember(prov, alt, True)
                        code, text, model = c2, t2, alt
                        break
            dt = _t.time() - t0
            good = code == 200 and bool(text)
            why = {401: "key invalid", 403: "key refused / model not allowed", 404: "model not found",
                   410: "model retired (Gone)",
                   408: "timed out", 429: "rate-limited (wait a minute)", 0: "unreachable"}.get(code, f"HTTP {code}")
            c.print(f"{'[green]✓[/]' if good else '[red]✗[/]'} {prov} {model} key {llm.mask(k)}: "
                    + (f"{text[:20]!r} in {dt:.1f}s" if good else why))
    out = llm.chat("Reply in one short sentence.", "Name one skill an AI/ML intern needs.", max_tokens=1500)
    c.print(f"chain answer via {llm.LAST.get('prov', '-')} / {llm.LAST.get('model', '-')}: {out or 'none (templates will be used)'}")
