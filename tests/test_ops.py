"""Morning brief, Telegram bot commands, LLM fallback, encrypted backup/restore."""
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

from _home import use, make_home

HOME = make_home()


def setUpModule():
    use(HOME)

os.environ["STATE_KEY"] = "test-key-123"
os.environ["JOBHUNTER_BACKUP_DIR"] = tempfile.mkdtemp(prefix="jh-bk-")

from jobhunter.db import DB, Job  # noqa: E402
from jobhunter.score import score  # noqa: E402


def seed(db, title, company, **kw):
    j = score(Job(title, company, "Bengaluru, Karnataka", url="https://x.example/j", source="linkedin",
                  description="Python FastAPI LLMs freshers", posted_at=str(dt.date.today()), **kw))
    db.upsert(j)
    db.set_score(j.id, j.score, j.reasons, j.flags, j.dream)
    return j


class Morning(unittest.TestCase):
    def test_brief(self):
        from jobhunter import morning
        db = DB(HOME / "data" / "m.db")
        a = seed(db, "Machine Learning Engineer Intern", "Aditya Birla Capital", job_type="internship")
        b = seed(db, "AI Engineer", "Foo Labs")
        db.set_status(b.id, "needs_you")
        g = Job("Recruitment of Project Engineer (AI)", "IISc", "Bengaluru", url="https://iisc/x", kind="govt",
                deadline=str(dt.date.today() + dt.timedelta(days=4)))
        g.score, g.flags = 80, ["tech", "eligibility:eligible"]
        db.upsert(g)
        db.set_score(g.id, g.score, [], g.flags, False)
        br = morning.build(db=db, today=dt.date.today(), heartbeat=False)
        self.assertEqual(br["new"][0]["id"], a.id)                 # dream first
        self.assertEqual(len(br["waiting"]), 1)
        self.assertEqual(br["govt_soon"][0][1]["id"], g.id)
        msg = morning.as_telegram(br)
        self.assertIn("Morning brief", msg)
        self.assertIn(f"/show_{a.id[:8]}", msg)
        self.assertIn("Govt deadlines", msg)


class Bot(unittest.TestCase):
    def test_commands(self):
        from jobhunter.bot import handle
        db = DB(HOME / "data" / "b.db")
        j = seed(db, "Data Scientist", "Sarvam AI")
        self.assertIn("Sarvam AI", handle("/top", db))
        self.assertIn("Sarvam AI", handle(f"/show_{j.id[:8]}", db))
        self.assertIn("skipped", handle(f"/skip {j.id[:8]}", db))
        self.assertEqual(db.get(j.id)["status"], "skipped")
        self.assertIn("interview", handle(f"/interview_{j.id[:8]}@JobBot", db))
        self.assertIn("tracked", handle("/stats", db))
        self.assertIn("Unknown", handle("/nope", db))
        self.assertIn("/help", handle("hello", db))
        self.assertIn("Usage", handle("/show", db))


class LLM(unittest.TestCase):
    def test_auto_falls_back_to_none(self):
        from jobhunter import llm
        os.environ["LLM_PROVIDER"] = "auto"
        for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY", "NVIDIA_API_KEYS"):
            os.environ.pop(k, None)
        llm._PICKED.clear()
        llm.PRESETS["ollama"] = ("http://127.0.0.1:9/v1", "x")     # nothing listens on port 9
        self.assertFalse(llm.available())
        self.assertIsNone(llm.chat("s", "u"))
        os.environ["LLM_PROVIDER"] = "none"


class Backup(unittest.TestCase):
    def test_roundtrip(self):
        from jobhunter import ops
        (HOME / "data" / "answers.yaml").write_text("q: {answer: '1'}\n")
        DB().upsert(Job("AI Engineer", "Backup Co", "Bengaluru"))
        f = ops.backup()
        self.assertTrue(f.name.endswith(".tar.gz.enc"))
        self.assertNotIn(b"Backup Co", f.read_bytes())               # encrypted
        (HOME / "data" / "answers.yaml").unlink()
        ops.restore(str(f))
        self.assertTrue((HOME / "data" / "answers.yaml").is_file())
        with self.assertRaises(SystemExit):
            ops.restore(str(f), key="wrong-key")


if __name__ == "__main__":
    unittest.main()
