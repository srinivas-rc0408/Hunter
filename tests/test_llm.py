"""LLM chain: NVIDIA multi-key rotation, model fallback, <think> stripping, .env parsing, privacy."""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from _home import make_home, use

HOME = make_home()


def setUpModule():
    use(HOME)


class Fake(BaseHTTPRequestHandler):
    """OpenAI-compatible endpoint. Key 'bad' → 401, 'busy' → 429, 'good' → answers.
    Model 'gone/model' → 404. Records every request."""
    log: list = []
    models = ["deepseek-ai/deepseek-v4-flash", "qwen/qwen3-next-80b-a3b-instruct", "nvidia/llama-guard-safety",
              "gone/model", "meta/llama-3.3-70b-instruct"]
    reply = "<think>planning…</think>Hello from the model"
    retire_ds = False

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(200, {"data": [{"id": m} for m in Fake.models]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        key = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        Fake.log.append((key, body["model"], body))
        if key == "bad":
            return self._send(401, {"error": "invalid key"})
        if key == "busy":
            return self._send(429, {"error": "rate limit"})
        if body["model"] == "gone/model":
            return self._send(404, {"error": "model not found"})
        if body["model"] == "deepseek-ai/deepseek-v4-flash" and Fake.retire_ds:
            return self._send(410, {"error": "gone"})
        self._send(200, {"choices": [{"message": {"content": Fake.reply}}]})


class Chain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        from jobhunter import llm
        self.llm = llm
        for k in ("NVIDIA_API_KEY", "NVIDIA_API_KEYS", "NVIDIA_API_KEY_2", "NVIDIA_API_KEY_3", "NVIDIA_MODEL",
                  "GEMINI_API_KEY", "GROQ_API_KEY"):
            os.environ.pop(k, None)
        llm._PICKED.clear(); llm._DEAD.clear(); llm._REST.clear(); llm._TURN.clear(); llm.LAST.clear(); llm._GONE.clear(); Fake.retire_ds = False
        self.saved = dict(llm.PRESETS)
        llm.PRESETS["nvidia"] = (self.base, "deepseek-ai/deepseek-v4-flash")
        llm.PRESETS["ollama"] = ("http://127.0.0.1:9/v1", "qwen3:4b")      # nothing listens on port 9
        os.environ["LLM_PROVIDER"] = "auto"
        Fake.log.clear()
        Fake.reply = "<think>planning…</think>Hello from the model"

    def tearDown(self):
        self.llm.PRESETS.clear(); self.llm.PRESETS.update(self.saved)
        os.environ["LLM_PROVIDER"] = "none"

    def test_keys_from_every_form(self):
        os.environ["NVIDIA_API_KEYS"] = "a, b,a"
        os.environ["NVIDIA_API_KEY"] = "c"
        os.environ["NVIDIA_API_KEY_3"] = "d"
        self.assertEqual(self.llm.keys("nvidia"), ["a", "b", "c", "d"])

    def test_bad_and_busy_keys_are_skipped(self):
        os.environ["NVIDIA_API_KEYS"] = "bad,busy,good"
        out = self.llm.chat("sys", "hi")
        self.assertEqual(out, "Hello from the model")             # <think> block stripped
        self.assertEqual([k for k, _, _ in Fake.log], ["bad", "busy", "good"])
        self.assertIn("bad", self.llm._DEAD)
        self.assertIn("busy", self.llm._REST)
        Fake.log.clear()
        self.llm.chat("sys", "again")
        self.assertEqual([k for k, _, _ in Fake.log], ["good"])   # dead and resting keys not retried
        self.assertEqual(self.llm.LAST["prov"], "nvidia")

    def test_all_keys_bad_means_none(self):
        os.environ["NVIDIA_API_KEYS"] = "bad,busy"
        self.assertIsNone(self.llm.chat("sys", "hi"))              # Ollama is down too → templates

    def test_retired_model_falls_back(self):
        os.environ["NVIDIA_API_KEY"] = "good"
        os.environ["NVIDIA_MODEL"] = "gone/model"
        self.llm.PRESETS["nvidia"] = (self.base, "x")
        Fake.models = Fake.models + []
        self.assertEqual(self.llm.chat("s", "u"), "Hello from the model")
        self.assertEqual([m for _, m, _ in Fake.log], ["gone/model", "deepseek-ai/deepseek-v4-flash"])

    def test_missing_model_picks_best_live_and_skips_guard_models(self):
        os.environ["NVIDIA_API_KEY"] = "good"
        old = Fake.models
        Fake.models = ["nvidia/llama-guard-safety", "meta/llama-3.3-70b-instruct", "qwen/qwen3-next-80b-a3b-instruct"]
        try:
            st = {r["prov"]: r for r in self.llm.status()}
            self.assertEqual(st["nvidia"]["model"], "qwen/qwen3-next-80b-a3b-instruct")
            self.assertNotIn("nvidia/llama-guard-safety", st["nvidia"]["fallbacks"])
            self.assertFalse(st["ollama"]["ok"])
        finally:
            Fake.models = old

    def test_listed_but_retired_410_moves_on_and_remembers(self):
        os.environ["NVIDIA_API_KEYS"] = "good,good2"
        Fake.retire_ds = True
        self.assertEqual(self.llm.chat("s", "u"), "Hello from the model")
        self.assertEqual([m for _, m, _ in Fake.log][:2],
                         ["deepseek-ai/deepseek-v4-flash", "qwen/qwen3-next-80b-a3b-instruct"])
        Fake.log.clear()
        self.llm.chat("s", "again")
        self.assertEqual([m for _, m, _ in Fake.log], ["qwen/qwen3-next-80b-a3b-instruct"])

    def test_unclosed_think_is_no_answer(self):
        self.assertEqual(self.llm.clean("<think>still thinking"), "")
        self.assertEqual(self.llm.clean("Answer <think>x</think>done"), "Answer done")

    def test_qwen3_on_ollama_gets_no_think(self):
        self.assertTrue(self.llm._system_for("ollama", "qwen3:4b", "S").endswith("/no_think"))
        self.assertEqual(self.llm._system_for("nvidia", "qwen/qwen3-next-80b-a3b-instruct", "S"), "S")

    def test_none_disables(self):
        os.environ["LLM_PROVIDER"] = "none"
        os.environ["NVIDIA_API_KEY"] = "good"
        self.assertFalse(self.llm.available())
        self.assertIsNone(self.llm.chat("s", "u"))
        self.assertEqual(Fake.log, [])

    def test_cover_letter_never_sends_phone_or_email(self):
        from jobhunter import config, tailor
        os.environ["NVIDIA_API_KEY"] = "good"
        p = config.cfg("profile")
        Fake.reply = (f"Dear Acme hiring team,\n\nI'm applying for the ML Intern role at Acme.\n\nRegards,\n"
                      f"{p['name']}\n[PHONE] · [EMAIL]")
        letter, _, _ = tailor.cover({"id": "x" * 16, "title": "ML Intern", "company": "Acme",
                                     "description": "Python, PyTorch"})
        sent = json.dumps([b for _, _, b in Fake.log])
        self.assertTrue(Fake.log)
        self.assertNotIn(str(p["email"]), sent)
        self.assertNotIn(str(p["phone_digits"]), sent)
        self.assertNotIn(str(p["phone"]), sent)


class EnvFile(unittest.TestCase):
    def test_empty_line_does_not_hide_real_value(self):
        from jobhunter import config
        f = config.path(".env")
        f.write_text("NVIDIA_API_KEYS=\n# later\nNVIDIA_API_KEYS=nvapi-real\nexport JH_X=\"1\"\n")
        try:
            os.environ.pop("NVIDIA_API_KEYS", None); os.environ.pop("JH_X", None)
            config._FROM_FILE.clear()
            self.assertEqual(config.env("NVIDIA_API_KEYS"), "nvapi-real")
            self.assertEqual(config.env("JH_X"), "1")
            os.environ["NVIDIA_API_KEYS"] = "set-at-runtime"          # runtime value wins over the file
            self.assertEqual(config.env("NVIDIA_API_KEYS"), "set-at-runtime")
        finally:
            f.unlink()
            os.environ.pop("NVIDIA_API_KEYS", None); os.environ.pop("JH_X", None)


if __name__ == "__main__":
    unittest.main()
