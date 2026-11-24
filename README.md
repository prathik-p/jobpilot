# JobPilot

Finds early-career tech jobs (any software role asking for up to 3 years) in Bengaluru, Hyderabad
and Kochi every few hours, scores how worth applying each one is with an LLM (Gemini or Claude),
writes a cover letter for the best matches, and sends you a digest. You apply yourself; for
Lever/Greenhouse/Ashby jobs, `review` pre-fills the form and you press Submit.

```
collect ──────────> filter ──────> score (LLM) ────> cover letter ──> digest ──> you apply
Naukri + Indeed     experience,    0-100 worth       best matches,    HTML +     review / done
+ ~90 company       title, city    applying, gaps    from profile     Telegram
  career pages
```

Nothing is ever submitted automatically. LinkedIn is not scraped, because LinkedIn bans accounts for it.

## 1. Setup (once, about 15 minutes)

Needs Python 3.10+.

```bash
cd jobpilot
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env               # Windows: copy .env.example .env
cp config.example.yaml config.yaml # Windows: copy config.example.yaml config.yaml
cp profile.example.yaml profile.yaml
```

`.env`, `config.yaml` and `profile.yaml` hold your keys and personal details and are git-ignored.
Only the `*.example` versions are committed.

Then:

1. **`.env`**: add the key for the model in `config.yaml` (`scoring.model`): `GEMINI_API_KEY`
   (free tier, aistudio.google.com) for `gemini-*`, or `ANTHROPIC_API_KEY` (console.anthropic.com)
   for `claude-*`. To switch provider, change `scoring.model`. Without a key, JobPilot still
   runs with keyword scoring and template cover letters, but matching is much weaker.
2. **`data/resume.pdf`**: put your resume here (used when forms ask for an upload).
3. **`profile.yaml`**: replace the placeholder with your own facts from your resume. The AI only
   uses facts written here, so keep it accurate.
4. **`config.yaml`**: fill in `answers` (current and expected CTC, notice period). Adjust
   `terms`, `filters` and the score thresholds if you like.
5. **`companies.yaml`**: company career pages on Lever, Greenhouse, Ashby, SmartRecruiters and
   Workday (how to add one is explained inside the file). Run `python -m jobpilot check-companies`
   after editing.
6. **Job site logins (optional)**: run `python -m jobpilot login`, sign in by hand in the window
   that opens, then press Enter. `review` reuses that browser session. No password is stored.
7. **Telegram digest (optional)**: create a bot with @BotFather, get your chat id from
   @userinfobot, and put both in `.env`.

## 2. First run

```bash
python -m jobpilot run          # collect, filter, score, cover letters, digest
python -m jobpilot list         # your apply queue
python -m jobpilot show <id>    # score reasoning + cover letter
```

Then run `python -m jobpilot board` (or double-click `scripts\board.bat`). It opens the
matches in your browser, where each job has a green ✓ (applied) and a red ✗ (discard) that save
immediately, with Undo. The Applied and Discarded tabs list everything you've marked, each with a
"Back to queue" button. Leave the window running while you use the page and press Ctrl+C when done.
`data/output/digest-latest.html` is the same page as a read-only snapshot.

## 3. Daily use

| What | Command |
|---|---|
| Run everything | `python -m jobpilot run` |
| Go through the review queue (forms pre-filled, ~1 min each) | `python -m jobpilot review` |
| **Tick off jobs in the browser (✓ applied / ✗ discard)** | `python -m jobpilot board` |
| Same in the terminal (numbered list, same numbers as the board) | `python -m jobpilot done` |
| Mark one job by id | `python -m jobpilot mark <id> applied` |
| Counts | `python -m jobpilot status` |
| Spreadsheet of jobs you applied to (`--all` for every job) | `python -m jobpilot export` |

**How jobs are routed**
- **queued**: score at least `min_score_to_queue` (55). These are your apply queue (`list`,
  `review`, `done`, digest). Cover letters are written for scores of `cover_letter_min_score` (70) and up.
- **scored / filtered**: rejected, with the reason saved in `notes`.

**How scoring works.** The score answers "how worth applying is this?" for an early-career
engineer, not "how exactly does it match my stack". Experience asks up to `max_experience_years`
count as met; a different language or framework is a gap, not a disqualifier. Location is handled
separately (city weights in `config.yaml`). Only real employer problems (bond, unpaid, night shift,
body shopping, fees, commission-only, disguised sales) count as red flags, at -10 each, two at most.
After changing filters or scoring, `python -m jobpilot requeue --days 7` re-checks recently rejected jobs.

## 4. Scheduling

**Linux/macOS (cron)**: every 4 hours from 8am to 8pm:
```
0 8-20/4 * * * /full/path/to/jobpilot/scripts/run.sh
```

**Windows**: run `scripts\schedule_windows.ps1` once in PowerShell.

**GitHub Actions (optional)**: `.github/workflows/jobpilot.yml` runs the pipeline in the cloud
(manual trigger; add a `schedule:` block to automate). Use a **private** repo and add your keys as
repository secrets, plus `CONFIG_YAML` and `PROFILE_YAML` (the full contents of your local files),
since those aren't committed.

The computer must be awake for the local schedule.

## 5. Tuning

- Too many irrelevant jobs: add words to `title_exclude`, or raise `min_score_to_queue`.
- Missing good jobs: lower thresholds, add `terms`, or increase `hours_old`.
- LinkedIn returns nothing: it blocks scrapers aggressively. Add `JOBSPY_PROXY` in `.env`, or
  remove `linkedin` from `sites`.
- API cost: each scored job is one LLM call (about $0.001 on Haiku), capped by
  `max_llm_calls_per_run` and `max_cost_per_run_usd`. Hard filters run first so you don't pay for
  obviously wrong jobs. Jobs over the cap stay `new` and are scored next run.
- More companies: add Workday / SmartRecruiters / Lever / Greenhouse / Ashby pages to `companies.yaml`.

## 6. Tests

```bash
python -m unittest discover tests
```
These run offline, with no API key, network or browser.

## Files

```
config.yaml           search, filters, thresholds, standard answers
profile.yaml          your facts (source of truth for scoring and letters)
companies.yaml        company career pages (Lever, Greenhouse, Ashby, SmartRecruiters, Workday)
jobpilot/collectors   JobSpy boards + company career page feeds
jobpilot/filters.py   experience / title / city rules
jobpilot/scorer.py    LLM scoring + routing
jobpilot/tailor.py    cover letters
jobpilot/apply/       browser session + ATS form pre-fill (used by review)
jobpilot/review.py    interactive review queue
jobpilot/digest.py    HTML / Telegram / email
jobpilot/board.py     local page with applied / discard buttons (`board`)
data/                 SQLite DB, resume, browser session, digests (git-ignored)
```
