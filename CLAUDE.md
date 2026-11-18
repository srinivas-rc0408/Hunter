# JobHunter repo

Personal job-hunting system for Srinivas R C. Python 3.10+, CLI `hunt` (jobhunter/cli.py).

- Run tests: `cd tests && python -m unittest discover` (Playwright Chromium needed for test_apply_browser).
- Config is YAML in `config/`; never hard-code personal facts in code.
- Truth rule: form answers come only from `config/profile.yaml` or `data/answers.yaml`. Never add
  guessed defaults to `jobhunter/apply/fields.py`. Unknown must stay `basis="none"`.
- Auto-submit gate lives in `jobhunter/apply/flows.py::gate`. Don't weaken it without being asked.
- `skills/hunter-*` is vendored from github.com/Jakeschincariol/hunter-skill (MIT). `jobhunter/sources/ats.py`
  and `tailor.py` import its scout parsers and cover verifier: keep their function names stable.
- Use the `/jobhunter` skill for day-to-day usage instructions.
- The repo is PUBLIC. Personal facts live only in gitignored `config/private.yaml` (merged in `config.cfg("profile")`).
  `tests/test_review.py::PrivacyGuard` fails if a private value appears in a tracked file. Never weaken it.
