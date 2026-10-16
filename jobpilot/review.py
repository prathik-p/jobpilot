"""Interactive review: walk through queued jobs one by one.

For Lever/Greenhouse/Ashby jobs the form is pre-filled for you. For everything else
(LinkedIn, Indeed, company sites) the page opens and the cover letter is printed to copy.
"""
from __future__ import annotations

import json
import logging

from .apply.ats_forms import prefill
from .apply.browser import browser
from .config import Settings
from .db import now

log = logging.getLogger(__name__)
ATS = {"lever", "greenhouse", "ashby"}


def _card(job: dict) -> str:
    v = json.loads(job.get("score_json") or "{}")
    lines = [
        "",
        "=" * 78,
        f"[{job['score']}] {job['title']}  @  {job['company']}",
        f"     {job.get('location','')}  |  {job.get('site')}  |  id {job['id'][:8]}",
        f"     {v.get('one_line','')}",
    ]
    if v.get("missing_skills"):
        lines.append(f"     gaps: {', '.join(v['missing_skills'][:6])}")
    if v.get("red_flags"):
        lines.append(f"     red flags: {', '.join(v['red_flags'])}")
    if job.get("notes"):
        lines.append(f"     note: {job['notes']}")
    lines.append(f"     {job.get('url')}")
    return "\n".join(lines)


def run(settings: Settings, db, limit: int = 20):
    jobs = [dict(r) for r in db.by_status("queued", "auto")][:limit]
    if not jobs:
        print("Review queue is empty.")
        return
    print(f"{len(jobs)} jobs to review. Commands: [a]pplied  [s]kip  [l]ater  [q]uit")
    with browser(settings, headless=False) as ctx:
        page = ctx.new_page()
        for job in jobs:
            print(_card(job))
            try:
                if job.get("site") in ATS:
                    filled = prefill(page, settings, job)
                    print(f"     pre-filled: {', '.join(filled) or 'nothing (check the page)'}")
                    print("     -> answer any custom questions, then press Submit in the browser.")
                else:
                    page.goto(job["apply_url"] or job["url"], wait_until="domcontentloaded", timeout=45000)
                    if job.get("cover_letter"):
                        print("\n----- cover letter -----\n" + job["cover_letter"] + "\n------------------------")
            except Exception as e:
                print(f"     could not open page: {e}")
            choice = input("     result? [a/s/l/q]: ").strip().lower()[:1]
            if choice == "a":
                db.set(job["id"], status="applied", applied_at=now(), notes="applied (reviewed)")
            elif choice == "s":
                db.set(job["id"], status="skipped")
            elif choice == "q":
                break
