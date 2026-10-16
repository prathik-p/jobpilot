"""Thin LLM wrapper (Gemini or Claude, picked by model id) with JSON parsing and a no-key fallback."""
from __future__ import annotations

import json
import logging
import re
import time

from .config import env

log = logging.getLogger(__name__)
_clients: dict = {}
_last_call = 0.0
_spent = 0.0  # USD this process (= this run)

# USD per 1M tokens (input, output). Unknown Claude models are priced as Opus so the cap stays safe.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}
FREE = (0.0, 0.0)


class BudgetExceeded(Exception):
    pass


def _price(model: str) -> tuple[float, float]:
    if _provider(model) == "gemini":
        return FREE  # free tier
    return PRICES.get(model, PRICES["claude-opus-5-5"])


def spent() -> float:
    return _spent

KEYS = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
logging.getLogger("google_genai").setLevel(logging.WARNING)  # hides per-call "AFC is enabled" noise


def _provider(model: str) -> str:
    return "gemini" if model.lower().startswith("gemini") else "anthropic"


def available(model: str | None = None) -> bool:
    if model is None:
        return any(env(k) for k in KEYS.values())
    return bool(env(KEYS[_provider(model)]))


def client(provider: str = "anthropic"):
    if provider not in _clients:
        if provider == "gemini":
            from google import genai
            _clients[provider] = genai.Client(api_key=env("GEMINI_API_KEY"))
        else:
            import anthropic
            _clients[provider] = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    return _clients[provider]


def _gemini(model: str, system: str, prompt: str, max_tokens: int) -> str:
    from google.genai import types
    cfg = types.GenerateContentConfig(system_instruction=system, max_output_tokens=max_tokens)
    waits = [15, 30, 60]  # free tier: back off on rate limits (429) and "high demand" (503)
    for attempt in range(len(waits) + 1):
        try:
            resp = client("gemini").models.generate_content(model=model, contents=prompt, config=cfg)
            return (resp.text or "").strip()
        except Exception as e:
            transient = any(k in str(e) for k in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE"))
            if transient and attempt < len(waits):
                log.warning("Gemini busy (%s), retrying in %ds", str(e)[:3], waits[attempt])
                time.sleep(waits[attempt])
                continue
            raise
    return ""


def complete(model: str, system: str, prompt: str, max_tokens: int = 1200,
             min_interval: float = 0, budget: float | None = None, effort: str | None = None) -> str:
    """budget: stop (BudgetExceeded) if this call could push the run's spend past it."""
    global _last_call, _spent
    pin, pout = _price(model)
    worst = ((len(system) + len(prompt)) / 3 * pin + max_tokens * pout) / 1e6  # ~3 chars/token, generous
    if budget is not None and _spent + worst > budget:
        raise BudgetExceeded(f"run spend ${_spent:.4f}, next call could cost up to ${worst:.4f}")
    wait = min_interval - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    try:
        if _provider(model) == "gemini":
            return _gemini(model, system, prompt, max_tokens)
        extra = {"output_config": {"effort": effort}} if effort else {}
        resp = client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            **extra,
        )
        _spent += (resp.usage.input_tokens * pin + resp.usage.output_tokens * pout) / 1e6
        log.debug("llm usage in=%d out=%d run_spend=$%.4f", resp.usage.input_tokens,
                  resp.usage.output_tokens, _spent)
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    finally:
        _last_call = time.monotonic()


def is_fatal(e: Exception) -> bool:
    """Errors that will fail every call (bad model id, bad key), so retrying the next job is pointless."""
    s = str(e)
    return any(k in s for k in ("404", "NOT_FOUND", "401", "403", "PERMISSION_DENIED",
                                "API_KEY_INVALID", "authentication_error"))


def parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        raise ValueError(f"no JSON in model output: {text[:200]}")
    return json.loads(m.group(1))
