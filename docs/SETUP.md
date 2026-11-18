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
git am -3 ~/Downloads/jobhunter-update-2026-11-18.patch
```
✅ Check: `git log --oneline | head -3` shows "JobHunter update: morning brief, Telegram bot…".

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

### B3. Free LLM keys (2 min, needed for cover letters/resume rewording when Ollama is off)
- Gemini: https://aistudio.google.com/apikey → Create API key → `.env`: `GEMINI_API_KEY=...`
- Groq (backup): https://console.groq.com/keys → `.env`: `GROQ_API_KEY=...`
- Ollama: run `ollama list`. If your Qwen tag isn't `qwen2.5:7b-instruct`, add
  `OLLAMA_MODEL=<exact tag, e.g. qwen2.5:7b>` to `.env`.

The bot tries Ollama → Gemini → Groq → plain templates, and swaps retired model IDs for current
ones by itself (Google retired Gemini 2.0 Flash in June 2026; Groq retired Llama 3.3 70B in August 2026).

✅ Check: `hunt doctor` shows `✓ LLM (...)`.

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

### C3. Your own resume PDF stays at `config/resume.pdf`
It's only uploaded if you set `apply.resume: own` in `config/search.yaml`. Default is the tailored one-pager.

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
Open the Review Center → **To review**. For each job, check every row:
green = your fact, purple = learned from you, amber = drafted (read it), red = missing.
Fix anything wrong in `config/private.yaml` / `config/profile.yaml`, then:
```fish
hunt apply --top 10
```
Basic-only forms go out by themselves. Everything else waits under **To review**, where
**Approve & Submit** sends it. If something is still missing, the browser waits for you to type it,
and the bot learns the answer for next time.

```fish
hunt backup
```
✅ Check: a `.tar.gz.enc` file in `~/jobhunter-backups`.

---

## Daily routine (25–35 min)

**07:30 Telegram morning brief.** Read it on your phone. `/skip_<id>` anything you don't want.

**Morning at the laptop:**
```fish
hunt morning              # same brief + cloud health
hunt apply --top 10       # target: 10 applications a day
```
Then clear **To review** in the Review Center (Approve & Submit, or Skip).

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
| resume tailoring + ATS score + truth check (`hunt tailor`) | no (Ollama/Gemini/Groq or none) |
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
| Cover letters are generic | No LLM reachable: `hunt doctor` → set `GEMINI_API_KEY` or start Ollama |
| Disk full | `rm -rf data/screens/*` (screenshots) and `data/logs/*`. The jobs database is small |
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
hunt morning [--send]            today's brief          hunt apply --top 10          apply (best first)
hunt list --min 55               open jobs              hunt apply <id> --approve    finish a reviewed form
hunt show <id>                   one job + keyword gap  hunt apply --dry-run         preview, never submit
hunt govt / calendar / advantages                       hunt tailor <id>             tailored resume + ATS
hunt mark <id> applied|interview|offer|rejected|skipped hunt cover <id>              verified cover letter
hunt review                      Review Center          hunt sync                    pull cloud results
hunt doctor [--test-alert]       health check           hunt update                  pull + reinstall
hunt backup / restore <file>     encrypted backups      hunt secrets --push          update GitHub secrets
Telegram: /brief /top /govt /review /stats /scan /show_<id> /skip_<id> /queue_<id> /applied_<id> /interview_<id>
```
