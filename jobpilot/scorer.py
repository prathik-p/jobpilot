"""Score each filtered job against your profile.

With an API key (GEMINI_API_KEY or ANTHROPIC_API_KEY, matching scoring.model): the LLM reads the full JD and returns a structured verdict.
Without it: a keyword-overlap fallback so the pipeline still runs.
"""
from __future__ import annotations

import json
import logging
import re

import yaml

from . import llm
from .config import Settings
from .filters import location_weight

log = logging.getLogger(__name__)

SYSTEM = """You screen tech jobs for one early-career software engineer who applies broadly:
any software / tech role (full stack, frontend, backend in any language, AI/ML, data, cloud/DevOps,
QA automation, mobile, platform) that fits their experience level.
Your score answers: "how worth applying is this, and how good are the interview odds?"
Return ONLY a JSON object, no prose."""

PROMPT = """CANDIDATE PROFILE (YAML):
{profile}

JOB:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

How to judge:
- Experience: any requirement up to {max_years} years counts as MET. Do not lower the score for
  "candidate has fewer years than asked" unless the role needs more than {max_years} years.
- Stack: a different language or framework is a learnable gap at this level, not a disqualifier.
  Strong engineering fundamentals, shipped production work and AI experience transfer to most roles.
- Ignore location and work mode (on-site / hybrid / remote); they are handled separately.

Score from 0 to 100:
 85+   core stack overlaps strongly (JS/TS, React/Next.js, Node/NestJS, Python, SQL, LLM/AI) and the level fits
 70-84 software role at the right level with good overlap, or a gap that is quick to close
 55-69 software/tech role at the right level but mostly a different stack or domain; still worth applying
 40-54 a real stretch: very specialised (embedded, VLSI, kernel, deep finance/scientific domain) or mostly non-coding
 <40   not a software/tech role, or clearly needs more than {max_years} years

Return JSON exactly in this shape:
{{
  "score": <int>,
  "category": "<one of: full stack, frontend, backend, ai/ml, data, cloud/devops, qa, mobile, other>",
  "seniority_ok": <true if the role suits someone with up to {max_years} years>,
  "required_years": <minimum years the JD asks for, int or null>,
  "matched_skills": [<strings>],
  "missing_skills": [<strings>],
  "red_flags": [<ONLY real employer problems, from: "bond/agreement", "unpaid", "fees to apply",
                 "night shift", "body shopping", "commission only", "sales role in disguise">],
  "one_line": "<why this is or isn't worth applying to, max 25 words>",
  "pitch_points": [<2-3 real achievements from the profile most relevant to this job>]
}}"""


def _profile_skills(profile: dict) -> set[str]:
    skills = set()
    for vals in (profile.get("skills") or {}).values():
        for v in vals:
            skills.add(v.lower())
    return skills


def heuristic_score(settings: Settings, job: dict) -> dict:
    text = f"{job.get('title','')} {job.get('description','')}".lower()
    skills = _profile_skills(settings.profile)
    matched = sorted(s for s in skills if re.search(rf"(?<![a-z]){re.escape(s)}(?![a-z])", text))
    core = ["react", "next.js", "node.js", "typescript", "postgresql", "nestjs", "javascript", "python"]
    core_hits = sum(1 for c in core if c in text)
    score = min(100, 45 + 5 * core_hits + 2 * len(matched))
    return {
        "score": score, "seniority_ok": True, "required_years": None,
        "matched_skills": matched, "missing_skills": [], "red_flags": [],
        "one_line": f"Keyword match: {', '.join(matched[:6]) or 'few overlaps'} (no API key, heuristic)",
        "pitch_points": [], "heuristic": True,
    }


def llm_score(settings: Settings, job: dict) -> dict:
    prompt = PROMPT.format(
        max_years=settings.config["filters"].get("max_experience_years", 3),
        profile=yaml.safe_dump({k: v for k, v in settings.profile.items() if k != "personal"},
                               sort_keys=False),
        title=job["title"], company=job["company"], location=job.get("location", ""),
        description=(job.get("description") or "")[:9000],
    )
    sc = settings.config["scoring"]
    out = llm.complete(sc["model"], SYSTEM, prompt, max_tokens=4000,
                       min_interval=sc.get("min_seconds_between_calls", 0),
                       budget=sc.get("max_cost_per_run_usd"), effort=sc.get("effort"))
    data = llm.parse_json(out)
    data["score"] = int(max(0, min(100, int(data.get("score", 0)))))
    return data


# Only these count against the score; anything else the model lists is ignored, so experience,
# stack or location complaints can't sneak in as "red flags" and double-penalise a job.
RED_FLAGS = {"bond/agreement", "unpaid", "fees to apply", "night shift", "body shopping",
             "commission only", "sales role in disguise"}


def real_red_flags(verdict: dict) -> list[str]:
    return [f for f in verdict.get("red_flags") or [] if str(f).strip().lower() in RED_FLAGS]


def final_score(settings: Settings, job: dict, verdict: dict) -> int:
    s = verdict["score"] * location_weight(settings, job.get("location", ""), bool(job.get("is_remote")))
    # The raw score already reflects experience. Only penalise again when the job asks for more
    # than filters.max_experience_years (or the model said it's too senior without giving a number).
    req = verdict.get("required_years")
    max_years = settings.config["filters"].get("max_experience_years", 3)
    if (req is not None and req > max_years) or (req is None and not verdict.get("seniority_ok", True)):
        s -= 25
    s -= 10 * min(2, len(real_red_flags(verdict)))
    return int(max(0, min(100, round(s))))


def decide(settings: Settings, job: dict, score: int) -> str:
    return "queued" if score >= settings.config["scoring"]["min_score_to_queue"] else "scored"


def score_jobs(settings: Settings, db) -> dict:
    rows = db.by_status("new", order="collected_at DESC")
    budget = settings.config["scoring"].get("max_llm_calls_per_run", 60)
    use_llm = llm.available(settings.config["scoring"]["model"])
    if not use_llm:
        log.warning("no API key for %s — using keyword heuristic scoring", settings.config["scoring"]["model"])
    stats = {"scored": 0, "queued": 0, "errors": 0}
    for row in rows:
        job = dict(row)
        if use_llm and budget <= 0:
            break  # leave the rest as 'new' for next run
        try:
            verdict = llm_score(settings, job) if use_llm else heuristic_score(settings, job)
            if use_llm:
                budget -= 1
        except llm.BudgetExceeded as e:
            log.info("cost cap reached, rest stays 'new' for next run (%s)", e)
            break
        except Exception as e:
            log.warning("scoring failed for %s @ %s: %s", job["title"], job["company"], e)
            stats["errors"] += 1
            if use_llm and llm.is_fatal(e):
                log.error("stopping scoring: check scoring.model and your API key (jobs stay 'new')")
                break
            continue
        score = final_score(settings, job, verdict)
        status = decide(settings, job, score)
        db.set(job["id"], score=score, score_json=json.dumps(verdict), status=status)
        stats[status if status in stats else "scored"] += 1
        log.info("%3d %-6s %s @ %s", score, status, job["title"][:50], job["company"][:30])
    return stats
