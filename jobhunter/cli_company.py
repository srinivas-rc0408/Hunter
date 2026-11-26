"""`hunt company`, `hunt questions`, `hunt answers`, `hunt classify`: companies and form questions. Commands are wired up in cli.main."""
from __future__ import annotations

import json
import webbrowser

from .cli import _console


def _ask(prompt: str, default: str = "") -> str:
    try:
        v = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"
    return v.strip() or default


def _print_listings(rows: list[dict], ka_only: bool, fit_only: bool, limit: int) -> list[dict]:
    from rich.table import Table
    shown = [r for r in rows if (not ka_only or r["karnataka"]) and (not fit_only or r["fit"])][:limit]
    t = Table(header_style="bold", show_lines=False)
    for col, w in (("#", 4), ("posted", 9), ("role", 46), ("where", 24), ("type", 10), ("via", 16), ("fit", 4),
                   ("status", 9)):
        t.add_column(col, max_width=w, overflow="fold")
    for i, r in enumerate(shown, 1):
        d = r.get("days_ago")
        when = ("🔥 today" if d == 0 else f"{d}d ago") if d is not None else "?"
        t.add_row(str(i), when, r["title"], (r.get("location") or "")[:40] + (" ✓KA" if r["karnataka"] else ""),
                  r.get("job_type") or "", ",".join(r["sources"])[:16], str(r["score"]) if r["fit"] else "—",
                  r.get("status") or "")
    _console().print(t)
    return shown


def cmd_company(a):
    """Type a company → choose → every listing with date, place, source and link → apply/watch."""
    from . import companies
    c = _console()
    q = a.query or _ask("Company name: ")
    if not q or q == "q":
        return
    sug = companies.suggest(q, online=not a.offline)
    if a.pick:
        choice = sug[min(a.pick, len(sug)) - 1]
    else:
        for i, s in enumerate(sug, 1):
            c.print(f"  [bold]{i:>2}[/]. {s['name']}  [dim]{s.get('hint') or ''} · {s['source']}[/]")
        n = _ask("Pick a number [1]: ", "1")
        if n == "q" or not n.isdigit() or not 1 <= int(n) <= len(sug):
            return
        choice = sug[int(n) - 1]
    c.print(f"[dim]Finding {choice['name']}'s careers page and hiring platform…[/]")
    info = companies.resolve(choice["name"], choice.get("domain"), choice.get("wikidata"), refresh=a.refresh)
    c.rule(f"[bold]{info['name']}[/]")
    meta = [x for x in [info.get("description"), info.get("website"), f"~{info['employees']} employees" if info.get("employees")
                        else None, f"founded {info['founded']}" if info.get("founded") else None] if x]
    if meta:
        c.print(" · ".join(meta))
    plats = ", ".join(f"{b['type']}:{b.get('token') or b.get('host')}" for b in info.get("boards", [])) or "none found"
    c.print(f"hiring platform: [bold]{plats}[/]" + (f" · careers page: {info['careers_url']}" if info.get("careers_url") else ""))
    for n_ in info.get("notes", []):
        c.print(f"[yellow]{n_}[/]")
    c.print("[dim]Collecting listings (company platform + LinkedIn/Naukri/Indeed + your database)…[/]")
    res = companies.listings(info, boards_search=not a.no_boards)
    rows = res["listings"]
    k = res["counts"]
    c.print(f"[bold]{k['total']}[/] listings · {k['karnataka']} in Karnataka/remote-India · {k['fit']} fit your rules · "
            f"{k['new_24h']} posted in the last 24 h")
    for e in res["errors"][:5]:
        c.print(f"[dim]  · {e}[/]")
    if a.json:
        print(json.dumps(res, default=str, ensure_ascii=False))
        return
    ka, fit, limit = not a.all, False, 60
    shown = _print_listings(rows, ka, fit, limit)
    help_ = ("[dim]commands: [bold]apply 1,3[/] (fill + submit basic) · [bold]preview 2[/] (fill only) · "
             "[bold]track 1-4[/] (add to queue) · [bold]show 3[/] · [bold]open 3[/] · [bold]watch[/] (include in every scan) · "
             "[bold]links[/] (referrals) · [bold]all[/]/[bold]ka[/]/[bold]fit[/] (filters) · [bold]q[/][/]")
    c.print(help_)

    def pick(spec: str) -> list[dict]:
        idx = set()
        for part in spec.replace(" ", "").split(","):
            if "-" in part:
                lo, hi = part.split("-", 1)
                if lo.isdigit() and hi.isdigit():
                    idx |= set(range(int(lo), int(hi) + 1))
            elif part.isdigit():
                idx.add(int(part))
        return [shown[i - 1] for i in sorted(idx) if 1 <= i <= len(shown)]

    while True:
        cmd = _ask("> ")
        if cmd in ("q", "quit", "exit"):
            break
        verb, _, arg = cmd.partition(" ")
        if verb in ("all", "ka", "fit"):
            ka, fit = verb == "ka", verb == "fit"
            shown = _print_listings(rows, ka, fit, limit)
        elif verb in ("apply", "preview", "track"):
            chosen = pick(arg)
            if not chosen:
                c.print("which numbers? e.g. apply 1,3 or track 2-5")
                continue
            ids = companies.track(chosen)
            c.print(f"added to your queue: {', '.join(i[:8] for i in ids)}")
            if verb != "track":
                from .apply.runner import apply
                apply(ids=ids, dry=verb == "preview", with_cover=True)
        elif verb in ("show", "open"):
            for r in pick(arg):
                if verb == "open":
                    webbrowser.open(r.get("apply_url") or r["url"])
                else:
                    c.rule(f"{r['title']} · {r['company']}")
                    c.print(f"{r.get('location')} · {r.get('job_type') or ''} · posted {r.get('posted') or '?'} · "
                            f"via {', '.join(r['sources'])}\napply: {r.get('apply_url') or r['url']}\n"
                            f"fit: {r['score'] if r['fit'] else 'outside your rules'} · {r.get('why') or ''}\n")
                    c.print((r.get("description") or "")[:1500])
        elif verb == "watch":
            c.print("added to config/watchlist.yaml: every scan now includes it" if companies.watch(info)
                    else "already watched (or no platform/careers page to watch)")
        elif verb == "links":
            for k_, v in companies.links(info).items():
                if v:
                    c.print(f"  {k_}: {v}")
        else:
            c.print(help_)


def cmd_questions(a):
    """Answer what forms asked that isn't in your files. Never guessed; asked once."""
    from .apply import questions
    c = _console()
    rows = questions.pending()
    if not rows:
        c.print("No open questions. ✅")
        return
    if a.list:
        for i, r in enumerate(rows, 1):
            c.print(f"{i:>2}. {r['label']}  [dim]({len(r['jobs'])} job(s) waiting"
                    + (f"; options: {', '.join(r['options'][:8])}" if r.get("options") else "") + ")[/]")
        return
    c.print(f"[bold]{len(rows)} question(s).[/] Answer only with true facts. Enter = skip, q = stop.\n")
    for r in rows:
        c.print(f"[bold]{r['label']}[/]  [dim]{len(r['jobs'])} job(s) waiting · seen on {', '.join(r.get('sites') or ['?'])}[/]")
        if r.get("options"):
            for i, o in enumerate(r["options"], 1):
                c.print(f"   {i}. {o}")
        v = _ask("   your answer: ")
        if v == "q":
            break
        if not v:
            continue
        if r.get("options") and v.isdigit() and 1 <= int(v) <= len(r["options"]):
            v = r["options"][int(v) - 1]
        try:
            res = questions.answer(r["key"], v)
            c.print(f"   [green]saved[/] → {res['value']!r}" + (f" · {len(res['requeued'])} job(s) back in the apply queue"
                                                              if res["requeued"] else ""))
        except (ValueError, KeyError) as exc:
            c.print(f"   [red]{exc}[/]")


def cmd_answers(a):
    from .apply.fields import load_bank
    bank = load_bank()
    for k, v in sorted(bank.items()):
        print(f"{v['label'][:60]:60} → {v['answer']}")
    print(f"\n{len(bank)} learned answers in data/answers.yaml (edit or delete lines freely)")


def cmd_classify(a):
    """Debug: how would a form label be answered?"""
    from .apply.fields import answer
    r = answer(a.label, a.type, a.placeholder)
    print(f"{r.key} → {r.value!r} ({r.basis}{', BASIC' if r.basic else ''}) {r.note}")
