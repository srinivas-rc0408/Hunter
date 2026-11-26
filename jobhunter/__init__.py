"""JobHunter India — find every relevant job early, rank it, tailor, fill, track.

Works offline-first: scanning and ranking need no LLM; tailoring uses any
OpenAI-compatible endpoint (local Ollama by default).
"""
import subprocess
from pathlib import Path


def _git_version(run=subprocess.run) -> str:
    """The git tag (`v2.5`, `v2.5-3-gabc123`, `...-dirty`); 'v2.5' when pip-installed or git is missing."""
    try:
        r = run(["git", "describe", "--tags", "--always", "--dirty"], cwd=Path(__file__).resolve().parent.parent,
                capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "v2.5"
    except (OSError, subprocess.SubprocessError):
        return "v2.5"


__version__ = _git_version()
