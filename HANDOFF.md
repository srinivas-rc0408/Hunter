# Start here (next session)

**Version:** v2.7.2 (docs commit 700d7bf after it). 401 tests: `cd tests && ../.venv/bin/python -m unittest discover` (~4 min).

**What runs where**
- Laptop (systemd user units): `jobhunter-bot` (Telegram, 6 PM nudge), `jobhunter-review` (Review Center),
  timers `scan` (every 2 h), `sync` (30 min), `health` (30 min), `morning` (08:00), `backup` + `weekly` (Sun 21:00).
- Cloud: GitHub Actions `radar` (light every 30 min, full every 3 h, digest 20:15, brief 07:25 IST), encrypted state on `radar-state`.
- Restart after code changes: `systemctl --user restart jobhunter-bot jobhunter-review`.

**Check health**
- `hunt doctor` and `hunt health` (✓/✗ with the fix); `systemctl --user list-timers | grep jobhunter`.
- Radar: `gh run list --workflow radar --limit 5`. GitHub sometimes drops scheduled runs (none for 3 h on 27 Nov):
  `gh workflow run radar`.

**Open items (Telegram AI, batch C)**
- Fit-question p95 not re-measured since the strong model got a 6 s timeout (it was 17 s): `python -m jobhunter.evals answers data/evals/after2.json`.
- Citations correct on 21 of 30 questions: the misses are in `data/evals/after.json` (rows with `cite_ok: false`).
- Live routing eval not re-run after the prompt change (was 66/67): `python -m jobhunter.evals`.
- Hybrid (similarity) search undecided. A synonym probe found gaps aliases don't cover ("AI agents" / "agentic AI").
- Unmerged swarm branches: see memory note (watchdog, semantic-referrals).

**Your to-dos**
- Fill `sc_subgroup` and `marital_status` in `config/private.yaml` (doctor ✗); set up the email digest if you want it.
- Keep a copy of `STATE_KEY` off this laptop (restore needs it).
