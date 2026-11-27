"""Isolated JOBHUNTER_HOME per test run: a copy of config/, the real skills/, an empty data/."""
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def make_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="jh-test-"))
    shutil.copytree(ROOT / "config", home / "config", ignore=shutil.ignore_patterns("private.yaml", "resume.pdf"))
    shutil.copy(ROOT / "tests" / "fixtures" / "private.test.yaml", home / "config" / "private.yaml")
    os.symlink(ROOT / "skills", home / "skills")
    (home / "data").mkdir()
    os.environ["JOBHUNTER_HOME"] = str(home)
    os.environ.setdefault("JOBHUNTER_BACKUP_DIR", str(home / "backups"))   # never the real ~/jobhunter-backups
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_USER", "SMTP_PASS"):
        os.environ.pop(k, None)
    for k in list(os.environ):          # never let real LLM keys reach a test
        if k.startswith(("NVIDIA_", "GEMINI_", "GROQ_", "OPENAI_", "LLM_", "OLLAMA_")):
            os.environ.pop(k, None)
    os.environ["LLM_PROVIDER"] = "none"
    from jobhunter import config
    config.reload()
    import yaml
    sp = home / "config" / "search.yaml"
    d = yaml.safe_load(sp.read_text())
    d["apply"]["typing_delay_ms"] = [0, 1]        # fast tests
    d["apply"]["headless"] = True
    d["apply"]["pause_range_sec"] = [0, 0]
    d["apply"]["mode"] = "basic_auto"            # most tests check the auto rules; check mode has its own tests
    sp.write_text(yaml.safe_dump(d))
    config.reload()
    from jobhunter.apply import browser
    browser.human_pause = lambda a=0, b=0: None
    from jobhunter import assistant
    assistant.live_scan = lambda *a, **k: 0       # no test ever searches LinkedIn/Naukri for real
    from jobhunter import salary                  # … or AmbitionBox / the web for salaries
    salary.company_source = salary.role_source = lambda *a, **k: None
    salary.web_sources = lambda *a, **k: []
    return home


def write_pdf(path: Path) -> Path:
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    c.drawString(72, 760, "Test resume")
    c.save()
    return path


def use(home: Path) -> None:
    """Point the package at this module's home (call from setUpModule: test modules
    loaded together would otherwise share whichever home was created last)."""
    os.environ["JOBHUNTER_HOME"] = str(home)
    os.environ["LLM_PROVIDER"] = "none"
    from jobhunter import config
    config.reload()
