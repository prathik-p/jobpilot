"""Cheap hard filters, run before spending anything on the LLM."""
from __future__ import annotations

import re

from .config import Settings

CITY_ALIASES = {
    "bengaluru": ["bengaluru", "bangalore", "blr"],
    "hyderabad": ["hyderabad", "secunderabad", "hyd"],
    "kochi": ["kochi", "cochin", "ernakulam", "kakkanad", "infopark"],
}

# "3-5 years", "3 to 5 yrs", "4+ years", "minimum of 5 years", "at least 4 years"
_RANGE = re.compile(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*(?:years|yrs|year)", re.I)
_PLUS = re.compile(r"(\d{1,2})\s*\+\s*(?:years|yrs|year)", re.I)
_MIN = re.compile(r"(?:minimum|min\.?|at\s*least)\s*(?:of\s*)?(\d{1,2})\s*(?:years|yrs|year)", re.I)


def required_experience(text: str) -> int | None:
    """Best guess of the *minimum* years a posting asks for. None if not stated."""
    text = text or ""
    mins = [int(a) for a, _ in _RANGE.findall(text)]
    mins += [int(x) for x in _PLUS.findall(text)]
    mins += [int(x) for x in _MIN.findall(text)]
    mins = [m for m in mins if m <= 20]
    return min(mins) if mins else None


def city_of(location: str) -> str | None:
    l = (location or "").lower()
    for city, aliases in CITY_ALIASES.items():
        if any(a in l for a in aliases):
            return city
    return None


def location_weight(settings: Settings, location: str, is_remote: bool) -> float:
    city = city_of(location)
    for loc in settings.config["search"]["locations"]:
        if city and city in loc["name"].lower().replace("bangalore", "bengaluru"):
            return float(loc.get("weight", 1.0))
    if is_remote or "remote" in (location or "").lower():
        return 0.95
    return 0.0  # not one of your cities


def check(settings: Settings, job: dict) -> tuple[bool, str]:
    f = settings.config["filters"]
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()
    company = (job.get("company") or "").lower()

    if any(b.lower() == company for b in f.get("company_blocklist", [])):
        return False, "company blocklisted"
    if not any(k in title for k in f.get("title_must_include_any", [])):
        return False, "title not relevant"
    for bad in f.get("title_exclude", []):
        term = bad.strip().lower()
        if term and re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", title):
            return False, f"title contains '{term}'"
    for bad in f.get("description_exclude", []):
        if bad.lower() in desc:
            return False, f"description contains '{bad}'"

    # Naukri puts the range in the URL ("...-6-to-11-years-..."), sometimes nowhere else
    url = (job.get("url") or "").replace("-", " ") if job.get("site") == "naukri" else ""
    req = required_experience(f"{job.get('description', '')} {job.get('title', '')} {url}")
    if req is not None and req > f.get("max_experience_years", 3):
        return False, f"asks for {req}+ years"

    if location_weight(settings, job.get("location", ""), bool(job.get("is_remote"))) == 0:
        return False, f"location '{job.get('location')}' not targeted"
    return True, ""
