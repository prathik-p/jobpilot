"""Pre-fill Lever / Greenhouse / Ashby application forms. You check the page and press Submit.

Custom questions differ per company, so the final click stays with you. Standard fields
(name, email, phone, links, resume, cover letter, location) are filled automatically.
"""
from __future__ import annotations

import logging
import re

from ..config import Settings

log = logging.getLogger(__name__)


def _fill(page, selectors: list[str], value: str) -> bool:
    if not value:
        return False
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible() and not (loc.input_value() or "").strip():
                loc.fill(str(value))
                return True
        except Exception:
            continue
    return False


def _fill_by_label(page, pattern: str, value: str) -> bool:
    if not value:
        return False
    try:
        loc = page.get_by_label(re.compile(pattern, re.I)).first
        if loc.count() and loc.is_visible() and not (loc.input_value() or "").strip():
            loc.fill(str(value))
            return True
    except Exception:
        pass
    return False


def _upload(page, selectors: list[str], path: str) -> bool:
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count():
                loc.set_input_files(path)
                return True
        except Exception:
            continue
    return False


def apply_url_for(job: dict) -> str:
    url = job.get("apply_url") or job.get("url") or ""
    if job.get("site") == "lever" and "/apply" not in url:
        url = url.rstrip("/") + "/apply"
    if job.get("site") == "ashby" and "/application" not in url:
        url = url.rstrip("/") + "/application"
    return url


def prefill(page, settings: Settings, job: dict) -> list[str]:
    p = settings.profile["personal"]
    a = settings.config.get("answers", {})
    full = f"{p['first_name']} {p['last_name']}"
    resume = settings.path("resume_pdf")
    filled = []

    page.goto(apply_url_for(job), wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)

    steps = [
        ("name", lambda: _fill(page, ["input[name='name']", "input[name='_systemfield_name']"], full)),
        ("first name", lambda: _fill(page, ["#first_name", "input[name='first_name']"], p["first_name"])
         or _fill_by_label(page, r"^first name", p["first_name"])),
        ("last name", lambda: _fill(page, ["#last_name", "input[name='last_name']"], p["last_name"])
         or _fill_by_label(page, r"^last name", p["last_name"])),
        ("email", lambda: _fill(page, ["input[name='email']", "#email", "input[type='email']"], p["email"])),
        ("phone", lambda: _fill(page, ["input[name='phone']", "#phone", "input[type='tel']"], p["phone"])),
        ("location", lambda: _fill(page, ["input[name='location']", "#candidate-location"], p.get("location", ""))
         or _fill_by_label(page, r"^(current )?location|city", p.get("location", ""))),
        ("linkedin", lambda: _fill(page, ["input[name='urls[LinkedIn]']"], p.get("linkedin", ""))
         or _fill_by_label(page, r"linkedin", p.get("linkedin", ""))),
        ("github", lambda: _fill(page, ["input[name='urls[GitHub]']"], p.get("github", ""))
         or _fill_by_label(page, r"github", p.get("github", ""))),
        ("portfolio", lambda: _fill(page, ["input[name='urls[Portfolio]']", "input[name='urls[Other]']"],
                                    p.get("portfolio", ""))
         or _fill_by_label(page, r"portfolio|website", p.get("portfolio", ""))),
        ("notice period", lambda: _fill_by_label(page, r"notice period", a.get("notice_period", ""))),
        ("current ctc", lambda: _fill_by_label(page, r"current (ctc|salary|compensation)", a.get("current_ctc_lpa", ""))),
        ("expected ctc", lambda: _fill_by_label(page, r"expected (ctc|salary|compensation)", a.get("expected_ctc_lpa", ""))),
        ("experience", lambda: _fill_by_label(page, r"years of (relevant )?experience",
                                              str(a.get("total_experience_years", "")))),
        ("cover letter", lambda: _fill(page, ["textarea[name='comments']", "#cover_letter_text",
                                              "textarea[name='cover_letter_text']"], job.get("cover_letter") or "")
         or _fill_by_label(page, r"cover letter|additional information", job.get("cover_letter") or "")),
        ("resume", lambda: resume.exists() and _upload(
            page, ["input[name='resume']", "#resume", "input[type='file'][id*='resume']",
                   "input[type='file']"], str(resume))),
    ]
    for label, fn in steps:
        try:
            if fn():
                filled.append(label)
        except Exception as e:
            log.debug("prefill %s failed: %s", label, e)
    return filled
