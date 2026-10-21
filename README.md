# JobPilot

Finds full-stack jobs in Bengaluru, Hyderabad and Kochi every few hours, scores them against your
profile with an LLM (Gemini or Claude), writes a cover letter for each good match, auto-applies on Naukri where it's
safe, pre-fills Lever/Greenhouse/Ashby forms for you, and sends you a digest.

```
collect ──> filter ──> score (LLM)   ──> cover letter ──> apply / review ──> digest
JobSpy +     experience,   0-100 fit,       honest, from       Naukri auto      HTML +
ATS feeds    title, city   gaps, red flags  profile.yaml only  ATS pre-fill     Telegram
```

**Safety defaults:** `dry_run: true` in `config.yaml` (it clicks nothing until you flip it).
LinkedIn is used for discovery only, never automated, because LinkedIn bans accounts for it.

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
5. **`companies.yaml`**: add startups you like that hire on Lever/Greenhouse/Ashby (instructions
   inside the file), then run `python -m jobpilot check-companies`.
6. **Naukri login**: run `python -m jobpilot login`, sign in by hand in the window that opens,
   then press Enter. No password is stored; the browser session is saved locally.
7. **Telegram digest (optional)**: create a bot with @BotFather, get your chat id from
   @userinfobot, and put both in `.env`.

## 2. First runs (dry run)

```bash
python -m jobpilot run          # full pipeline, Naukri step only pretends to click
python -m jobpilot list         # what it found
python -m jobpilot show <id>    # score reasoning + cover letter
```

Open `data/output/digest-latest.html` to see the day's matches.

Do this for 2–3 days. If the `auto` list is jobs you'd genuinely apply to, set
`apply.dry_run: false`. If not, raise `min_score_to_auto_apply` or tighten the filters.

## 3. Daily use

| What | Command |
|---|---|
| Run everything | `python -m jobpilot run` |
| Go through the review queue (forms pre-filled, ~1 min each) | `python -m jobpilot review` |
| Mark jobs you applied to (numbered list, no ids) | `python -m jobpilot done` |
| Mark one job by id | `python -m jobpilot mark <id> applied` |
| Counts | `python -m jobpilot status` |
| Spreadsheet of jobs you applied to (`--all` for every job) | `python -m jobpilot export` |

**How jobs are routed**
- **auto**: score at least 80 on Naukri, Lever, Greenhouse or Ashby. Naukri jobs are applied to
  automatically (up to 25 a day). ATS jobs are pre-filled in `review` and you press Submit.
- **queued**: score 65–79, or from LinkedIn/Indeed/Google, or Naukri asked screening questions.
  These go to `review`.
- **scored / filtered**: rejected, with the reason saved in `notes`.

## 4. Scheduling

**Linux/macOS (cron)**: every 4 hours from 8am to 8pm:
```
0 8-20/4 * * * /full/path/to/jobpilot/scripts/run.sh
```

**Windows**: run `scripts\schedule_windows.ps1` once in PowerShell.

**GitHub Actions (optional)**: `.github/workflows/jobpilot.yml` runs collect, score and digest
in the cloud every 4 hours and never applies. Use a **private** repo and add your keys as
repository secrets, plus `CONFIG_YAML` and `PROFILE_YAML` (the full contents of your local files),
since those aren't committed. Your laptop then only needs to run `apply` and `review`.

The computer must be awake for the local schedule. The Naukri step needs your saved login, so it
only runs locally.

## 5. Tuning

- Too many irrelevant jobs: add words to `title_exclude`, or raise `min_score_to_queue`.
- Missing good jobs: lower thresholds, add `terms`, or increase `hours_old`.
- LinkedIn returns nothing: it blocks scrapers aggressively. Add `JOBSPY_PROXY` in `.env`, or
  remove `linkedin` from `sites`.
- API cost: each scored job is one LLM call, capped by `max_llm_calls_per_run`. Hard filters
  run first so you don't pay for obviously wrong jobs.
- Naukri page changed: selectors are at the top of `jobpilot/apply/naukri.py`.

## 6. Tests

```bash
python -m unittest discover tests
```
These run offline, with no API key, network or browser.

## Files

```
config.yaml           search, filters, thresholds, standard answers
profile.yaml          your facts (source of truth for scoring and letters)
companies.yaml        Lever / Greenhouse / Ashby company slugs
jobpilot/collectors   JobSpy boards + ATS JSON feeds
jobpilot/filters.py   experience / title / city rules
jobpilot/scorer.py    LLM scoring + routing
jobpilot/tailor.py    cover letters
jobpilot/apply/       Naukri auto-apply, ATS form pre-fill
jobpilot/review.py    interactive review queue
jobpilot/digest.py    HTML / Telegram / email
data/                 SQLite DB, resume, browser session, digests (git-ignored)
```
