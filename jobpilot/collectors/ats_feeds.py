"""Public job feeds from Lever, Greenhouse and Ashby. No scraping, no login, no blocking."""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone

import requests

from ..config import Settings

log = logging.getLogger(__name__)
UA = {"User-Agent": "JobPilot/1.0 (personal job search)"}

INDIA_HINTS = ["bangalore", "bengaluru", "hyderabad", "kochi", "cochin", "kerala",
               "india", "karnataka", "telangana"]


def _strip_html(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def relevant_location(loc: str, remote: bool) -> bool:
    l = (loc or "").lower()
    if any(h in l for h in INDIA_HINTS):
        return True
    # "Remote" with no country, or "Remote - APAC/Asia": keep, scoring decides
    return remote and (l in ("", "remote") or "apac" in l or "asia" in l)


def _ms_to_iso(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except Exception:
        return ""


def lever(slug: str) -> list[dict]:
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for p in r.json():
        cats = p.get("categories") or {}
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        remote = (p.get("workplaceType") or "").lower() == "remote"
        desc = p.get("descriptionPlain") or ""
        for lst in p.get("lists") or []:
            desc += f"\n\n{lst.get('text','')}\n" + _strip_html(lst.get("content", ""))
        desc += "\n" + (p.get("additionalPlain") or "")
        out.append({
            "source": "lever", "site": "lever", "title": p.get("text", ""), "company": slug,
            "location": loc, "is_remote": int(remote), "url": p.get("hostedUrl"),
            "apply_url": p.get("applyUrl") or p.get("hostedUrl"), "description": desc.strip(),
            "date_posted": _ms_to_iso(p.get("createdAt")),
        })
    return out


def greenhouse(slug: str) -> list[dict]:
    base = f"https://boards-api.greenhouse.io/v1/boards/{slug}"
    r = requests.get(f"{base}/jobs?content=true", headers=UA, timeout=20)
    r.raise_for_status()
    company = slug
    try:
        company = requests.get(base, headers=UA, timeout=10).json().get("name") or slug
    except Exception:
        pass
    out = []
    for j in r.json().get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append({
            "source": "greenhouse", "site": "greenhouse", "title": j.get("title", ""),
            "company": company, "location": loc, "is_remote": int("remote" in loc.lower()),
            "url": j.get("absolute_url"), "apply_url": j.get("absolute_url"),
            "description": _strip_html(j.get("content", "")),
            "date_posted": (j.get("first_published") or j.get("updated_at") or "")[:10],
        })
    return out


def ashby(slug: str) -> list[dict]:
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        loc = ", ".join(x for x in locs if x)
        out.append({
            "source": "ashby", "site": "ashby", "title": j.get("title", ""), "company": slug,
            "location": loc, "is_remote": int(bool(j.get("isRemote"))), "url": j.get("jobUrl"),
            "apply_url": j.get("applyUrl") or j.get("jobUrl"),
            "description": j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", "")),
            "date_posted": (j.get("publishedAt") or "")[:10],
        })
    return out


FETCHERS = {"lever": lever, "greenhouse": greenhouse, "ashby": ashby}


def collect(settings: Settings) -> list[dict]:
    jobs = []
    for kind, fetch in FETCHERS.items():
        for slug in settings.companies.get(kind) or []:
            try:
                got = [j for j in fetch(slug) if relevant_location(j["location"], bool(j["is_remote"]))]
                log.info("%-10s %-24s -> %d India/remote jobs", kind, slug, len(got))
                jobs.extend(got)
            except Exception as e:
                log.warning("%s '%s' failed: %s", kind, slug, str(e)[:160])
    return jobs


def check_companies(settings: Settings) -> list[tuple[str, str, str]]:
    results = []
    for kind, fetch in FETCHERS.items():
        for slug in settings.companies.get(kind) or []:
            try:
                n = len(fetch(slug))
                results.append((kind, slug, f"OK ({n} open roles)"))
            except Exception as e:
                results.append((kind, slug, f"FAILED: {str(e)[:80]}"))
    return results
