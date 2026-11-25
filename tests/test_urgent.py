"""🚨 APPLY NOW (score ≥ 95): own pinned message, queued first on the laptop, reminders at 2 h and 6 h, max 3 a day."""
import datetime as dt
import unittest
from unittest import mock

from _home import make_home, use

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter import urgent  # noqa: E402
from jobhunter.db import DB, Job  # noqa: E402
from jobhunter.util import IST, iso  # noqa: E402
from test_ops import assert_telegram_html  # noqa: E402


class Urgent(unittest.TestCase):
    def setUp(self):
        self.db = DB()
        for t in ("jobs", "events"):
            self.db.c.execute(f"DELETE FROM {t}")
        self.db.c.commit()
        self.sent, self.calls, self.mid = [], [], 100

        def telegram_id(text):
            self.mid += 1
            self.sent.append(text)
            return self.mid
        self.p = [mock.patch("jobhunter.notify.telegram_id", side_effect=telegram_id),
                  mock.patch("jobhunter.notify.telegram", side_effect=lambda t: bool(telegram_id(t))),
                  mock.patch("jobhunter.notify.tg", side_effect=lambda m, **k: self.calls.append((m, k)) or True)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def job(self, score, title="GenAI Engineer", kind="private", hours_ago=3):
        j = Job(title, f"Acme {title}", "Bengaluru, Karnataka", url=f"https://boards.greenhouse.io/acme/{title}{score}",
                source="greenhouse", kind=kind)
        j.posted_at = iso(dt.datetime.now(IST) - dt.timedelta(hours=hours_ago))
        self.db.upsert(j)
        self.db.set_score(j.id, score, ["Python, FastAPI, LLMs: all in your resume", "fresher role"], [], False)
        return self.db.get(j.id)

    def age(self, j, hours):
        self.db.c.execute("UPDATE events SET at=? WHERE job_id=? AND kind='urgent'",
                          (iso(dt.datetime.now(IST) - dt.timedelta(hours=hours)), j["id"]))
        self.db.c.commit()

    def test_the_alert_is_whole_pinned_and_queued_first(self):
        j = self.job(97)
        self.assertTrue(urgent.send(self.db, j, laptop=True))
        msg = self.sent[0]
        assert_telegram_html(self, msg)
        self.assertTrue(msg.startswith("🚨 <b>APPLY NOW</b> (score 97) · posted 3 h ago"))
        for part in ("GenAI Engineer", "Why it matched", "Python, FastAPI, LLMs", "Apply on company site",
                     f"/check_{j['id'][:8]}", f"/laptop_{j['id'][:8]}"):
            self.assertIn(part, msg)
        self.assertEqual(self.calls, [("pinChatMessage", {"message_id": 101, "disable_notification": False})])
        self.assertEqual(self.db.get(j["id"])["status"], "queued")          # laptop on: first in `hunt apply`
        from jobhunter.apply.runner import queue
        self.job(99, "Other")                                                # higher score, but not urgent-queued
        self.assertEqual(queue(self.db, None, 5, 40)[0]["id"], j["id"])
        k = self.job(96, "Cloud")
        urgent.send(self.db, k, laptop=False)                                # cloud radar: alert only
        self.assertEqual(self.db.get(k["id"])["status"], "alerted")

    def test_never_more_than_3_a_day_the_rest_are_normal(self):
        jobs = [self.job(s, f"R{s}") for s in (95, 99, 97, 96, 80)] + [self.job(99, "Govt", kind="govt")]
        hot, rest = urgent.split(self.db, jobs)
        self.assertEqual([j["score"] for j in hot], [99, 97, 96])
        self.assertEqual(sorted(j["score"] for j in rest), [80, 95, 99])    # the 95, the 80 and the govt notice
        for j in hot:
            urgent.send(self.db, j, laptop=True)
        hot, rest = urgent.split(self.db, [self.job(98, "Later")])
        self.assertEqual((hot, len(rest)), ([], 1))                          # cap reached today
        tomorrow = dt.datetime.now(IST) + dt.timedelta(days=1)
        self.assertEqual(len(urgent.split(self.db, rest, now=tomorrow)[0]), 1)

    def test_reminders_at_2h_and_6h_then_stop(self):
        j = self.job(97)
        urgent.send(self.db, j, laptop=True)
        self.sent.clear()
        self.assertEqual(urgent.tick(self.db), 0)                            # 0 h: nothing
        self.age(j, 2.1)
        self.assertEqual(urgent.tick(self.db), 1)
        self.assertIn("Still open</b> (2 h)", self.sent[-1])
        assert_telegram_html(self, self.sent[-1])
        self.assertEqual(urgent.tick(self.db), 0)                            # once
        self.age(j, 6.1)
        self.assertEqual(urgent.tick(self.db), 1)
        self.assertIn("(6 h)", self.sent[-1])
        self.assertIn("Last reminder", self.sent[-1])
        self.age(j, 30)
        self.assertEqual(urgent.tick(self.db), 0)                            # then silence
        k = self.job(98, "Off")                                              # laptop was off for 7 h: one reminder, not two
        urgent.send(self.db, k, laptop=True)
        self.age(k, 7)
        self.assertEqual(urgent.tick(self.db), 1)
        self.assertEqual(urgent.tick(self.db), 0)

    def test_unpinned_when_applied_or_skipped(self):
        a, b = self.job(97, "A"), self.job(96, "B")
        urgent.send(self.db, a, laptop=True)
        urgent.send(self.db, b, laptop=True)
        self.calls.clear()
        self.db.set_status(a["id"], "applied", "test")
        urgent.tick(self.db)
        self.assertEqual(self.calls, [("unpinChatMessage", {"message_id": 101})])
        self.age(a, 3)
        self.sent.clear()
        urgent.tick(self.db)
        self.assertFalse(any("Still open" in s and "A ·" in s for s in self.sent))   # applied: no reminders
        self.calls.clear()
        from jobhunter.bot import handle
        handle(f"/skip_{b['id'][:8]}", self.db)                              # skip from Telegram: unpinned at once
        self.assertEqual(self.calls, [("unpinChatMessage", {"message_id": 102})])
        urgent.tick(self.db)
        self.assertEqual(len(self.calls), 1)                                 # only once


if __name__ == "__main__":
    unittest.main()
