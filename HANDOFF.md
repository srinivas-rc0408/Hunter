# Batch C (Telegram AI) handoff, v2.7.0

- Done: 30-question eval (`python -m jobhunter.evals answers`, set in gitignored data/evals/): before 40% accurate / 40% cited / median 3.2 s / p95 10.8 s → after 86.7% / 70% / 1.5 s / 17.0 s.
- Done: named jobs read before the model (jobhunter/answer.py), job-id citations, cards = exactly the cited jobs, posting passages instead of the first 2,500 chars.
- Done: job's own page re-read for deadline/pay/eligibility questions (24 h cache, browser only for JS pages), scam warning before any answer about applying, Confirm/Cancel buttons tied to the waiting change, "typing…" on every message, nemotron-3-ultra for fit/compare only.
- Fixed: 9 HPE jobs had stale scam flags (a "we never charge a fee" disclaimer), re-scored.
- Not done: p95 rose to 17 s (the ultra model on fit questions; median for fit is 15 s). Citation misses on 9 of 30 questions remain (see data/evals/after.json, rows with cite_ok=false).
- Not done: the live routing eval (`python -m jobhunter.evals`, was 66/67) wasn't re-run after the prompt change.
- Skipped by decision: streamed edits, the 10-min answer cache. Hybrid search not built: none of the 30 misses came from keyword search; a synonym probe shows gaps (e.g. "data scientist intern" vs "data science internship": 78 jobs only the second finds). The user decides.
- Next: cap the strong model's timeout or fall back to the fast model at 8 s for fit; fix the remaining citation misses; re-run both evals.
