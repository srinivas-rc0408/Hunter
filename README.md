# JobHunter

A personal job-hunting system for an Indian final-year AI/ML engineer, built by
Srinivas R C. It finds every relevant job within the hour it's posted, ranks it,
tailors a one-page resume with an ATS truth check, fills the application in your own
logged-in browser, and keeps a reviewable record of every field it submitted.
It covers private tech jobs (LinkedIn, Naukri, Indeed, Google Jobs, and companies' own platforms:
Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, Recruitee, Keka, Darwinbox,
Zoho Recruit, SuccessFactors, plus schema.org job data on any careers page) and Indian government jobs
(ISRO, NIC, DRDO, BEL, IISc, C-DAC, banks, KPSC, KEA…), with reservation-aware
eligibility checks.

**It doesn't need Claude.** The cloud radar runs free on GitHub Actions and the
apply bot runs on your laptop. Claude Code is optional (`/jobhunter` plus the 11
[hunter-skill](https://github.com/Jakeschincariol/hunter-skill) skills).

```
 GitHub Actions: every 30 min, laptop off           Your laptop
 ┌──────────────────────────────────────┐ encrypted ┌──────────────────────────────────────┐
 │ LinkedIn · Naukri · Indeed · Google  │   sync    │ hunt apply                           │
 │ Greenhouse · Lever · Ashby · Workday │ ────────▶ │  ├ tailor resume (hunter-resume ATS) │
 │ watched career pages (diffed)        │           │  ├ LinkedIn Easy Apply / Naukri       │
 │ 20 govt notice boards + PDFs         │           │  ├ Greenhouse / Lever / Ashby / any   │
 │  → dedupe → score → eligibility      │           │  └ submit ONLY if every field is a    │
 │  → Telegram alert on your phone      │           │    fact you gave, else queue it       │
 └──────────────────────────────────────┘           │ Review Center → http://localhost:8765 │
                                                    └──────────────────────────────────────┘
```

## Rules it enforces

| Rule | How |
| --- | --- |
| **Location** | Karnataka only (+ remote-India). Everything else is dropped before scoring. |
| **Fresher fit** | Senior/lead/manager titles and posts asking 2+ years are dropped (dream companies kept with a penalty). |
| **Pay floors** | Full-time below **₹6 LPA** dropped (target 8+ ranks higher). Internships below ₹15k/month dropped (₹30k+ ranks higher). Unlisted pay kept. Govt exempt. |
| **Dream companies** | Aditya Birla Group (all businesses) and Birlasoft: +15 score, lower alert bar, front of the apply queue, career pages watched directly. |
| **You check, then submit** | The bot fills the real form, then a JobHunter panel on the page lists every field, its value and its source. You fix anything on the page and press Submit. Nothing is sent before that. (`hunt apply --auto`: no panel, auto-sends only forms where every field is a fact you gave.) |
| **Learns your fixes** | Every value you change on the form (or with ✎ in the Review Center, or `/fix` in Telegram) is saved as your answer and wins over the bot's own choice on every future form. |
| **Everything recorded** | Every attempt is saved field by field, with its source, a screenshot, the resume and the cover letter, in the Review Center and in Telegram (`/form_<id>`). |
| **Asks, never guesses** | An unknown required field becomes a *question* (terminal, Review Center or Telegram). Your answer is saved and reused, and jobs waiting on it re-queue. |
| **It learns** | Answers you type in a paused form go to `data/answers.yaml` and count as facts next time. |
| **No invented facts** | Tailored resumes pass hunter-resume's truth check (`ats.py compare`: no new numbers or skills), and cover letters pass hunter-cover's verifier. Anything that fails is discarded. |
| **Category stays private** | Your reservation category is used only for govt eligibility. It's never typed into a private company's form. |
| **Govt = alert, you apply** | OTR/OTP/fee portals stay manual. You get the link, the eligibility verdict and the last date. |
| **Human pace** | 35–90 s between applications. Caps of 20 LinkedIn and 25 total submits a day. Typing delays. |

> **Risk:** LinkedIn's and Naukri's terms forbid bots. Scanning is logged-out and
> never touches your account. Applying uses your account at a capped, human pace,
> but a restriction is still possible. Lower `max_per_site_per_day` to be safer.

## Privacy (the repo is public)

| file | in git? | holds |
| --- | --- | --- |
| `config/private.yaml` | **no** | name, phone, email, DOB, parents, gender, category, addresses, marks |
| `config/resume.pdf`, `config/advantages.yaml`, `docs/private/` | **no** | your resume, govt advantages, govt strategy |
| `data/` (jobs.db, answers, screenshots, browser profile) | **no** | everything the bot did |
| `config/profile.yaml`, `resume.yaml`, `search.yaml`, … | yes | targets, skills, public resume bullets |

The cloud radar receives `private.yaml` through the `PRIVATE_YAML_B64` secret,
masks every personal value in the logs, and saves its database **AES-256
encrypted** (`STATE_KEY`) on the `radar-state` branch. `tests/test_review.py`
fails if any value from `private.yaml` appears in a tracked file.

## Setup

**Follow [`docs/SETUP.md`](docs/SETUP.md)**: the step-by-step manual (setup, daily and weekly
routine, running without Claude, troubleshooting, disaster recovery). Short version:

```bash
git clone git@github.com:srinivas-rc0408/Hunter.git ~/jobhunter
cp config/private.example.yaml config/private.yaml   # fill it in (you already have yours)
~/jobhunter/scripts/install.sh   # venv, Chromium, `hunt` on PATH (fish too), global skills, systemd units
```

1. **Telegram:** @BotFather → `/newbot` → token into `.env` → message your bot →
   `hunt telegram-id` → id into `.env` → `hunt doctor --test-alert`.
2. **Cloud radar:** `sudo pacman -S github-cli && gh auth login`, then
   `hunt secrets --push`. That generates `STATE_KEY` and sets all the secrets. Then
   `gh workflow run radar -f mode=full`.
3. **Log in once:** `hunt login linkedin`, `hunt login naukri`.
4. **Services:** `./scripts/install-services.sh` sets up the Review Center (http://localhost:8765),
   Telegram bot, morning brief 07:30, cloud sync, laptop scans and weekly encrypted backups.

## Daily use

```bash
hunt list --min 55        # new matches (★ dream, 🏛 govt)
hunt apply --top 10       # tailor + fill + submit basic forms; queue the rest
# open http://localhost:8765 → "To review" → check every field → Approve & Submit
hunt govt; hunt calendar  # govt notices with eligibility + deadlines
hunt advantages           # your reservation / state / GATE advantages checklist
```

`hunt apply --dry-run` fills and records without submitting. You see exactly what
it *would* send in the Review Center.

## Daily use without Claude

Everything runs by itself: cloud radar on GitHub, laptop scans every 2 h, the 07:30 brief, a health
check every 30 min (an alert arrives on Telegram the first time something breaks, and once when it's
fixed), the Sunday 21:00 weekly report and backup. From your phone: `today`, `top 5`, `search python`,
`company Zoho`, `govt karnataka`, `eligible`, `deadlines`, `stats`, `health`, `help`.

```bash
hunt health                     # every check, ✓/✗ with the exact fix command (same as /health)
hunt apply --top 10             # apply (caps: LinkedIn 20, all sites 25 a day; never the same role twice)

# update to the latest version
git pull && systemctl --user restart jobhunter-bot jobhunter-review

# something broke after an update: go back to the last known-good version
git checkout v1.7-stable && systemctl --user restart jobhunter-bot jobhunter-review
git checkout main               # later, to return to the latest
```

Restore everything (laptop lost): clone the repo, `hunt restore ~/jobhunter-backups/<newest>.tar.gz.enc`
with `STATE_KEY` from your phone notes, then `scripts/install-services.sh`.

## Resume tailoring (hunter-resume inside)

`hunt tailor <id>` (also runs automatically before each apply):
1. writes `hunter/resumes/base.md` from your master resume and `<id>.jd.md` from the posting
2. scores both with hunter-resume's published formula (`50R + 20P + 20B + 10S`)
3. picks the 4 projects that score highest for this JD and puts the JD's skills first
4. with an LLM (NVIDIA build → local Ollama → Gemini → Groq, first that answers), rewords bullets into the JD's own words.
   `ats.py compare` must say **CLEAN** (no invented number or skill), or the rewrite is dropped
5. renders a one-page ATS-safe PDF, and lists what the JD asks that your resume doesn't
   show (add it to `resume.yaml → facts` **only if true**)

## Commands

| command | what it does |
| --- | --- |
| `hunt scan [--light] [--only boards,ats,pages,govt]` | scan, dedupe, score, alert |
| `hunt list / show <id>` | browse a job: full JD, score reasons, keyword gap |
| `hunt apply [ids] [--top N] [--dry-run] [--approve]` | apply in your browser |
| `hunt review` | Review Center (http://localhost:8765) |
| `hunt tailor <id>` / `hunt resume` | tailored resume + ATS report / one-page master PDF |
| `hunt cover <id>` | verified cover letter |
| `hunt govt` / `hunt calendar` / `hunt advantages` | govt radar / dates / your advantages |
| `hunt mark <id> <status>` | applied, interview, offer, rejected, skipped… |
| `hunt answers` / `hunt classify "<label>"` | learned answers / how a label gets answered |
| `hunt sync` / `hunt secrets [--push]` | pull encrypted cloud results / set GitHub secrets |
| `hunt company <name>` | type a company → pick → every listing (date, place, type, source, link) → apply / track / watch / referral links |
| `hunt questions` | answer what forms asked that isn't in your files (asked once, never guessed) |
| `hunt morning [--send]` | morning brief: cloud health, best new jobs, approvals waiting, govt deadlines, GATE countdown |
| `hunt bot` | Telegram command bot: `/top /govt /review /stats /scan /show_<id> /skip_<id> /applied_<id>` |
| `hunt backup` / `hunt restore <file>` / `hunt update` | encrypted backups, restore, self-update |
| `hunt browser [url]` | the bot's browser: on any form press 🎯 Fill this form, check, Submit (recorded + learned) |
| `hunt dream add/remove/list "Name"` | dream companies (+15, watched, pushed to the cloud radar); Telegram `/dream Name` |
| `hunt resume --use FILE` | the PDF every application uploads (or send it to the Telegram bot) |
| `hunt llm [--test] [--models]` | LLM chain status; `--test` checks every key and model |
| `hunt digest` / `hunt dashboard` / `hunt export-hunter` / `hunt doctor` | digest, static HTML, hunter-skill data, health check |

## Tests

```bash
cd tests && python -m unittest discover -v     # scoring, eligibility, fields, dedupe, govt radar,
                                               # real Chromium on Greenhouse/LinkedIn-style forms,
                                               # Review Center + approve flow, tailoring, privacy guard
cd .. && python -m unittest discover -s tests/hunter   # hunter-skill's own suite
```

## Known limits

- **Site layouts change.** The filler reads labels the way a person does, and when a
  flow breaks it queues the form for review instead of guessing. The selectors were
  never run against a real LinkedIn or Naukri login, so do your first runs with `--dry-run`.
- **Cloud IPs get rate-limited** by LinkedIn/Glassdoor sometimes. The laptop scans every 2 h as backup.
- **Scanned (image) govt PDFs** can't be read, so open those yourself.
