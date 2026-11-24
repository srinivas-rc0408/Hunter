"""Ask you, never guess: terminal questions for form fields the bot can't answer from your facts.

The bot shows the exact question and, for dropdowns and radio buttons, the site's own choices
numbered. You answer once. The answer goes into the form AND into data/answers.yaml, so the
same question is answered for you next time. Unanswered questions (timeout / 's') are left
for the Review Center, where you can answer them from your phone too.
"""
from __future__ import annotations

import select
import sys

from rich.console import Console

console = Console()
TIMEOUT = 180          # seconds per question before the form is queued for later


def interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def read(prompt: str, timeout: int = TIMEOUT) -> str | None:
    """One line from you, or None on timeout / no terminal."""
    if not interactive():
        return None
    console.print(prompt, end="")
    sys.stdout.flush()
    try:
        r, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError):
        return None
    if not r:
        console.print("\n[yellow]no answer: queued for the Review Center[/]")
        return None
    return sys.stdin.readline().rstrip("\n")


def question(label: str, options: list[str] | None, required: bool, note: str = "", job: str = "") -> str | None:
    """Ask one form question. Returns your answer (option text for choices), '' to leave empty, None to skip."""
    console.print(f"\n[bold yellow]?[/] [bold]{label.strip().rstrip('*').strip()}[/]"
                  + (" [red]*required[/]" if required else " [dim](optional)[/]")
                  + (f"  [dim]· {job}[/]" if job else ""))
    if note:
        console.print(f"  [dim]{note}[/]")
    opts = [o for o in (options or []) if o and o.strip().lower() not in ("select", "select an option", "please select",
                                                                          "--", "choose", "select one")]
    if opts:
        for i, o in enumerate(opts, 1):
            console.print(f"  [cyan]{i:>2}[/]. {o}")
        while True:
            a = read("  your choice (number, s = skip): ")
            if a is None or a.strip().lower() in ("s", "skip"):
                return None
            if a.strip().isdigit() and 1 <= int(a) <= len(opts):
                return opts[int(a) - 1]
            console.print("  [red]type one of the numbers shown[/]")
    a = read("  your answer (Enter = leave empty, s = skip): ")
    if a is None or a.strip().lower() in ("s", "skip"):
        return None
    return a.strip()


