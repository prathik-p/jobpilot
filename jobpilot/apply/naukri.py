"""Auto-apply to high-scoring Naukri jobs that use Naukri's own one-click Apply.

- Uses your saved browser session (run `python -m jobpilot login` once).
- Jobs that redirect to a company site, or open Naukri's screening-question chatbot,
  are moved to your review queue instead of being guessed at.
- Respects a daily cap and random delays. Dry-run by default.
"""
from __future__ import annotations

import logging
import re
import sys
import time

from ..config import Settings
from ..db import now
from .browser import browser, human_pause

log = logging.getLogger(__name__)

APPLY_BTN = ["button#apply-button", "button.apply-button", "button:has-text('Apply')"]
COMPANY_SITE_BTN = ["button#company-site-button", "button:has-text('Apply on company site')"]
ALREADY = re.compile(r"\bapplied\b", re.I)
SUCCESS = re.compile(r"(successfully applied|you have applied|application sent|applied to)", re.I)
QUOTA = re.compile(r"daily quota", re.I)
CAPTCHA = ["iframe[src*='captcha']", "iframe[title*='captcha' i]", "text=To continue your request"]
CHATBOT = ["div.chatbot_DrawerContentWrapper", "div[class*='chatbot']", "div:has-text('Kindly answer')"]


def _first(page, selectors, timeout=2500):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


def _wait_for_captcha(page, interactive: bool, timeout: int = 180) -> bool:
    """Naukri sometimes shows a CAPTCHA to automated browsers. We never try to solve it:
    if you're at the terminal, you solve it in the window and we continue. Returns True once clear."""
    if not _first(page, CAPTCHA, 1500):
        return True
    if not interactive:
        return False
    print("\n>>> Naukri is showing a CAPTCHA. Solve it in the browser window (waiting up to "
          f"{timeout // 60} min)...", flush=True)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        page.wait_for_timeout(2000)
        if not _first(page, CAPTCHA, 500):
            page.wait_for_timeout(2500)
            return True
    return False


def logged_in(page) -> bool:
    page.goto("https://www.naukri.com/mnjuser/homepage", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    return "nlogin" not in page.url and "login" not in page.url.lower()


def apply_one(page, job: dict, dry_run: bool, interactive: bool = False) -> tuple[str, str]:
    """Returns (status, note)."""
    page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    if not _wait_for_captcha(page, interactive):
        return "queued", "Naukri showed a CAPTCHA, apply manually"

    if _first(page, COMPANY_SITE_BTN, 1500):
        return "queued", "Naukri redirects to company site, apply manually"
    btn = _first(page, APPLY_BTN)
    if not btn:
        return "failed", "no Apply button found (posting may be closed)"
    label = (btn.inner_text() or "").strip()
    if ALREADY.search(label):
        return "applied", "already applied earlier"
    if dry_run:
        return "auto", "DRY RUN: would click Apply"

    btn.click()
    page.wait_for_timeout(3500)
    body = page.inner_text("body")
    if QUOTA.search(body):
        return "auto", "QUOTA"  # stop the run, retry tomorrow
    if _first(page, CHATBOT, 1500):
        return "queued", "Naukri asked screening questions, finish manually"
    if SUCCESS.search(body):
        return "applied", "applied via Naukri"
    after = _first(page, APPLY_BTN + ["button:has-text('Applied')"], 1500)
    if after and ALREADY.search(after.inner_text() or ""):
        return "applied", "applied via Naukri"
    return "failed", "clicked Apply but could not confirm success"


def run(settings: Settings, db) -> dict:
    cfg = settings.config["apply"]["naukri"]
    if not cfg.get("enabled"):
        return {"skipped": "naukri applier disabled"}
    dry = settings.dry_run
    cap = int(cfg.get("max_per_day", 25)) - db.applied_today("naukri")
    jobs = [dict(r) for r in db.by_status("auto") if r["site"] == "naukri"]
    stats = {"applied": 0, "queued": 0, "failed": 0, "dry_run": dry, "candidates": len(jobs)}
    if not jobs or cap <= 0:
        return stats

    headless = cfg.get("headless", False)
    interactive = not headless and sys.stdin.isatty()
    with browser(settings, headless=headless) as ctx:
        page = ctx.new_page()
        if not logged_in(page):
            log.error("Not logged in to Naukri. Run: python -m jobpilot login")
            stats["error"] = "not logged in"
            return stats
        for job in jobs[:cap]:
            try:
                status, note = apply_one(page, job, dry, interactive)
            except Exception as e:
                status, note = "failed", f"error: {str(e)[:120]}"
            if note == "QUOTA":
                log.warning("Naukri daily quota reached, stopping")
                break
            fields = {"status": status, "notes": note}
            if status == "applied":
                fields["applied_at"] = now()
            db.set(job["id"], **fields)
            stats[status] = stats.get(status, 0) + 1
            log.info("naukri %-8s %s @ %s (%s)", status, job["title"][:40], job["company"][:25], note)
            human_pause(settings)
    return stats
