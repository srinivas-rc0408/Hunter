---
name: jobhunter
description: >-
  Srinivas's job-hunting system (private tech jobs + Karnataka/central govt
  jobs). Scans LinkedIn, Naukri, Indeed, Google Jobs, company boards, Birla
  career pages and govt notice boards; ranks by fit; fills applications in his
  logged-in browser and auto-submits only when every field is a fact he gave;
  tracks everything. Use when he says "find jobs", "scan", "apply to the new
  ones", "what's new", "govt jobs", "am I eligible", "check my pipeline",
  "tailor my resume for this", "cover letter for <id>", or names any job id.
  Works with the hunter-* skills (resume, research, connector, interview,
  salary, follow-up) through `hunt export-hunter`.
---

# JobHunter

The CLI is `hunt` (repo at `~/jobhunter`, venv at `~/jobhunter/.venv`). Run
commands from the repo root. Everything also works without Claude. This skill
just drives the same commands.

## Daily loop

0. `hunt morning`: the brief (cloud health, best new, approvals, govt deadlines, GATE).
1. `hunt sync`: pull what the cloud radar (GitHub Actions) found.
2. `hunt list --min 55`: show the queue (★ = dream company, 🏛 = govt).
3. `hunt apply --top 10`: fills forms in his browser. It auto-submits only when
   every field is a basic fact from `config/profile.yaml` or `data/answers.yaml`;
   otherwise it waits for him to click Submit and learns what he typed.
4. `hunt govt` + `hunt calendar`: govt notices with eligibility, plus deadlines.
5. Review Center http://localhost:8765 (`hunt review`): every form the bot filled, field by
   field with the source of each value, a screenshot, the tailored resume (ATS before → after)
   and the cover letter. **Approve & Submit** sends a queued form.
6. `hunt advantages`: his reservation / Karnataka / GATE advantages checklist (private file).

## Rules you must keep

- **Never invent facts.** No skill, number, date or answer that isn't in
  `config/private.yaml`, `config/profile.yaml`, `config/resume.yaml` or `data/answers.yaml`.
- **Never commit or print private data.** `config/private.yaml`, `config/resume.pdf`,
  `config/advantages.yaml`, `docs/private/` and `data/` stay out of git (the repo is public).
- Gender is set (Male). Don't ask him again. If a form
  needs something missing (expected CTC, gender, pincode), ask him and save
  the answer to the profile, never guess.
- **Never turn on auto-submit for non-basic answers.** Don't change
  `search.yaml → apply.mode` or `never_auto_submit_on` unless he asks.
- **Caste/category** is used only for govt eligibility. Never type it into a
  private company's form (diversity questions stay with him).
- **Govt applications** are never submitted by the bot (OTR, OTP, fee). Give
  him the link, the eligibility verdict (`hunt show <id>`) and the last date.
  Remind him to confirm branch wording ("AI&ML" is not always listed) before
  paying a fee.
- LinkedIn/Naukri automation is against their terms. He accepted the risk for
  basic-only submits at a capped pace (`max_per_site_per_day`). Don't raise caps.

## Commands

| goal | command |
| --- | --- |
| full scan now | `hunt scan` (`--light` quick, `--only govt,pages`) |
| one job in detail + keyword gap | `hunt show <id>` |
| apply to specific jobs | `hunt apply <id> <id>` (`--dry-run` to preview) |
| log in once | `hunt login linkedin` / `hunt login naukri` |
| cover letter (verified) | `hunt cover <id>` |
| tailored resume + ATS report (hunter-resume truth check) | `hunt tailor <id>` |
| one-page master resume + `hunter/resumes/base.md` | `hunt resume` |
| approve a queued form | `hunt apply <id> --approve` (or the button in the Review Center) |
| set status | `hunt mark <id> interview --note "HR call Fri"` |
| learned answers | `hunt answers` (file: `data/answers.yaml`) |
| why a field got that answer | `hunt classify "Expected CTC"` |
| health check | `hunt doctor --test-alert` |
| backup / restore / update | `hunt backup` · `hunt restore <file>` · `hunt update` |
| setup manual | `docs/SETUP.md` (follow it exactly; don't improvise setup steps) |
| share data with hunter-* skills | `hunt export-hunter` → `hunter/profile.json`, `hunter/pipeline.json` |

## With the hunter skills

After `hunt export-hunter`:
- `hunter-resume`: `hunt tailor <id>` already writes `hunter/resumes/base.md`, `<id>.jd.md`,
  `<id>.md` and runs `ats.py`. Use the skill's step 4 (ask about each gap, one question at a time)
  and write confirmed answers to `config/resume.yaml → facts`.
- `hunter-research`: company brief before an interview.
- `hunter-connector`: find the hiring manager and draft the note. **He** sends it.
- `hunter-interview`: mock loop. `hunter-salary`: compare offers in INR.
- `hunter-followup` and `hunter-tracker`: read `hunter/pipeline.json`.

## Coaching duty

His known weak point is explaining his technical work. When he gets an
interview (`hunt mark <id> interview`), run `hunter-interview` and drill the
2-minute project walkthrough out loud for the project that matched that job.
