"""Pick the chat assistant's model: every candidate NVIDIA model × the 12 example messages, first planning step.

    .venv/bin/python scripts/bench_assistant.py [max_models]

Prints, per model: valid JSON, right tool, median and worst seconds. Put the winner first in ASSISTANT_MODEL
(.env, comma-separated, best first) or in assistant.MODELS. Keys are shown masked only.
"""
from __future__ import annotations

import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__file__)))
from jobhunter import assistant, llm  # noqa: E402

EXAMPLES = [   # message → tools a good model may pick (or "answer")
    ("find ML intern jobs in Bengaluru posted this week", {"search_jobs"}),
    ("top 5 GenAI roles at product companies", {"search_jobs", "top_jobs"}),
    ("what does the Birlasoft job need that I don't have?", {"explain_job"}),
    ("is f0512c6c a good fit for me?", {"explain_job"}),
    ("how much would a fresher AI engineer get at Zoho?", {"salary_estimate", "answer"}),
    ("mark ab12cd34 as interview", {"set_status"}),
    ("add a column Referral in the sheet", {"sheet_add_column", "answer"}),
    ("put 'asked Rahul for referral' in Referral for ab12cd34", {"sheet_set", "add_note", "answer"}),
    ("remove ab12cd34 from the sheet", {"sheet_remove_row", "answer"}),
    ("send me the sheet", {"sheet_send"}),
    ("what did I apply to this week?", {"applied"}),
    ("any govt jobs closing soon?", {"govt"}),
]
NOT_CHAT = re.compile(r"detector|cosmos|ising|gliner|riva|nvclip", re.I)    # llm._ok_id drops embed/guard/safety/coder


def main(max_models: int = 14) -> None:
    p = next((x for x in llm._providers() if x["prov"] == "nvidia"), None)
    ks = llm.keys("nvidia")
    if not p or not ks:
        sys.exit("No NVIDIA key works: set NVIDIA_API_KEYS in .env")
    print(f"keys: {', '.join(llm.mask(k) for k in ks)} · {len(p['ids'])} models live")
    models = [m for m in p["ids"] if llm._ok_id(m) and not NOT_CHAT.search(m)][:max_models]
    system = assistant._system()

    def bench(i_model):
        i, model = i_model
        rows = []
        for j, (msg, want) in enumerate(EXAMPLES):
            key = ks[(i + j) % len(ks)]
            t0 = time.time()
            code, text = llm._post(p, model, key, system, f"User: {msg}", 0, 500, timeout=20)
            secs = time.time() - t0
            obj = assistant._parse(text or "") if code == 200 else None
            got = None if obj is None else ("answer" if "answer" in obj else obj.get("tool"))
            rows.append((secs, obj is not None, got in want, code))
        return model, rows
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(bench, enumerate(models)))
    print(f"\n{'model':48} {'json':>5} {'right':>6} {'median':>7} {'worst':>6}  errors")
    table = []
    for model, rows in res:
        ok = [r for r in rows if r[1]]
        med = statistics.median([r[0] for r in rows])
        worst = max(r[0] for r in rows)
        errs = sorted({r[3] for r in rows if r[3] != 200})
        table.append((sum(r[2] for r in rows), -med, model, len(ok), med, worst, errs))
        print(f"{model:48} {len(ok):>3}/12 {sum(r[2] for r in rows):>4}/12 {med:>6.1f}s {worst:>5.1f}s  {errs or ''}")
    good = sorted([t for t in table if t[3] == 12 and t[0] >= 11], key=lambda t: (t[4], -t[0]))
    print("\nASSISTANT_MODEL=" + ",".join(t[2] for t in good[:3]) if good else "\nno model got 12/12 valid JSON")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 14)
