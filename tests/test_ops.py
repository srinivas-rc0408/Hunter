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


TG_TAGS = {"b", "i", "u", "s", "a", "code", "pre"}


def assert_telegram_html(tc, text):
    """What Telegram's HTML parse mode accepts: only its tags, every < and & escaped, tags balanced."""
    import re as _re
    stack = []
    for m in _re.finditer(r"<(/?)([a-zA-Z]*)([^>]*)>|<|&(?!(?:lt|gt|amp|quot|#\d+);)", text):
        if m.group(0) in ("<", "&") or m.group(2) not in TG_TAGS:
            tc.fail(f"invalid Telegram HTML {m.group(0)!r} in: {text[max(0, m.start() - 40):m.end() + 40]!r}")
        if m.group(1):
            tc.assertEqual(stack.pop() if stack else None, m.group(2))
        else:
            stack.append(m.group(2))
    tc.assertEqual(stack, [], "unclosed tag")


class BotHTML(unittest.TestCase):
    def test_every_reply_is_valid_telegram_html(self):
        from jobhunter.bot import handle
        db = DB(HOME / "data" / "html.db")
        j = seed(db, "ML <Intern> & R&D", "A&B <Labs>")
        for cmd in ("/help", "help", "/start", "top 5", "/top", "/stats", "/govt", "/review", "/ping", "/questions",
                    f"/show_{j.id[:8]}", "/show", "/answer", "/answer x", "/company", "/nope", "hello", "",
                    f"/queue {j.id[:8]}", "/skip"):
            out = handle(cmd, db)
            self.assertTrue(out.strip(), cmd)
            assert_telegram_html(self, out)
        self.assertIn("JobHunter bot", handle("help", db))
        self.assertIn("alive", handle("ping", db))

    def test_brief_is_valid_html(self):
        from jobhunter.morning import as_telegram, build
        db = DB(HOME / "data" / "html2.db")
        seed(db, "Data <Scientist> & co", "X&Y")
        assert_telegram_html(self, as_telegram(build(db=db, heartbeat=False)))

    def test_send_falls_back_to_plain_text(self):
        from unittest import mock

        from jobhunter import bot
        calls = []

        def post(url, timeout, json):
            calls.append(json)
            return mock.Mock(ok="parse_mode" not in json, status_code=400, text="can't parse entities")
        with mock.patch.object(bot.requests, "post", post):
            self.assertTrue(bot.send("http://x", "1", "<b>hi</b> &lt;id&gt;"))
        self.assertEqual(calls[1]["text"], "hi <id>")


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
