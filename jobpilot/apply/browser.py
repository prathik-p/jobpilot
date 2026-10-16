"""Shared Playwright browser with a persistent profile, so you log in once and stay logged in."""
from __future__ import annotations

import random
import time
from contextlib import contextmanager

from ..config import Settings


@contextmanager
def browser(settings: Settings, headless: bool = False):
    from playwright.sync_api import sync_playwright

    profile_dir = settings.root / "data" / "browser_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            viewport={"width": 1280, "height": 860},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        try:
            yield ctx
        finally:
            ctx.close()


def human_pause(settings: Settings):
    lo, hi = settings.config["apply"].get("delay_seconds", [8, 20])
    time.sleep(random.uniform(lo, hi))
