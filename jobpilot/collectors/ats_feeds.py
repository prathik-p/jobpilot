"""Public company job feeds: Lever, Greenhouse, Ashby, SmartRecruiters and Workday.

All are public JSON APIs (the same ones the companies' own career pages call): no scraping, no login.
Lever / Greenhouse / Ashby return every posting with its description in one request.
SmartRecruiters / Workday list postings first; the description is fetched only for postings that
are recent, new to the database and pass the cheap title/location check.
"""
from __future__ import annotations

import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
from urllib.parse import urlparse

import requests

from ..config import Settings
from ..db import job_id

log = logging.getLogger(__name__)
UA = {"User-Agent": "JobPilot/1.0 (personal job search)"}
JSON_UA = {**UA, "Content-Type": "application/json", "Accept": "application/json"}

INDIA_HINTS = ["bangalore", "bengaluru", "hyderabad", "kochi", "cochin", "kerala",
               "india", "karnataka", "telangana"]
# Whole words only: "india" must not match "Indianapolis" / "Indiana"
INDIA_WORDS = re.compile(r"(?<![a-z])(india|bangalore|bengaluru|hyderabad|kochi|cochin|bngl|blr)(?![a-z])")


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


def _entries(items) -> list[tuple[str, str]]:
    """companies.yaml entries are 'value' or {display name: value}. Returns [(name, value)]."""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            out += [(str(k), str(v)) for k, v in it.items()]
        else:
            out.append((str(it), str(it)))
    return out


# ---------- Lever / Greenhouse / Ashby: one request, full descriptions ----------

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


# ---------- SmartRecruiters / Workday: list first, details only for promising new postings ----------

def _always(title, location=None, is_remote=False) -> bool:
    return True


def smartrecruiters(company: str, name: str | None = None, max_age_days: int = 14,
                    keep=_always, seen: set | frozenset = frozenset()) -> list[dict]:
    api = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).date().isoformat()
    name = name or company
    out, offset = [], 0
    while offset < 2000:
        r = requests.get(api, params={"limit": 100, "offset": offset, "country": "in"}, headers=UA, timeout=20)
        r.raise_for_status()
        d = r.json()
        posts = d.get("content") or []
        for p in posts:
            posted = (p.get("releasedDate") or "")[:10]
            if posted and posted < cutoff:
                continue
            loc = p.get("location") or {}
            where = loc.get("fullLocation") or ", ".join(x for x in [loc.get("city"), loc.get("region")] if x)
            remote = bool(loc.get("remote"))
            title = p.get("name", "")
            if job_id(title, name, where) in seen or not keep(title, where, remote):
                continue
            try:
                det = requests.get(f"{api}/{p['id']}", headers=UA, timeout=20).json()
            except Exception as e:
                log.debug("smartrecruiters %s detail failed: %s", p.get("id"), e)
                continue
            sections = (det.get("jobAd") or {}).get("sections") or {}
            desc = "\n\n".join(_strip_html((sections.get(k) or {}).get("text", ""))
                               for k in ["jobDescription", "qualifications", "additionalInformation"])
            out.append({
                "source": "smartrecruiters", "site": "smartrecruiters", "title": title, "company": name,
                "location": where, "is_remote": int(remote), "url": det.get("postingUrl"),
                "apply_url": det.get("applyUrl") or det.get("postingUrl"), "description": desc.strip(),
                "date_posted": posted,
            })
        offset += len(posts)
        if not posts or offset >= (d.get("totalFound") or 0):
            break
    return out


def _workday_api(url: str) -> tuple[str, str, str]:
    """'https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite' -> (host, tenant, site)."""
    u = urlparse(url if "//" in url else "https://" + url)
    parts = [p for p in u.path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Za-z]{2}", p)]
    if not u.netloc or not parts:
        raise ValueError(f"not a Workday careers URL: {url}")
    return u.netloc, u.netloc.split(".")[0], parts[0]


def _facet_ids(facets: list, match) -> dict:
    """Location facet values whose label matches -> {facetParameter: [ids]}. Searches nested groups."""
    found: dict = {}
    for f in facets or []:
        for v in f.get("values") or []:
            if "facetParameter" in v:
                for k, ids in _facet_ids([v], match).items():
                    found.setdefault(k, []).extend(ids)
            elif match((v.get("descriptor") or "").strip().lower()):
                found.setdefault(f["facetParameter"], []).append(v["id"])
    return found


def _india_filter(facets: list) -> dict:
    """Workday sites name their location facets differently; find the 'India' country value,
    else fall back to the individual city values."""
    country = _facet_ids(facets, lambda d: d == "india")
    if country:
        k = next(iter(country))
        return {k: country[k]}
    return _facet_ids(facets, lambda d: INDIA_WORDS.search(d) is not None)


def _days_ago(posted_on: str) -> int:
    s = (posted_on or "").lower()
    if "today" in s:
        return 0
    if "yesterday" in s:
        return 1
    m = re.search(r"(\d+)\+?\s*day", s)
    return int(m.group(1)) if m else 0


def _workday_search(api: str, facets: dict | None, offset: int = 0) -> dict:
    body = {"appliedFacets": facets or {}, "limit": 20, "offset": offset, "searchText": ""}
    r = requests.post(f"{api}/jobs", json=body, headers=JSON_UA, timeout=25)
    r.raise_for_status()
    return r.json()


def workday(url: str, name: str | None = None, max_age_days: int = 14,
            keep=_always, seen: set | frozenset = frozenset()) -> list[dict]:
    host, tenant, site = _workday_api(url)
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    name = name if name and name != url else tenant.capitalize()
    facets = _india_filter(_workday_search(api, None).get("facets") or [])
    if not facets:
        log.warning("workday %s: no India location filter found, skipping", name)
        return []
    out, offset = [], 0
    while offset < 1000:  # newest first; stop once a page reaches postings older than max_age_days
        posts = _workday_search(api, facets, offset).get("jobPostings") or []
        if not posts:
            break
        recent = [p for p in posts if _days_ago(p.get("postedOn")) <= max_age_days]
        for p in recent:
            title, loc = p.get("title", ""), p.get("locationsText") or ""
            job_url = f"https://{host}/{site}{p.get('externalPath', '')}"
            multi = bool(re.fullmatch(r"\d+ locations", loc.strip().lower()))
            if job_url in seen or not keep(title, None if multi else loc):
                continue
            try:
                info = requests.get(api + p["externalPath"], headers=JSON_UA, timeout=20).json()["jobPostingInfo"]
            except Exception as e:
                log.debug("workday %s detail failed: %s", job_url, e)
                continue
            where = ", ".join(x for x in [info.get("location")] + list(info.get("additionalLocations") or []) if x)
            remote = "remote" in (where + " " + str(info.get("remoteType") or "")).lower()
            out.append({
                "source": "workday", "site": "workday", "title": title, "company": name,
                "location": where or loc, "is_remote": int(remote), "url": job_url,
                "apply_url": info.get("externalUrl") or job_url,
                "description": _strip_html(info.get("jobDescription", "")),
                "date_posted": (info.get("startDate") or "")[:10],
            })
        if len(recent) < len(posts):
            break
        offset += len(posts)
    return out


FETCHERS = {"lever": lever, "greenhouse": greenhouse, "ashby": ashby}
DETAIL_FETCHERS = {"smartrecruiters": smartrecruiters, "workday": workday}


def _tasks(settings: Settings, keep=_always, seen=frozenset()) -> list[tuple[str, str, object]]:
    max_age = settings.config["search"].get("company_max_age_days", 14)
    tasks = []
    for kind, fetch in FETCHERS.items():
        for name, slug in _entries(settings.companies.get(kind)):
            tasks.append((kind, name, partial(fetch, slug)))
    for kind, fetch in DETAIL_FETCHERS.items():
        for name, value in _entries(settings.companies.get(kind)):
            tasks.append((kind, name, partial(fetch, value, name, max_age, keep, seen)))
    return tasks


def collect(settings: Settings, keep=_always, seen: set | frozenset = frozenset()) -> list[dict]:
    """keep(title, location, is_remote): cheap pre-check so Workday/SmartRecruiters skip detail
    requests for irrelevant postings. seen: urls / job ids already in the database."""
    def run(task):
        kind, name, fetch = task
        try:
            got = [j for j in fetch() if relevant_location(j["location"], bool(j["is_remote"]))]
            log.info("%-15s %-24s -> %d India/remote jobs", kind, name[:24], len(got))
            return got
        except Exception as e:
            log.warning("%s '%s' failed: %s", kind, name, str(e)[:160])
            return []

    jobs = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for got in ex.map(run, _tasks(settings, keep, seen)):
            jobs.extend(got)
    return jobs


def _count(kind: str, value: str) -> str:
    if kind == "smartrecruiters":
        r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{value}/postings",
                         params={"limit": 1, "country": "in"}, headers=UA, timeout=20)
        r.raise_for_status()
        return f"OK ({r.json().get('totalFound', 0)} India roles)"
    if kind == "workday":
        host, tenant, site = _workday_api(value)
        api = f"https://{host}/wday/cxs/{tenant}/{site}"
        facets = _india_filter(_workday_search(api, None).get("facets") or [])
        if not facets:
            return "FAILED: no India location filter on this site"
        return f"OK ({_workday_search(api, facets).get('total', 0)} India roles)"
    return f"OK ({len(FETCHERS[kind](value))} open roles)"


def check_companies(settings: Settings) -> list[tuple[str, str, str]]:
    results = []
    for kind in [*FETCHERS, *DETAIL_FETCHERS]:
        for name, value in _entries(settings.companies.get(kind)):
            try:
                results.append((kind, name, _count(kind, value)))
            except Exception as e:
                results.append((kind, name, f"FAILED: {str(e)[:80]}"))
    return results
