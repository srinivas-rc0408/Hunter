"""Self-health: /health, alerts sent once (again only after recovery), the weekly report."""
import datetime as dt
import unittest
from unittest import mock

from _home import use, make_home

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter import health  # noqa: E402
from jobhunter.db import DB, Job  # noqa: E402
from test_ops import assert_telegram_html  # noqa: E402


class Health(unittest.TestCase):
    def test_health_reply_lists_checks_with_fix_commands(self):
        rows = [(True, "Telegram alerts", ""), (False, "jobhunter-bot.service: failed", "systemctl --user restart jobhunter-bot")]
        with mock.patch.object(health, "checks", return_value=rows):
            out = health.as_telegram(health.checks())
        assert_telegram_html(self, out)
        self.assertIn("✓ Telegram alerts", out)
        self.assertIn("✗ jobhunter-bot.service: failed", out)
        self.assertIn("<code>systemctl --user restart jobhunter-bot</code>", out)
        self.assertIn("1 problem", out)

    def test_alert_once_then_again_only_after_recovery(self):
        sent = []
        probs = {"disk": ("Disk almost full: 1.2 GB free", "free some space")}
        with mock.patch.object(health, "telegram", lambda m: sent.append(m) or True):
            with mock.patch.object(health, "problems", return_value=probs):
                health.watch()
                health.watch()                                     # still broken: no second alert
            self.assertEqual(len(sent), 1)
            self.assertIn("Disk almost full", sent[0])
            self.assertIn("free some space", sent[0])
            with mock.patch.object(health, "problems", return_value={}):
                health.watch()                                     # recovered: one "fixed" note
                health.watch()
            self.assertEqual(len(sent), 2)
            self.assertIn("✅", sent[1])
            with mock.patch.object(health, "problems", return_value=probs):
                health.watch()                                     # broken again: alert again
            self.assertEqual(len(sent), 3)
        for m in sent:
            assert_telegram_html(self, m)

    def test_problem_detectors(self):
        from jobhunter import llm
        now = dt.datetime.now().timestamp()
        with mock.patch.object(health.shutil, "disk_usage", return_value=mock.Mock(free=1.5e9)):
            self.assertIn("disk", health._disk())
        with mock.patch.object(health.shutil, "disk_usage", return_value=mock.Mock(free=50e9)):
            self.assertEqual(health._disk(), {})
        llm._mark_down(True)
        self.assertEqual(health._llm(), {})                        # failing, but not for 24 h yet
        st = llm._state()
        st["_down_since"] = now - 25 * 3600
        llm._state_file().write_text(__import__("json").dumps(st))
        msg = health._llm()["llm"][0]
        self.assertIn("24 h", msg)
        self.assertIn("templates", msg)                           # still works without AI: say so
        llm._mark_down(False)
        self.assertEqual(health._llm(), {})
        health.set_flag("linkedin_logged_out", True)
        self.assertIn("hunt login linkedin", health._logins()["linkedin_logged_out"][1])
        health.set_flag("linkedin_logged_out", False)
        self.assertEqual(health._logins(), {})
        runs = '[{"conclusion":"failure","status":"completed"},{"conclusion":"failure","status":"completed"}]'
        with mock.patch.object(health, "_gh", return_value=runs), \
                mock.patch("jobhunter.morning.cloud_heartbeat", return_value=("x", 0.5)):
            self.assertIn("last 2 cloud radar runs failed", health._radar()["radar"][0])
        with mock.patch.object(health, "_gh", return_value='[{"conclusion":"success","status":"completed"}]'), \
                mock.patch("jobhunter.morning.cloud_heartbeat", return_value=("x", 7.0)):
            self.assertIn("6 h", health._radar()["radar"][0])
        with mock.patch.object(health, "_gh", return_value='[{"conclusion":"success","status":"completed"}]'), \
                mock.patch("jobhunter.morning.cloud_heartbeat", return_value=("x", 0.5)):
            self.assertEqual(health._radar(), {})

    def test_unit_restarted_three_times_or_failed(self):
        def show(unit):
            return {"jobhunter-bot.service": ("active", 4), "jobhunter-review.service": ("failed", 0)}.get(unit, ("active", 0))
        with mock.patch.object(health, "_unit", side_effect=show):
            p = health._units()
        self.assertIn("restarted 4 times", p["unit:jobhunter-bot.service"][0])
        self.assertIn("systemctl --user restart jobhunter-review", p["unit:jobhunter-review.service"][1])
        self.assertEqual(len(p), 2)

    def test_systemd_on_failure_alerts_once(self):
        health._save({})
        sent = []
        with mock.patch.object(health, "telegram", lambda m: sent.append(m) or True):
            health.unit_failed("jobhunter-scan.service")             # what jobhunter-alert@%n passes
            health.unit_failed("jobhunter-scan.service")             # fails again before recovery: quiet
            self.assertEqual(len(sent), 1)
            self.assertIn("jobhunter-scan failed", sent[0])
            self.assertIn("systemctl --user restart jobhunter-scan", sent[0])
            with mock.patch.object(health, "problems", return_value={}):
                health.watch()                                         # recovered: one ✅ note
            self.assertIn("✅ fixed: jobhunter-scan failed", sent[1])
        assert_telegram_html(self, sent[0])

    def test_weekly_report(self):
        db = DB(HOME / "data" / "weekly.db")
        for i, (co, st) in enumerate([("Acme", "applied"), ("Acme", "interview"), ("Zeta", "offer"), ("Beta", "failed")]):
            j = Job(f"Role {'ABCD'[i] * 3}", co, "Bengaluru", url="https://x.example/j")
            db.upsert(j)
            db.set_status(j.id, "applied" if st != "failed" else "new")
            if st in ("interview", "offer"):
                db.set_status(j.id, st)
            db.add_application(j.id, "linkedin", "applied" if st != "failed" else "failed", False, True, False,
                               "sent" if st != "failed" else "page took too long", [], "", "", None, "", "")
        out = health.weekly(db, dt.date.today())
        assert_telegram_html(self, out)
        for want in ("Applied 3", "Interviews 1", "Offers 1", "Acme (2)", "page took too long"):
            self.assertIn(want, out)


if __name__ == "__main__":
    unittest.main()
