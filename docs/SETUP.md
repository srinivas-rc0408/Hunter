# JobHunter: full setup and operating manual

Follow it top to bottom once (about 60–75 min). After that, the whole system runs
**without Claude**: GitHub runs the cloud radar for free, your laptop runs the
apply bot, Review Center, Telegram bot, morning brief and backups. Claude Code only adds
the optional `/hunter-*` skills.

Every command is for **your Arch laptop, in fish**, from `~/jobhunter` unless it says otherwise.
`✅ Check:` lines tell you what you should see. Don't continue past a failed check.

---

## What you'll have at the end

| piece | where it runs | when |
|---|---|---|
| Cloud radar: LinkedIn, Naukri, Indeed, Google, company boards, Birla pages, 20 govt sites | GitHub Actions (free, laptop can be off) | every 30 min, full scan every 3 h |
| Telegram alerts for strong matches + govt notices | your phone | the moment the radar finds them |
| **Morning brief** (best new jobs, approvals waiting, govt deadlines, GATE countdown, cloud health) | laptop → Telegram | 07:30 daily (runs at login if the laptop was off) |
| **Telegram command bot** (`/top`, `/govt`, `/review`, `/skip_…`, `/stats`) | laptop | always on |
| **Review Center** (http://localhost:8765): every field the bot filled + Approve & Submit | laptop | always on |
| Laptop scan (backup to the cloud) + cloud sync | laptop | scan every 2 h, sync every 30 min |
| Encrypted backup of everything not in git | laptop → `~/jobhunter-backups` | Sundays 21:00 |
| Daily email digest (optional) | GitHub → Gmail | 20:15 |

Cost: ₹0. Everything uses free tiers.

---

## Part A: Bring your copy up to date (5 min)

You have the code from yesterday plus Claude Code's two fixes. Save those fixes first,
then apply this update.

```fish
cd ~/jobhunter
git status                          # shows pyproject.toml / scripts/install.sh if not committed
git add -A; and git commit -m "Local setup fixes (pypdf, fish PATH)"
git am -3 ~/Downloads/jobhunter-update-2026-11-18.patch          # update 1: morning brief, bot, backups
git am -3 ~/Downloads/jobhunter-update-2-company-search.patch    # update 2: company search, questions
git add tests/hunter; and git commit -m "Track hunter-skill tests"   # they were hidden by a .gitignore bug
```
✅ Check: `git log --oneline | head -4` shows "Track hunter-skill tests", "Company search…" and "JobHunter update: morning brief…".
`git status` should say "nothing to commit, working tree clean".

If `git am` stops on a conflict (only possible in `pyproject.toml` / `requirements.txt`, where you
and the patch both added `pypdf`): open the file, keep **one** `pypdf>=4` line, then
`git add <file>; and git am --continue`. Then:

```fish
.venv/bin/pip install -q -e .
hunt --version
```
✅ Check: `jobhunter 1.1.0`, and `hunt morning --help` works.

---

## Part B: Accounts and keys (20 min)

### B1. Telegram (finish it)
You already created **@Shotgun650** and sent `/start`. In the **terminal** (not in Telegram):
```fish
hunt telegram-id
```
It prints `TELEGRAM_CHAT_ID=123456789`. Open `.env` (`nano .env`) and set both lines:
```
TELEGRAM_BOT_TOKEN=<token from BotFather, no quotes, no spaces>
TELEGRAM_CHAT_ID=<the number>
```
```fish
hunt doctor --test-alert
```
✅ Check: "✅ JobHunter is connected" arrives in the Shotgun650 chat.
If you ever pasted the token anywhere except `.env`: in @BotFather send `/revoke`, pick the bot,
put the new token in `.env`.

### B2. GitHub CLI
```fish
sudo pacman -S --needed github-cli
gh auth login            # GitHub.com → HTTPS → Login with a web browser
gh auth status
```
✅ Check: "Logged in to github.com account srinivas-rc0408".

### B3. LLM: NVIDIA keys + local model (5 min, for cover letters and resume rewording)
Nothing needs an LLM (templates are the fallback), but letters and rewording are better with one.
The bot tries **NVIDIA build → local Ollama → Gemini → Groq → templates**, per request. A bad key,
a rate limit (HTTP 429), a retired model or a timeout just moves on to the next key, model or provider.

**NVIDIA build** (big models in the cloud):
1. https://build.nvidia.com → sign in → your profile → **API Keys** → **Generate API Key** (starts with `nvapi-`).
2. Put them in `.env` on ONE line, comma-separated, no spaces needed:
   ```bash
   nano ~/jobhunter/.env      # add or edit:  NVIDIA_API_KEYS=nvapi-AAA,nvapi-BBB,nvapi-CCC
   ```
   Keys live only in `.env` (gitignored). Never paste them in chat, a commit or a screenshot.
3. Free access is for development and **rate-limited (about 40 requests/minute per key)**. NVIDIA can
   change these terms at any time, so the local model below is your no-expiry fallback.

**Local model** (no key, no quota, works offline):
```bash
cd ~/jobhunter && ./scripts/install-local-llm.sh
```
It installs `ollama-cuda`, starts the service at boot, picks the model that fits your GPU
(`qwen3:4b` for a 4 GB card, ~2.5 GB) and writes `OLLAMA_MODEL` to `.env`.
`ollama ps` should show **100% GPU** while it answers. A 7B/8B model doesn't fit in 4 GB and runs ~5x slower.

**Optional extra backups:** Gemini (https://aistudio.google.com/apikey → `GEMINI_API_KEY=`),
Groq (https://console.groq.com/keys → `GROQ_API_KEY=`).

Model IDs get retired (Gemini 2.0 Flash in June 2026, Groq Llama 3.3 70B in August 2026), so each
provider's live model list is checked and the best current model is used automatically.
Pin one with `NVIDIA_MODEL=` / `OLLAMA_MODEL=` / `GEMINI_MODEL=` / `GROQ_MODEL=`.

Privacy: cloud LLMs get your resume bullets and the job description. Your phone and email are
replaced by `[PHONE]`/`[EMAIL]` before sending and filled back in afterwards.

✅ Check: `hunt llm --test` shows ✓ for every key and a "chain answer". `hunt doctor` shows `✓ LLM`.

### B4. Gmail digest (optional, 3 min)
Google Account → Security → 2-Step Verification (must be on) → App passwords → name it
"JobHunter" → copy the 16 letters → `.env`:
```
SMTP_USER=<your Gmail address>
SMTP_PASS=<16 letters, no spaces>
DIGEST_TO=<your Gmail address>
```

---

## Part C: Your data (15 min)

### C1. `config/private.yaml` (never leaves your laptop except as an encrypted GitHub secret)
```fish
nano config/private.yaml
```
- `marital_status: Single` (if true; some govt forms ask)
- `sc_subgroup: A` / `B` / `C`. Karnataka splits SC posts 6/6/5% into groups since Dec 2025. Find your
  caste in the Social Welfare Department's group list (or ask the Tahsildar office). This decides
  which vacancy column you compete in on every state post. Don't guess.
- Check: phone, both pincodes, parents' names, marks 88.8 / 82.5, DOB.

### C2. Resume facts
Your internship bullets have no results. Rewrite the 3 `experience` bullets in `config/resume.yaml`
with things you can prove (what you built, PRs merged, what it changed). Real numbers only.
Add any extra facts under `facts:`. Then:
```fish
hunt resume
xdg-open data/resumes/Srinivas_R_C_Resume.pdf
```
✅ Check: one page, every line true.

### C3. The resume every application uploads
Your own PDF, exactly as you made it (default). Set it once, in either way:
```fish
hunt resume --use ~/Downloads/Srinivas_RC_Resume.pdf
```
or **send the PDF to your Telegram bot**. It checks the file (real PDF, 1–3 pages, under 5 MB,
has readable text) and keeps the previous one in `data/resume-history/`.
Companies receive it as `Srinivas_R_C_Resume.pdf`. `hunt resume --show` says which file is used;
`/resume` in Telegram sends it back to you.
(Prefer a one-page version re-ordered per job? Set `apply.resume: tailored` in `config/search.yaml`.)

---

## Part D: Cloud radar on GitHub (10 min)

```fish
git push -u origin main
git ls-files | grep -E "config/private.yaml|config/resume.pdf|config/advantages.yaml|docs/private/"
```
✅ Check: the `grep` prints **nothing** (your private files are not on GitHub).

```fish
hunt secrets --push      # creates STATE_KEY in .env, uploads all secrets to the repo
gh secret list
```
✅ Check: STATE_KEY, PRIVATE_YAML_B64, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (+ SMTP_* if set).

**Save STATE_KEY off the laptop now.** `grep STATE_KEY .env`, then copy it into your phone's
notes or a password manager. Without it, no backup and no cloud state can ever be decrypted.

```fish
gh workflow enable radar
gh workflow run radar -f mode=full
gh run watch
```
✅ Check: green tick. Telegram alerts start arriving. `hunt sync` prints "synced from cloud radar".

> Re-run `hunt secrets --push` whenever you change `config/private.yaml` or `.env` keys,
> or the cloud keeps using the old values.

---

## Part E: Laptop services (5 min)

Run this **from a terminal inside your niri session** (it hands your display to the services so
the Approve button can open a browser window):
```fish
./scripts/install-services.sh
```
✅ Check: the timer list shows `jobhunter-sync`, `-scan`, `-morning`, `-backup`; `hunt doctor` shows
every `jobhunter-*` unit `active`. Open **http://localhost:8765**, bookmark it, and pin the tab.

Make niri hand its display to user services at every login. Add this line to `~/.config/niri/config.kdl`:
```kdl
spawn-at-startup "systemctl" "--user" "import-environment" "DISPLAY" "WAYLAND_DISPLAY" "XDG_RUNTIME_DIR"
```
(If the Approve button ever says "can't see your screen", run `hunt apply <id> --approve` in a terminal.)

Telegram bot: send `/stats` to Shotgun650.
✅ Check: it replies with numbers. Type `/` to see the command menu.

---

## Part F: First live run (15 min)

```fish
hunt login linkedin      # log in in the window, press Enter in the terminal
hunt login naukri
hunt scan
hunt list --min 55
hunt apply --dry-run --top 3
```
Open the Review Center → **To review**. Each job lists every field the bot filled and where the value
came from: **your details** (green), **learned from you** (purple), **AI draft: read it** (amber),
**EMPTY** (red). Change anything with ✎: it's saved as your answer for every future form.
Then apply for real:
```fish
hunt apply --top 5
```
**How a real application works (check mode, the default):**
1. The bot opens the job in its browser and fills the real form.
2. A **JobHunter panel** appears on the page. It lists every field with the value and where it
   came from, and the same colours outline the fields on the page. Click a row to jump to that field.
3. You read the form. Fix anything right on the page: the row turns blue, "you changed this".
4. Press **Submit application** on the panel (or the site's own Submit). On LinkedIn every step
   shows the panel: press **Looks right · Next step** until the last one.
5. The bot reads the form one last time. Every value you changed or typed is saved as your answer,
   so the same mistake never happens again. Telegram gets "✅ Applied".
   **Skip job** hides it; **Later** keeps it under To review.

Never saved as answers: files, cover letters, long essays (they're about one job), captcha/OTP,
caste/diversity questions.

Away from the laptop? `hunt apply --auto` runs without the panel: it sends only forms where every
field is a fact you gave, and Telegram sends you the rest with the screenshot (`/form_<id>`,
`/fix_<id> 3 new value`, `/approve_<id>`).

```fish
hunt backup
```
✅ Check: a `.tar.gz.enc` file in `~/jobhunter-backups`.

---

## The dashboard: http://localhost:8765

Bookmark it. It opens on **Dashboard**:
- your numbers: applied in total, this week, today, interviews, offers, waiting for you;
- **Apply**: type a company and press Enter (its open jobs, pick and apply), **▶ Apply to my best
  matches** (the bot fills each one, you check and press Submit), **🎯 Open bot browser**, **⟳ Scan**;
- **Your applications**: every job you applied to, with the date, how it was sent, its status (change it
  there: interview / offer / rejected), how many fields you fixed, the job link, the resume that was
  sent, the filled form's screenshot, and **what was sent** (every field and value).
Other tabs: To review, Questions, Companies, Applied, Jobs, Govt, Everything.

## The bot browser (apply anywhere)
```fish
hunt browser                       # or: hunt browser https://careers.adityabirla.com/job-search
```
A browser opens with your LinkedIn/Naukri logins. Go to any job or application form and press the green
**🎯 Fill this form** button (bottom-left). The bot fills what it knows, shows the check panel, you fix and
press Submit, and it's recorded in the dashboard with your fixes learned. Close the window when done.
Only one bot window can run at a time (`hunt apply`, `hunt browser`, `hunt login` share the logins).

## Company search (any time)

**Terminal:**
```fish
hunt company birla          # type part of a name
```
1. Pick from the suggestions (your watchlist, the 129-company directory, companies already in
   your tracker, Wikidata, or "exactly as typed").
2. It finds the company's careers page and hiring platform by itself: Greenhouse, Lever, Ashby,
   Workday, SmartRecruiters, Workable, Recruitee, Keka, Darwinbox, Zoho Recruit, SuccessFactors,
   Aditya Birla's site, or schema.org job data. JavaScript-only pages are rendered in Chromium.
3. You get every listing: posted date (🔥 = today), role, location (KA = Karnataka), type, where it
   was found (company site / LinkedIn / Naukri / Indeed / your tracker), fit score, your status.
4. Then type `apply 1,3` · `preview 2` · `track 1-4` · `show 3` · `open 3` · `watch` · `links` · `all` · `q`.

**Review Center → Companies tab:** the same thing with a search box that suggests as you type.
Tick listings → **Apply to ticked**, **Preview ticked** or **Add ticked to queue**. **★ Watch company** adds it to every scan.
**Referrals & research** opens LinkedIn searches for REVA alumni at that company (your best referral
source), recruiters, AmbitionBox reviews and fresher salaries, and Glassdoor interview questions.

**Telegram:** `/company sarvam` (top listings with links), `/watch sarvam`.

Your dream companies are read straight from their own sites, not just job boards:
Aditya Birla Group (careers.adityabirla.com → PeopleStrong apply links, with each job's closing date)
and Birlasoft (SuccessFactors).

## Questions (the bot never guesses)

When a form asks something that isn't in your files, the bot doesn't guess: it saves a **question**.
Answer it once, with a true fact, in any of three places:
- `hunt questions` (terminal, walks you through each one)
- Review Center → **Questions** tab
- Telegram: `/questions`, then `/answer 2 Yes`

Each answer is saved to `data/answers.yaml` and used on every future form. Jobs that were waiting only
on that answer go back into the apply queue automatically. The morning brief tells you how many
questions are open. Never asked: captchas, OTPs, essays, and voluntary diversity questions.

## Daily routine (25–35 min)

**07:30 Telegram morning brief.** Read it on your phone. `/skip_<id>` anything you don't want.

**Morning at the laptop:**
```fish
hunt morning              # same brief + cloud health
hunt apply --top 10       # target: 10 applications a day
```
Each form opens with the JobHunter panel: read it, fix anything on the page, press Submit.
Then clear **Questions** (one answer can unblock several jobs) and **To review** (Open & check, or Skip)
in the Review Center.

**Govt:** `hunt govt`, open each notice marked `eligible`/`check` and read the official PDF's
eligibility clause, especially branch wording (AI&ML is not always listed). Apply on the portal
yourself, then `hunt mark <id> applied`.

**When something happens:** `hunt mark <id> interview --note "HR call Fri 3pm"` (or `/interview_<id>` in Telegram).

## Weekly (Sunday, 20 min)
- `hunt doctor`: everything ✓
- `hunt update`: pull fixes, refresh Chromium. Then `systemctl --user restart jobhunter-review jobhunter-bot`
- `hunt answers`: check what the bot learned from you; edit `data/answers.yaml` if anything is wrong
- `hunt calendar` and `hunt advantages`: deadlines and SC/state benefits still open
- Backup runs at 21:00 by itself. Once a month, copy the newest file in `~/jobhunter-backups` to Google Drive or a USB stick.

---

## When the Claude subscription ends

**Everything keeps working.** Nothing in JobHunter calls Claude.

| feature | needs Claude? |
|---|---|
| cloud radar, alerts, digest, morning brief, Telegram bot | no |
| scanning, scoring, govt eligibility, dedupe | no |
| apply bot, Review Center, learning answers | no |
| resume tailoring + ATS score + truth check (`hunt tailor`) | no (NVIDIA/Ollama/Gemini/Groq or none) |
| cover letters (`hunt cover`) | no (LLM optional, template fallback) |
| hunter-* skills as slash commands | Claude Code only |

Without Claude Code:
- The skills' scripts still run directly: `python skills/hunter-resume/ats.py score hunter/resumes/base.md --jd hunter/resumes/<id>.jd.md`,
  `python skills/hunter-tracker/pipeline.py validate --file hunter/pipeline.json`,
  `python skills/hunter-cover/cover.py --help`.
- The skills' instructions (`skills/<name>/SKILL.md`) also work in any free chat AI: paste the
  file, then your request.
- Do nothing else. Don't delete `~/.claude/skills`; it's harmless.

---

## Troubleshooting

| symptom | fix |
|---|---|
| Morning brief says **cloud radar STALE** | `gh run list -w radar` → open the failed run. Usual causes: secrets changed (`hunt secrets --push`), or workflow disabled (`gh workflow enable radar`) |
| Run fails at "Check secrets" | `hunt secrets --push` |
| Run fails at "Restore state" (bad decrypt) | STATE_KEY in GitHub ≠ .env. `hunt secrets --push` (same key from `.env`) |
| No Telegram alerts | `hunt doctor --test-alert`. Token revoked? Chat id wrong? Did you `/start` the bot? |
| Bot doesn't answer commands | `systemctl --user status jobhunter-bot`; `journalctl --user -u jobhunter-bot -n 50` |
| LinkedIn shows a login wall / "not logged in" | `hunt login linkedin` again |
| LinkedIn warning or CAPTCHA on your account | Stop LinkedIn applies for a week: `max_per_site_per_day: {linkedin: 0}` in `config/search.yaml` |
| Many jobs end in "needs you" for the same question | Answer it once in the browser (Approve flow). It's learned. Or add it to `config/profile.yaml → answers` |
| Review Center not loading | `systemctl --user restart jobhunter-review`, or run `hunt review` in a terminal |
| Approve says "can't see your screen" | Run `./scripts/install-services.sh` from your desktop terminal, or `hunt apply <id> --approve` |
| `hunt: command not found` in fish | `fish_add_path -P ~/jobhunter/.venv/bin` and open a new terminal |
| Chromium won't start after an Arch update | `hunt update` (reinstalls Playwright's Chromium) |
| Cover letters are generic | No LLM reachable: `hunt llm --test` → fix the ✗ key, or `./scripts/install-local-llm.sh` |
| Bot browser: "already open (process …)" | Another `hunt apply`/`hunt login` window is open. Close it (or wait), then retry |
| "the browser tab closed before the page loaded" | Fixed in 1.5: the bot retries with a fresh tab. If it repeats, close every bot browser window and run again |
| A form keeps getting one field wrong | Change it on the page before Submit, or ✎ in the Review Center, or `/fix_<id> n value`: your answer wins from then on |
| Telegram bot doesn't answer | `systemctl --user restart jobhunter-bot`, send `ping`, then `journalctl --user -u jobhunter-bot -n 30` shows every message and why a reply failed |
| `hunt llm --test` says 429 | That key hit NVIDIA's rate limit; it rests 60 s and the next key/provider is used. Wait a minute |
| `ollama ps` shows CPU, not GPU | Model too big for 4 GB, or `ollama` (CPU build) installed: `sudo pacman -S ollama-cuda`, use `qwen3:4b` |
| Disk full | `rm -rf data/screens/*` (screenshots) and `data/logs/*`. The jobs database is small |
| Company search finds no platform | Run `hunt company "<name>" --refresh`. If it still says none, the company only posts on job boards, and those results are still shown. Add its careers URL to `config/company_directory.yaml` (`careers:`) and search again |
| Aditya Birla shows 0 listings | Chromium is needed for that site: `python -m playwright install chromium` (laptop). The cloud installs it for full scans |
| A question has the wrong answer saved | Edit or delete that line in `data/answers.yaml` |
| New laptop / disk died | see "Disaster recovery" |

## Disaster recovery (new laptop)
```fish
git clone https://github.com/srinivas-rc0408/Hunter.git ~/jobhunter
cd ~/jobhunter; and ./scripts/install.sh
# copy your newest backup file to this laptop, then:
env STATE_KEY='<from your phone notes>' hunt restore ~/jobhunter-backup-file.tar.gz.enc
./scripts/install-services.sh
hunt login linkedin; hunt login naukri
hunt doctor
```

## Command cheat sheet
```
hunt morning [--send]            today's brief          hunt apply --top 5           apply: you check each form, press Submit
hunt company <name>              search a company       hunt apply <id>              one job
hunt browser [url]               bot browser + 🎯 button
hunt dream add "Zoho"            new dream company      hunt apply --dry-run         preview to the Review Center, never sends
hunt dream remove "Zoho" / list                         hunt apply --auto            no panel: basic-only forms, queue the rest
hunt list --min 55               open jobs              hunt questions               answer what forms asked
hunt show <id>                   one job + keyword gap  hunt resume --use FILE       the PDF every application uploads
hunt govt / calendar / advantages                       hunt rescore                 re-score jobs with current rules
hunt mark <id> applied|interview|offer|rejected|skipped hunt cover <id>              verified cover letter
hunt review                      Review Center          hunt sync                    pull cloud results
hunt doctor [--test-alert]       health check           hunt llm --test              check the AI keys/models
hunt backup / restore <file>     encrypted backups      hunt secrets --push          update GitHub secrets
Telegram (words work without the slash):
  /brief /top /review /govt /stats /scan /ping /help
  /form_<id>  every field filled     /fix_<id> 3 new value  correct field 3 (remembered)
  /approve_<id>  send as shown       /check_<id>  open on the laptop with the panel
  /show_<id> /skip_<id> /queue_<id> /applied_<id> /interview_<id> /rejected_<id> /offer_<id>
  /dream Zoho  /undream Zoho  /dreams      /company <name>  /watch <name>
  /questions  /answer 1 Yes              send a PDF = new resume · /resume = get it back
```
