"""Resume versions (v1, v2 …) and response rate per version, /check talking points, weekly 'why no replies?'."""
import datetime as dt
import unittest

from _home import make_home, use

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter import config, selection  # noqa: E402


def row(applied, answered=False, fid="x"):
    return {"applied": applied, "answered": answered, "full_id": fid}


class Versions(unittest.TestCase):
    def setUp(self):
        f = selection._file()
        if f.exists():
            f.unlink()
        config.path("config", "resume.pdf").write_bytes(b"%PDF old")

    def test_old_resume_is_v1_then_new_ones_count_up(self):
        self.assertEqual(selection.versions()[0]["v"], 1)
        self.assertEqual(selection.register(b"%PDF new", dt.datetime(2026, 12, 1, 10)), "v2")
        self.assertEqual(selection.register(b"%PDF new"), "v2")                     # same file again: same label
        self.assertEqual(selection.register(b"%PDF old", dt.datetime(2026, 12, 5)), "v1")  # back to the old one
        vs = selection.versions()
        self.assertEqual(selection.version_at(dt.datetime(2026, 11, 20), vs), "v1")
        self.assertEqual(selection.version_at(dt.datetime(2026, 12, 2), vs), "v2")
        self.assertEqual(selection.version_at(dt.datetime(2026, 12, 6), vs), "v1")

    def test_rate_only_after_15_per_version(self):
        selection.register(b"%PDF new", dt.datetime(2026, 12, 1))
        R = [row(dt.datetime(2026, 11, 1), i < 3) for i in range(15)] + [row(dt.datetime(2026, 12, 2))] * 4
        got = {v: txt for v, _, _, txt in selection.by_version(R)}
        self.assertEqual(got, {"v1": "20%", "v2": "too few to tell (4 of 15)"})


class TalkingPoints(unittest.TestCase):
    def test_only_resume_projects_that_overlap_the_jd(self):
        r = config.cfg("resume")
        p = (r.get("projects") or [])[0]
        skill = (p.get("stack") or p.get("tags"))[0]
        tp = selection.talking_points({"title": "ML Intern", "description": f"We use {skill} daily."})
        self.assertTrue(tp and tp[0].startswith(p["name"]))
        self.assertEqual(selection.talking_points({"title": "Chef", "description": "Cook pasta."}), [])


class WhyNoReplies(unittest.TestCase):
    def test_lists_causes_and_one_fix(self):
        from jobhunter.db import DB, Job
        db, R = DB(), []
        for i in range(20):
            j = Job(title=f"Senior Data Engineer {i}", company=f"C{i}", location="Bengaluru", url=f"https://x.example/{i}",
                    source="linkedin", description="5+ years Kubernetes", posted_at="2026-11-01")
            db.upsert(j)
            R.append(row(dt.datetime(2026, 11, 10), False, j.id))
        t = selection.why_no_replies(db, R)
        self.assertIn("20 of 20 were senior", t)
        self.assertIn("more than 5 days after posting", t)
        self.assertIn("Kubernetes", t)
        self.assertIn("Try one thing", t)
        R[0]["answered"] = True
        self.assertIsNone(selection.why_no_replies(db, R))


if __name__ == "__main__":
    unittest.main()
