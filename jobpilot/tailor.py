"""Generate a short, honest cover letter per good match."""
from __future__ import annotations

import json
import logging
import re

import yaml

from . import llm
from .config import Settings

log = logging.getLogger(__name__)

SYSTEM = """You write concise, specific cover letters for a software engineer.
Rules: use ONLY facts from the candidate profile. Never invent skills, employers, numbers or degrees.
No clichés ("I am writing to express", "passionate", "esteemed"). Plain, confident Indian-English
professional tone. 130-180 words. No placeholders, no subject line, no address block."""

PROMPT = """CANDIDATE PROFILE:
{profile}

JOB: {title} at {company} ({location})
JD (truncated):
{description}

Most relevant points identified earlier: {pitch}

Write the cover letter body, signed "{name}"."""


def _template(settings: Settings, job: dict, verdict: dict) -> str:
    p = settings.profile
    name = f"{p['personal']['first_name']} {p['personal']['last_name']}"
    skills = ", ".join((verdict.get("matched_skills") or [])[:5]) or "React, Next.js, NestJS and PostgreSQL"
    notice = str(settings.config.get("answers", {}).get("notice_period", "")).strip()
    avail = "I'm available to start immediately" if notice.lower().startswith("immediate") else (
        f"My notice period is {notice}" if notice else "I can share my availability")
    latest = (p.get("experience") or [{}])[0]
    wins = [h.rstrip(".") for h in (latest.get("highlights") or [])[:2]]
    did = (f" At {latest['company']} I " + "; I also ".join(w[0].lower() + w[1:] for w in wins) + "."
           if latest.get("company") and wins else "")
    cities = settings.config.get("answers", {}).get("preferred_locations") or []
    where = f" and I'm open to {', '.join(cities[:-1])} or {cities[-1]}" if len(cities) > 1 else (
        f" and I'm open to {cities[0]}" if cities else "")
    portfolio = p["personal"].get("portfolio")
    return (
        f"Hi {job['company']} team,\n\n"
        f"I'd like to apply for the {job['title']} role.{did} "
        f"The role's focus on {skills} matches the work I've been shipping.\n\n"
        f"{avail}{where}." + (f" My portfolio is at {portfolio}." if portfolio else "") +
        f"\n\nThanks for your time,\n{name}"
    )


def cover_letter(settings: Settings, job: dict) -> str:
    verdict = json.loads(job.get("score_json") or "{}")
    if not llm.available(settings.config["scoring"]["model"]):
        return _template(settings, job, verdict)
    p = settings.profile
    name = f"{p['personal']['first_name']} {p['personal']['last_name']}"
    prompt = PROMPT.format(
        profile=yaml.safe_dump(p, sort_keys=False), title=job["title"], company=job["company"],
        location=job.get("location", ""), description=(job.get("description") or "")[:5000],
        pitch="; ".join(verdict.get("pitch_points") or []), name=name,
    )
    sc = settings.config["scoring"]
    text = llm.complete(sc["model"], SYSTEM, prompt, max_tokens=2000,
                        min_interval=sc.get("min_seconds_between_calls", 0),
                        budget=sc.get("max_cost_per_run_usd"), effort=sc.get("effort"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def tailor_pending(settings: Settings, db) -> int:
    n = 0
    for row in db.by_status("queued", "auto"):
        if row["cover_letter"]:
            continue
        try:
            db.set(row["id"], cover_letter=cover_letter(settings, dict(row)))
            n += 1
        except llm.BudgetExceeded as e:
            log.info("cost cap reached, remaining letters next run (%s)", e)
            break
        except Exception as e:
            log.warning("cover letter failed for %s: %s", row["title"], e)
            if llm.is_fatal(e):
                break
    return n
