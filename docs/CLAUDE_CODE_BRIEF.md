# Brief for Claude Code: make JobHunter the most accurate auto-apply bot

You are working in `~/jobhunter` on Srinivas's laptop (Arch Linux, niri, fish shell, RTX 3050 Ti 4 GB).
Everything JobHunter is in this one folder. This brief says what exists, the rules, and what to do next,
in order. Read `CLAUDE.md` and `docs/SETUP.md` first.

**Your big advantage over earlier work:** earlier versions were built in a cloud sandbox that could not
open LinkedIn, Naukri or company sites. You can. Every job below is about checking the bot against the
**real live sites** in the logged-in bot browser and fixing what's actually there. Don't guess from memory
what a site looks like: open it, read it, then change code.

---

## 1. What exists (v1.6.1)

| Part | Where | State |
| --- | --- | --- |
| Cloud radar (GitHub Actions, every 30 min) | `.github/workflows/radar.yml` | runs; first full scan timed out at 25 min → limit now 55 min. Verify green runs |
| Scan → dedupe → score → alert | `jobhunter/pipeline.py`, `score.py`, `sources/` | works (2000+ jobs tracked) |
| Apply engine (check mode) | `jobhunter/apply/` (`flows.py`, `browser.py`, `fields.py`, `panel.py`, `runner.py`) | tested on local copies of forms only; **live LinkedIn not yet verified** |
| Check panel + learning your fixes | `apply/panel.py`, `fields.learn(override=True)` | tested in real Chromium on fixtures |
| Bot browser + 🎯 Fill this form | `apply/assist.py`, `hunt browser` | tested on fixtures |
| Dashboard / Review Center | `jobhunter/review.py`, http://localhost:8765 | tested (all tabs, no script errors) |
| Telegram bot | `jobhunter/bot.py` (systemd `jobhunter-bot`) | /help fixed; new commands untested live |
| LLM chain | `jobhunter/llm.py` | NVIDIA (3 keys) → Ollama qwen3:4b-instruct → Gemini → Groq → templates |
| Tests | `tests/` | 102 + 157 hunter-skill, all pass |

Live problems seen on 18 Nov (before 1.6.1, which targets them):
- LinkedIn "no apply button" on Barclays and Qualcomm; "took too long" on Birlasoft, The Wedding
  Company, NURY. 1.6.1 finds Apply by accessible name (button or link) and follows company-site tabs.
  When it still fails it prints `buttons on the page: …` and saves the page to `data/screens/<id>-page.html`.

## 2. Rules you must never break

1. **Truth:** form answers come only from `config/private.yaml`, `config/profile.yaml`, `data/answers.yaml`
   or what Srinivas types. Unknown stays `basis="none"`: it becomes a question, never a guess.
   Don't add guessed defaults to `apply/fields.py`.
2. **Nothing is sent without him:** default mode is `check` (panel → he presses Submit). Don't weaken
   `flows.gate()` or `flows.checking()` unless he asks. `--auto` sends only all-basic forms.
3. **Category stays private:** caste/SC is used only for govt eligibility, never typed into private forms,
   never learned (`panel.NEVER_LEARN`).
4. **Public repo:** personal data lives only in gitignored files (`config/private.yaml`, `config/resume.pdf`,
   `config/advantages.yaml`, `docs/private/`, `data/`, `.env`). `tests/test_review.py::PrivacyGuard` must pass.
   Never paste keys, phone, email or DOB into code, tests, commits or fixtures (fixtures use "Asha K R").
   Real page HTML saved for fixtures must be scrubbed of his name/email/phone first.
5. **No social automation:** no auto-likes, comments, DMs, connection requests, or account creation.
   LinkedIn is his job lifeline; keep the human pace and daily caps in `config/search.yaml`.
6. **Tests stay green:** `cd tests && python -m unittest discover` and `python -m unittest discover -s tests/hunter`.
   Every fix gets a fixture/test that fails before the fix.
7. Commits: author `Srinivas R C <srinivas-rc0408@users.noreply.github.com>`; push to `main` when tests pass.

## 3. Do this, in order

### Task 1: Prove LinkedIn Easy Apply on the live site (highest priority)
1. `hunt --version` must be 1.6.1. `hunt doctor` must be all ✓ except optional email.
2. `hunt apply --top 3 --dry-run`, then look at each result in the dashboard (To review).
3. For every failure: open the job in `hunt browser`, read the live DOM (Easy Apply modal: it may be in a
   shadow root; `browser.DEEP_JS` / `B.scan` handle open shadow roots), find why the flow broke, fix
   `flows.linkedin` / `_linkedin_check` / `browser.scan`.
4. Save a scrubbed copy of the real modal HTML as a fixture in `tests/fixtures/forms/` and add a test.
5. Then a real run with him at the laptop: `hunt apply --top 5`. Success = each Easy Apply job reaches the
   panel with every field filled or marked EMPTY, each step's Next works, Submit sends, ✅ in Telegram.

Acceptance: 5 real LinkedIn applications sent through the panel with no manual clicking except Submit/Next.

### Task 2: AI backup navigator (only after Task 1 shows where scripted steps still fail)
When the scripted flow can't find Apply / Next / Continue / Review / Submit:
- snapshot the visible interactive elements (role, accessible name, text, enabled, inside a dialog?);
- ask `llm.chat` which ONE element moves the application forward, as JSON `{"id": n, "why": "..."}`;
- click it, re-snapshot, verify progress (new step, new fields, success text); max 6 AI clicks per job;
- **navigation clicks only**: never type, never pick options, never press a final Submit (the panel does
  that), never click anything named delete/withdraw/pay/buy/sign out;
- log each AI click to the application record so the dashboard shows "AI clicked: Continue";
- tests with a fake LLM server (see `tests/test_llm.py` `Fake`).

### Task 3: Naukri live
`hunt apply <naukri job id>` in check mode: one-click Apply, the recruiter-question chatbot, the "apply on
company site" redirect. Fix and add fixtures the same way.

### Task 4: Company career sites live
Birlasoft (`jobs.birlasoft.com`, SuccessFactors), Aditya Birla Group (`careers.adityabirla.com` → PeopleStrong),
one Greenhouse, one Lever, one Workday job. Multi-page forms must re-fill each page after "moved"
(`flows.check_form`). Workday needs an account: stop and ask him, never create one.

### Task 5: Telegram, checked live
With the bot running (`systemctl --user restart jobhunter-bot`), send each and confirm the reply:
`ping`, `help`, `top 5`, `review`, `/form_<id>`, `/fix_<id> 1 <value>` (then confirm `data/answers.yaml`),
`/check_<id>` (opens on the laptop), `/approve_<id>`, `dream Zoho`, `undream Zoho`, `dreams`, `resume`,
sending a PDF, `/company Birlasoft`, `questions`. Watch `journalctl --user -u jobhunter-bot -f`.
Fix any reply that fails or reads badly; every reply must stay valid Telegram HTML (`tests/test_ops.py`).

### Task 6: Cloud radar
`gh run list --workflow radar -L 5` must show green light and full runs. If a full run nears 55 min,
time each source (`hunt scan --only …`) and cap the slow one; don't just raise the limit.

### Task 7: Accuracy pass on matching
Read 30 jobs from `hunt list --min 55`. Anything he wouldn't apply to (wrong level, wrong place, gig,
stale) is a scoring bug: fix `score.py` with a test. Anything good that scored low: same.

## 4. How he likes to work
Blunt, short, plain words. Say what's wrong first. One recommendation, not a menu. Finish everything
you start. End with one action he can do today.

## 5. Prompt to paste into Claude Code
> Read docs/CLAUDE_CODE_BRIEF.md, CLAUDE.md and docs/SETUP.md. Then do Task 1 on the live sites using the
> bot browser. Follow every rule in section 2. Show me each failure with the cause before you fix it,
> run the tests after each fix, and commit when green.
