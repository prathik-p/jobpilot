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

AUTO_APPLY_SITES = {"naukri", "lever", "greenhouse", "ashby"}

SYSTEM = """You are a strict technical recruiter screening jobs for one candidate.
Judge honestly: a weak match wastes the candidate's time and reputation.
Return ONLY a JSON object, no prose."""

PROMPT = """CANDIDATE PROFILE (YAML):
{profile}

JOB:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

Score fit from 0 to 100 where:
 90+  nearly every core requirement is clearly met by the profile
 75-89 strong match, 1-2 minor gaps
 60-74 plausible but notable gaps
 <60  poor fit (wrong stack, too senior, wrong domain)

Return JSON exactly in this shape:
{{
  "score": <int>,
  "seniority_ok": <true|false>,
  "required_years": <int or null>,
  "matched_skills": [<strings>],
  "missing_skills": [<strings>],
  "red_flags": [<e.g. "bond/agreement", "unpaid", "night shift", "body shopping", "10+ yrs">],
  "one_line": "<why this is or isn't a fit, max 25 words>",
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
    score = min(100, 35 + 6 * core_hits + 2 * len(matched))
    return {
        "score": score, "seniority_ok": True, "required_years": None,
        "matched_skills": matched, "missing_skills": [], "red_flags": [],
        "one_line": f"Keyword match: {', '.join(matched[:6]) or 'few overlaps'} (no API key, heuristic)",
        "pitch_points": [], "heuristic": True,
    }


def llm_score(settings: Settings, job: dict) -> dict:
    prompt = PROMPT.format(
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


def final_score(settings: Settings, job: dict, verdict: dict) -> int:
    s = verdict["score"] * location_weight(settings, job.get("location", ""), bool(job.get("is_remote")))
    if not verdict.get("seniority_ok", True):
        s -= 25
    s -= 10 * len(verdict.get("red_flags") or [])
    return int(max(0, min(100, round(s))))


def decide(settings: Settings, job: dict, score: int) -> str:
    sc = settings.config["scoring"]
    if score < sc["min_score_to_queue"]:
        return "scored"
    if score >= sc["min_score_to_auto_apply"] and job.get("site") in AUTO_APPLY_SITES:
        return "auto"
    return "queued"


def score_jobs(settings: Settings, db) -> dict:
    rows = db.by_status("new", order="collected_at DESC")
    budget = settings.config["scoring"].get("max_llm_calls_per_run", 60)
    use_llm = llm.available(settings.config["scoring"]["model"])
    if not use_llm:
        log.warning("no API key for %s — using keyword heuristic scoring", settings.config["scoring"]["model"])
    stats = {"scored": 0, "queued": 0, "auto": 0, "errors": 0}
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
