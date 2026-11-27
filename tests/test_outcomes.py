"""v2.6: outcomes from Telegram buttons, response rate by source/role/week, ranking that learns from outcomes,
secrets kept owner-only and out of git, and an alert when a source goes quiet."""
import datetime as dt
import json
import os
import stat
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from _home import make_home, use

HOME = make_home()
ROOT = Path(__file__).resolve().parent.parent


def setUpModule():
    use(HOME)


from jobhunter import notify, ops, outcomes, pipeline  # noqa: E402
from jobhunter.db import DB, Job  # noqa: E402
from jobhunter.notify import job_card  # noqa: E402


def seed(db, title, company, source="linkedin", status="new", desc="Python, PyTorch", applied_days_ago=None):
    j = Job(title, company, "Bengaluru, Karnataka", url=f"https://x.example/{company}/{title}".replace(" ", ""),
            source=source, description=desc, posted_at=str(dt.date.today()))
    db.upsert(j)
    if status != "new":
        db.set_status(j.id, status)
    if applied_days_ago is not None:
        at = (dt.datetime.now() - dt.timedelta(days=applied_days_ago)).isoformat(timespec="seconds")
        db.c.execute("DELETE FROM events WHERE job_id=? AND kind='applied'", (j.id,))
        db.c.execute("INSERT INTO events VALUES (?,?,?,?)", (j.id, at, "applied", "test"))
        db.c.commit()
    return db.get(j.id)


def labels(kb):
    return [b["text"] for row in (kb or {}).get("inline_keyboard", []) for b in row]


class Buttons(unittest.TestCase):
    def setUp(self):
        self.db = DB(HOME / "data" / f"{self._testMethodName}.db")

    def test_one_card_gets_the_buttons_for_its_next_outcome(self):
        j = seed(self.db, "ML Intern", "Acme")
        self.assertEqual(labels(outcomes.buttons_for(job_card(j), self.db)), ["✅ Applied", "⏭ Skip"])
        self.db.set_status(j["id"], "applied")
        kb = outcomes.buttons_for(job_card(self.db.get(j["id"])), self.db)
        self.assertEqual(labels(kb), ["🎤 Interview", "✗ Rejected", "👻 Ghosted"])
        self.assertEqual(kb["inline_keyboard"][0][2]["callback_data"], f"o:ghosted:{j['id'][:12]}")
        self.assertLessEqual(max(len(b["callback_data"].encode()) for b in kb["inline_keyboard"][0]), 64)   # Telegram's cap

    def test_a_list_of_cards_or_a_form_waiting_for_approval_gets_none(self):
        a, b = seed(self.db, "ML Intern", "Acme"), seed(self.db, "AI Engineer", "Beta")
        self.assertIsNone(outcomes.buttons_for(job_card(a) + "\n\n" + job_card(b), self.db))
        self.db.set_status(a["id"], "needs_you")
        self.assertIsNone(outcomes.buttons_for(job_card(self.db.get(a["id"])), self.db))

    def test_a_tap_records_the_outcome_and_moves_the_buttons_on(self):
        j = seed(self.db, "ML Intern", "Acme", status="applied")
        note, kb = outcomes.tap(self.db, f"o:interview:{j['id'][:12]}")
        self.assertEqual(self.db.get(j["id"])["status"], "interview")
        self.assertIn("🎤 Interview: ML Intern · Acme", note)
        self.assertEqual(labels(kb), ["🎉 Offer", "✗ Rejected", "👻 Ghosted"])
        ev = self.db.c.execute("SELECT detail FROM events WHERE job_id=? AND kind='interview'", (j["id"],)).fetchone()
        self.assertEqual(ev[0], "telegram button")
        note, kb = outcomes.tap(self.db, f"o:offer:{j['id'][:12]}")
        self.assertEqual(kb, {"inline_keyboard": []})                # nothing after an offer: the buttons go
        self.assertIn("old version", outcomes.tap(self.db, "o:hired:abcdef12")[0])
        self.assertIn("no longer", outcomes.tap(self.db, "o:applied:abcdef12")[0])

    def test_the_bot_only_takes_your_chats_buttons(self):
        j = seed(self.db, "ML Intern", "Acme")
        calls = []
        post = lambda method, **kw: calls.append((method, kw))  # noqa: E731
        cq = {"id": "q1", "data": f"o:applied:{j['id'][:12]}", "message": {"message_id": 7, "chat": {"id": 999}}}
        with mock.patch.object(outcomes, "DB", lambda: self.db):
            outcomes.on_button("api", "42", cq, post)
            self.assertEqual(self.db.get(j["id"])["status"], "new")       # someone else's chat: nothing changes
            self.assertEqual(calls[0][1]["text"], "Not your chat.")
            calls.clear()
            outcomes.on_button("api", "42", {**cq, "message": {"message_id": 7, "chat": {"id": 42}}}, post)
        self.assertEqual(self.db.get(j["id"])["status"], "applied")
        self.assertEqual([c[0] for c in calls], ["answerCallbackQuery", "editMessageReplyMarkup"])
        self.assertEqual(calls[1][1]["message_id"], 7)
        self.assertEqual(labels(calls[1][1]["reply_markup"]), ["🎤 Interview", "✗ Rejected", "👻 Ghosted"])

    def test_alerts_and_bot_messages_carry_the_buttons(self):
        j = seed(self.db, "ML Intern", "Acme")
        sent = []
        fake = mock.MagicMock(ok=True)
        fake.json.return_value = {"result": {"message_id": 1}}
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42"}), \
                mock.patch.object(outcomes, "DB", lambda: self.db), \
                mock.patch("requests.post", lambda url, json=None, **k: sent.append(json) or fake):
            notify.alert_jobs([j])
            from jobhunter import bot
            bot.send("api", "42", job_card(j))
            bot.send("api", "42", "plain text, no card")
        self.assertEqual(labels(sent[0]["reply_markup"]), ["✅ Applied", "⏭ Skip"])
        self.assertEqual(labels(sent[1]["reply_markup"]), ["✅ Applied", "⏭ Skip"])
        self.assertNotIn("reply_markup", sent[2])


class Ghosted(unittest.TestCase):
    def test_no_reply_in_21_days_asks_once_and_still_waiting_asks_again_in_2_weeks(self):
        db = DB(HOME / "data" / "ghost.db")
        old = seed(db, "ML Intern", "Silentco", status="applied", applied_days_ago=22)
        seed(db, "AI Engineer", "Recentco", status="applied", applied_days_ago=5)
        seed(db, "Data Scientist", "Repliedco", status="rejected", applied_days_ago=30)
        got = outcomes.ghost_prompts(db)
        self.assertEqual(len(got), 1)
        self.assertIn("No reply from Silentco in 22 days", got[0][0])
        self.assertEqual(labels(got[0][1]), ["🎤 Interview", "✗ Rejected", "👻 Ghosted", "⏳ Still waiting"])
        self.assertEqual(outcomes.ghost_prompts(db), [])                       # asked once
        outcomes.tap(db, f"o:wait:{old['id'][:12]}")
        self.assertEqual(outcomes.ghost_prompts(db, today=dt.date.today() + dt.timedelta(days=13)), [])
        self.assertEqual(len(outcomes.ghost_prompts(db, today=dt.date.today() + dt.timedelta(days=14))), 1)


class Report(unittest.TestCase):
    def test_rates_by_source_role_and_week(self):
        db = DB(HOME / "data" / "report.db")
        for i, st in enumerate(["interview", "rejected", "ghosted", "applied"]):
            seed(db, f"Machine Learning Engineer {i}", f"Li{i}", source="linkedin", status=st, applied_days_ago=3)
        seed(db, "Data Scientist", "Gh1", source="greenhouse", status="offer", applied_days_ago=10)
        seed(db, "Data Scientist II", "Gh2", source="greenhouse", status="rejected", applied_days_ago=10)
        r = outcomes.report(db)
        self.assertIn("<b>linkedin</b>: 4 applied · 2 replied (50%) · 1 interview (25%) · 1 ghosted · 1 waiting", r)
        self.assertIn("<b>greenhouse</b>: 2 applied · 2 replied (100%) · 1 interview (50%) · 0 ghosted · 0 waiting", r)
        self.assertLess(r.index("<b>greenhouse</b>"), r.index("<b>linkedin</b>"))     # most interviews per try first
        self.assertIn("<b>Machine Learning Engineer</b>: 4 applied", r)
        self.assertIn("<b>Data Scientist</b>: 2 applied", r)
        self.assertIn("By week applied", r)
        self.assertIn("after 20 decided ones (5 so far)", r)
        self.assertIn("No applications yet", outcomes.report(DB(HOME / "data" / "empty.db")))


class Learning(unittest.TestCase):
    def history(self, db, n_win, n_lose):
        for i in range(n_win):
            seed(db, f"Machine Learning Intern {i}", f"Win{i}", status="interview", desc="Python PyTorch deep learning")
        for i in range(n_lose):
            seed(db, f"Frontend Web Developer {i}", f"Lose{i}", status=("rejected", "ghosted")[i % 2], desc="React CSS HTML")

    def test_nothing_changes_before_20_outcomes(self):
        db = DB(HOME / "data" / "learn19.db")
        self.history(db, 5, 14)
        self.assertIsNone(outcomes.model(db))
        self.assertEqual(outcomes.lift(outcomes.model(db), "Machine Learning Intern"), (0, None))

    def test_jobs_like_your_interviews_rank_higher_and_say_why(self):
        db = DB(HOME / "data" / "learn20.db")
        self.history(db, 6, 14)
        m = outcomes.model(db)
        self.assertEqual((m["n"], m["wins"]), (20, 6))
        pts, why = outcomes.lift(m, "Machine Learning Intern", "PyTorch and Python")
        self.assertGreater(pts, 0)
        self.assertLessEqual(pts, 10)
        self.assertIn("like jobs that got you interviews (Machine Learning Intern", why)
        self.assertEqual(outcomes.lift(m, "Frontend Web Developer", "React"), (0, None))  # only ever raises

    def test_a_scan_applies_the_lift(self):
        db = DB(HOME / "data" / "learnscan.db")
        self.history(db, 6, 14)
        ml = Job("Machine Learning Intern", "Newco", "Bengaluru, Karnataka", url="https://x/new-ml", source="indeed",
                 description="Python PyTorch freshers", posted_at=str(dt.date.today()), job_type="internship")
        base = Job(**{**ml.__dict__})
        from jobhunter.score import score
        before = score(base).score
        pipeline.save(db, [ml])
        row = db.get(ml.id)
        self.assertGreater(row["score"], before)
        self.assertTrue(any("got you interviews" in r for r in row["reasons"]))

    def test_the_20th_outcome_re_ranks_open_jobs(self):
        db = DB(HOME / "data" / "learn-tap.db")
        self.history(db, 6, 13)
        last = seed(db, "Frontend Web Developer X", "LoseX", status="applied")
        with mock.patch("jobhunter.rescore.rescore") as rescore:
            outcomes.tap(db, f"o:rejected:{last['id'][:12]}")
        rescore.assert_called_once_with(db)


class Secrets(unittest.TestCase):
    def mode(self, p):
        return stat.S_IMODE(os.stat(p).st_mode)

    def test_secure_makes_secrets_owner_only(self):
        env, priv, data = HOME / ".env", HOME / "config" / "private.yaml", HOME / "data"
        env.write_text("X=1\n")
        db = DB(data / "perm.db")
        for p in (env, priv, db.path):
            os.chmod(p, 0o644)
        os.chmod(data, 0o755)
        self.assertTrue(any("others can read" in x for x in ops.secrets_problems()))
        fixed = ops.secure()
        self.assertTrue(any(".env" in f for f in fixed), fixed)
        self.assertEqual([self.mode(p) for p in (env, priv, db.path, data)], [0o600, 0o600, 0o600, 0o700])
        self.assertEqual([x for x in ops.secrets_problems() if "others can read" in x], [])
        os.chmod(db.path, 0o644)
        DB(db.path)
        self.assertEqual(self.mode(db.path), 0o600)                  # every open locks the database down again

    def test_backups_are_encrypted_and_owner_only(self):
        if not __import__("shutil").which("openssl"):
            self.skipTest("openssl not installed")
        with mock.patch.dict(os.environ, {"STATE_KEY": "test-key"}):
            out = ops.backup(keep=2)
        raw = out.read_bytes()
        self.assertTrue(raw.startswith(b"Salted__"))
        self.assertNotIn(b"SQLite format 3", raw)
        self.assertEqual(self.mode(out), 0o600)
        self.assertEqual(self.mode(out.parent), 0o700)
        plain = out.parent / "jobhunter-plain.tar.gz.enc"
        plain.write_bytes(b"\x1f\x8b not encrypted")
        try:
            self.assertTrue(any("jobhunter-plain.tar.gz.enc is not encrypted" in x for x in ops.secrets_problems()))
        finally:
            plain.unlink()

    def test_this_repo_never_committed_a_secret(self):
        """The real checkout: .env, config/private.yaml, data/ and docs/private/ untracked and absent from all history."""
        git = lambda *a: subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True).stdout  # noqa: E731
        if not git("rev-parse", "--git-dir"):
            self.skipTest("not a git checkout")
        self.assertEqual([f for f in git("ls-files", "--", *ops.NEVER_COMMIT).split() if f != "data/.gitkeep"], [])
        ever = {f for f in git("log", "--all", "--format=", "--name-only", "--", *ops.NEVER_COMMIT).split()}
        self.assertEqual(ever - {"data/.gitkeep"}, set())
        ignored = git("check-ignore", ".env", "config/private.yaml", "data/jobs.db", "docs/private/x").split()
        self.assertEqual(len(ignored), 4, ignored)


class Silence(unittest.TestCase):
    def run_counts(self, db, source, counts, done=1):
        for n in counts:
            db.log_coverage(source, done, 10, n)

    def test_two_empty_runs_after_normal_ones_alert_once_then_say_when_its_back(self):
        db = DB(HOME / "data" / "silence.db")
        sent = []
        self.run_counts(db, "indeed", [40, 38, 41, 0])
        self.assertEqual(pipeline._silence_alerts(db, ["indeed"], sent.append), [])     # one empty run: not yet
        self.run_counts(db, "indeed", [0])
        pipeline._silence_alerts(db, ["indeed"], sent.append)
        self.assertEqual(len(sent), 1)
        self.assertIn("indeed returned 0 jobs 2 scans in a row", sent[0])
        self.assertIn("(41, 38, 40 in the scans before)", sent[0])
        self.run_counts(db, "indeed", [0])
        pipeline._silence_alerts(db, ["indeed"], sent.append)
        self.assertEqual(len(sent), 1)                                                     # once per episode
        self.run_counts(db, "indeed", [37])
        pipeline._silence_alerts(db, ["indeed"], sent.append)
        self.assertEqual(sent[-1], "✅ indeed is returning jobs again (37 this scan).")

    def test_a_source_that_is_usually_empty_or_was_rate_limited_never_alerts(self):
        db = DB(HOME / "data" / "silence2.db")
        sent = []
        self.run_counts(db, "govt radar", [0, 0, 1, 0, 0, 0])                 # new notices only: usually 0
        self.run_counts(db, "linkedin", [40, 40, 40])
        self.run_counts(db, "linkedin", [0, 0], done=0)                       # 429 on the first search: no data
        pipeline._silence_alerts(db, ["govt radar", "linkedin"], sent.append)
        self.assertEqual(sent, [])

    def test_a_company_board_whose_feed_empties_alerts(self):
        db = DB(HOME / "data" / "silence3.db")
        sent = []
        for n in (55, 54, 56, 0, 0):
            pipeline._remember(db, {"company boards": {"total": 1, "done": 1, "cut": False, "found": 0,
                                                       "marks": [("greenhouse:acme", [str(i) for i in range(n)], "2026-11-27")]}}, False)
        pipeline._silence_alerts(db, ["board greenhouse:acme"], sent.append)
        self.assertEqual(len(sent), 1)
        self.assertIn("board greenhouse:acme returned 0 jobs", sent[0])


if __name__ == "__main__":
    unittest.main()
