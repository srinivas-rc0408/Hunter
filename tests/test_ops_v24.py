"""v2.4 ops: git-tag version, exact pins, safe library upgrades, weekly tidy."""
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _home import ROOT, make_home, use

HOME = make_home()


def setUpModule():
    use(HOME)


from jobhunter import ops, _git_version  # noqa: E402
from jobhunter.db import DB  # noqa: E402


class Version(unittest.TestCase):
    def test_git_tag(self):
        self.assertEqual(_git_version(lambda *a, **k: SimpleNamespace(returncode=0, stdout="v2.4-3-gabc123\n")),
                         "v2.4-3-gabc123")

    def test_fallback_without_git(self):
        def missing(*a, **k):
            raise FileNotFoundError("git")
        self.assertEqual(_git_version(missing), "v2.5")
        self.assertEqual(_git_version(lambda *a, **k: SimpleNamespace(returncode=128, stdout="")), "v2.5")
        self.assertEqual(_git_version(mock.Mock(side_effect=subprocess.TimeoutExpired("git", 3))), "v2.5")


class Pins(unittest.TestCase):
    def test_every_requirement_is_exact(self):
        for name in ("requirements.txt", "requirements-radar.txt", "requirements-ml.txt"):
            for line in (ROOT / name).read_text().splitlines():
                if line.strip() and not line.startswith(("#", "--")):
                    self.assertRegex(line, r"^[A-Za-z0-9_.\-\[\]]+==[0-9]", f"{name}: {line}")


class UpdateCheck(unittest.TestCase):
    PKG = "python-jobspy"

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="jh-req-"))
        self.req = self.d / "requirements.txt"
        shutil.copy(ROOT / "requirements.txt", self.req)
        self.cmds = []

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_check(self, latest, tests_ok=True):
        def run(cmd, **kw):
            self.cmds.append((cmd, kw))
            failed = not tests_ok and "unittest" in cmd
            return SimpleNamespace(returncode=1 if failed else 0, stdout="",
                                   stderr="Ran 3 tests\n\nFAILED (failures=1)\n" if failed else "OK\n")
        pypi = mock.Mock(return_value=mock.Mock(json=lambda: {"info": {"version": latest}}))
        with mock.patch("requests.get", pypi):
            return ops.update_check((self.PKG,), run=run, req_files=(self.req,))[self.PKG]

    def test_up_to_date(self):
        have = version(self.PKG)
        r = self.run_check(have)
        self.assertEqual(r, {"installed": have, "latest": have, "action": "up to date"})
        self.assertEqual(self.cmds, [])

    def test_upgraded_when_tests_pass(self):
        r = self.run_check("99.0.0")
        self.assertEqual(r["action"], "upgraded")
        self.assertIn(f"{self.PKG}==99.0.0\n", self.req.read_text())
        target = self.cmds[0][0]
        self.assertIn("--target", target)                       # trial install never touches the venv
        tests = [(c, kw) for c, kw in self.cmds if "unittest" in c]
        self.assertEqual(len(tests), 2)
        tmp = target[target.index("--target") + 1]
        for _, kw in tests:
            self.assertTrue(kw["env"]["PYTHONPATH"].startswith(tmp + os.pathsep))
        self.assertEqual(self.cmds[-1][0][-1], f"{self.PKG}==99.0.0")   # real install only after the tests

    def test_kept_when_tests_fail(self):
        before = self.req.read_text()
        r = self.run_check("99.0.0", tests_ok=False)
        self.assertEqual(r["action"], "kept (tests failed: FAILED (failures=1))")
        self.assertEqual(self.req.read_text(), before)
        self.assertFalse(any(c[-1] == f"{self.PKG}==99.0.0" and "--target" not in c for c, _ in self.cmds))

    def test_pypi_error(self):
        with mock.patch("requests.get", mock.Mock(side_effect=OSError("offline"))):
            r = ops.update_check((self.PKG,), run=None, req_files=(self.req,))[self.PKG]
        self.assertEqual(r["action"], "error: offline")


class Tidy(unittest.TestCase):
    def test_vacuum_old_logs_and_screens(self):
        data = HOME / "data"
        old = time.time() - 15 * 86400
        files = {}
        for rel in ("logs/old.log", "logs/new.log", "screens/old.png", "screens/new.png"):
            f = data / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
            if "old" in rel:
                os.utime(f, (old, old))
            files[rel] = f
        self.assertEqual(ops.tidy(DB()), 1)
        self.assertFalse(files["logs/old.log"].exists())
        self.assertFalse(files["screens/old.png"].exists())
        self.assertTrue(files["logs/new.log"].exists())
        self.assertTrue(files["screens/new.png"].exists())


if __name__ == "__main__":
    unittest.main()
