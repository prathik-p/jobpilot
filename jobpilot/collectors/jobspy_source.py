"""Collect jobs from Naukri, Indeed, Google Jobs and LinkedIn via JobSpy.

Each (site, term, city) is queried separately so one blocked board doesn't kill the run.
"""
from __future__ import annotations

import logging
import math
import time

from ..config import Settings, env
from ..db import _REGION_WORDS, _norm

log = logging.getLogger(__name__)


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _with_city(location: str, searched: str) -> str:
    """Indeed often gives only a state ('KA, India'). Add the city we searched so filters can place it."""
    words = [_norm(part).split(" ")[0] for part in location.split(",") if _norm(part)]
    if searched != "India" and words and "remote" not in words and all(w in _REGION_WORDS for w in words):
        return f"{searched.split(',')[0]}, {location}"
    return location


def _rows_to_jobs(df, city: str) -> list[dict]:
    jobs = []
    for r in df.to_dict(orient="records"):
        title, company = _clean(r.get("title")), _clean(r.get("company"))
        if not title or not company:
            continue
        jobs.append({
            "source": "jobspy",
            "site": str(r.get("site") or ""),
            "title": str(title),
            "company": str(company),
            "location": _with_city(str(_clean(r.get("location")) or city), city),
            "is_remote": int(bool(_clean(r.get("is_remote")))),
            "url": _clean(r.get("job_url")),
            "apply_url": _clean(r.get("job_url_direct")) or _clean(r.get("job_url")),
            "description": str(_clean(r.get("description")) or ""),
            "date_posted": str(_clean(r.get("date_posted")) or ""),
            "min_salary": _clean(r.get("min_amount")),
            "max_salary": _clean(r.get("max_amount")),
        })
    return jobs


def collect(settings: Settings) -> list[dict]:
    try:
        from jobspy import scrape_jobs
    except ImportError:
        log.error("python-jobspy not installed: pip install python-jobspy")
        return []

    cfg = settings.config["search"]
    proxy = env("JOBSPY_PROXY")
    cities = [loc["name"] for loc in cfg["locations"]]
    if cfg.get("include_remote_india"):
        cities.append("India")

    all_jobs: list[dict] = []
    for site in cfg["sites"]:
        tried = found = 0
        for term in cfg["terms"]:
            for city in cities:
                if tried >= 3 and found == 0:
                    break  # site is blocking us this run; don't burn time on the rest
                if city == "India" and site != "linkedin" and site != "naukri":
                    continue  # remote-India search only makes sense on these two
                kwargs = dict(
                    site_name=[site],
                    search_term=term,
                    location=city,
                    results_wanted=cfg.get("results_per_search", 30),
                    hours_old=cfg.get("hours_old", 48),
                    country_indeed="India",
                    description_format="markdown",
                    fetch_description=True,  # Naukri returns no JD without this (one extra request per job)
                    verbose=0,
                )
                if site == "google":
                    kwargs["google_search_term"] = f"{term} jobs in {city.split(',')[0]} since yesterday"
                if site == "linkedin" and city == "India":
                    kwargs["is_remote"] = True
                if proxy:
                    kwargs["proxies"] = [proxy]
                tried += 1
                try:
                    df = scrape_jobs(**kwargs)
                    got = _rows_to_jobs(df, city) if df is not None else []
                    log.info("jobspy %-8s %-32s %-28s -> %d", site, term[:32], city[:28], len(got))
                    all_jobs.extend(got)
                    found += len(got)
                except Exception as e:  # boards block often; keep going
                    log.warning("jobspy %s '%s' %s failed: %s", site, term, city, str(e)[:160])
                time.sleep(2)
            else:
                continue
            log.warning("jobspy %s returned nothing for %d searches, skipping it this run", site, tried)
            break
    return all_jobs
