"""LLM router with prefix-caching, daily spend caps, and per-provider cooldowns.

Design (PLAN.md §5 Phase 2, compass_artifact.md §6):
  - Tier maps to a model id via cfg.tiers (interactive_fast, cron_default, ...).
  - OpenRouter is the primary transport; OpenAI direct is the fallback.
  - On HTTP 5xx / 429, the provider goes into a 60s cooldown and we fall through.
  - Per-provider daily USD spend cap; over the cap, that provider is skipped.
  - When all providers are tripped, raise CircuitOpen.
  - Every call writes one row to cost_ledger with provider, model, tier, token
    counts (including cached_tokens), and computed USD cost.

State (`_cooldowns`, `_daily_spend`) is module-level. This is fine for a
single-process design (PLAN §3 invariant: one process, one Agent, one SQLite).
For multi-process we'd persist to SQLite — out of scope for V1.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from tars.util.cost import price_for

log = logging.getLogger("tars.router")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

COOLDOWN_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 60.0

# Per-tier hard cap on completion tokens. Guardrail against verbose models that
# ignore "be terse" prompt rules. interactive_fast in particular has been
# observed to generate 1500+ token essays despite explicit instructions.
TIER_MAX_TOKENS: dict[str, int] = {
    "interactive_fast": 400,    # short Telegram answers — never a paragraph essay
    "cron_default": 1500,       # morning briefings etc can be longer
    "ingest": 800,              # entity extraction, summaries
    "web_research": 2000,       # /research command, full report
}

# OpenAI direct: substitute non-OpenAI models with a safe OpenAI equivalent.
OPENAI_DIRECT_SUBSTITUTIONS: dict[str, str] = {
    # If the primary tier model was a DeepSeek route, fall back to a cheap
    # OpenAI model for direct calls.
    "deepseek/deepseek-v3.2": "gpt-5-mini",
    "deepseek/deepseek-chat": "gpt-5-mini",
}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[dict] = field(default_factory=list)
    cached_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    provider: str = ""
    cost_usd: float = 0.0


class CircuitOpen(Exception):
    """All configured providers are tripped (cooled-down or over daily cap)."""


# ---------------------------------------------------------------------------
# Router state
# ---------------------------------------------------------------------------


_cooldowns: dict[str, float] = {}                   # provider -> unix_ts when ok again
_daily_spend: dict[tuple[str, str], float] = {}     # (provider, yyyy-mm-dd) -> usd
_state_lock = asyncio.Lock()


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _cap_for(provider: str, cfg) -> float:
    if provider == "openrouter":
        return float(cfg.openrouter.daily_cap_usd)
    if provider == "openai":
        return float(cfg.openai.daily_cap_usd)
    return 0.0


def _cap_ok(provider: str, cfg) -> bool:
    return _daily_spend.get((provider, _today()), 0.0) < _cap_for(provider, cfg)


def _cooldown_ok(provider: str) -> bool:
    return _cooldowns.get(provider, 0.0) <= time.time()


async def _record_spend(provider: str, usd: float) -> None:
    async with _state_lock:
        key = (provider, _today())
        _daily_spend[key] = _daily_spend.get(key, 0.0) + usd


async def _trip(provider: str, secs: int = COOLDOWN_SECONDS) -> None:
    async with _state_lock:
        _cooldowns[provider] = time.time() + secs
    log.warning("Provider %s cooled down for %ds", provider, secs)


# Test/inspection helpers (used by unit tests).
def _reset_state() -> None:
    _cooldowns.clear()
    _daily_spend.clear()


def _state_snapshot() -> dict[str, Any]:
    return {"cooldowns": dict(_cooldowns), "daily_spend": dict(_daily_spend)}


# ---------------------------------------------------------------------------
# Per-provider HTTP call
# ---------------------------------------------------------------------------


async def _post(provider: str, url: str, body: dict, headers: dict) -> dict:
    """Single HTTP call. Trips the provider on 5xx/429 and re-raises."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        try:
            r = await client.post(url, json=body, headers=headers)
        except httpx.TransportError as e:
            await _trip(provider)
            raise
        if r.status_code in (429, 500, 502, 503, 504):
            await _trip(provider)
            raise httpx.HTTPStatusError(
                f"{provider} returned {r.status_code}",
                request=r.request,
                response=r,
            )
        r.raise_for_status()
        return r.json()


def _apply_max_tokens(body: dict, tier: str) -> None:
    cap = TIER_MAX_TOKENS.get(tier)
    if cap is not None:
        body["max_tokens"] = cap


async def _call_openrouter(
    model: str, messages: list[dict], tools: list[dict] | None, cfg, tier: str
) -> dict:
    body: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    _apply_max_tokens(body, tier)
    headers = {
        "Authorization": f"Bearer {cfg.openrouter.api_key}",
        "HTTP-Referer": "https://tars.local",
        "X-Title": "TARS",
    }
    return await _post("openrouter", OPENROUTER_URL, body, headers)


async def _call_openai(
    model: str, messages: list[dict], tools: list[dict] | None, cfg, tier: str
) -> dict:
    # OpenAI direct does not understand 'openai/' prefix or non-OpenAI models.
    if model.startswith("openai/"):
        model = model.removeprefix("openai/")
    elif model in OPENAI_DIRECT_SUBSTITUTIONS:
        model = OPENAI_DIRECT_SUBSTITUTIONS[model]
    elif "/" in model:
        # Unknown non-openai model; substitute to a known-cheap OpenAI model.
        model = "gpt-5-mini"

    body: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    _apply_max_tokens(body, tier)
    headers = {"Authorization": f"Bearer {cfg.openai.api_key}"}
    return await _post("openai", OPENAI_URL, body, headers)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def call(
    *,
    messages: list[dict],
    tools: list[dict] | None,
    tier: str,
    cfg,
    db,
    thread_key: str | None = None,
    job_id: str | None = None,
) -> LLMResponse:
    """Dispatch an LLM call through the tier router with fallback + caps + cooldowns."""

    # Tier -> model id resolution (from config).
    tiers = cfg.tiers.model_dump()
    if tier not in tiers:
        raise ValueError(f"Unknown tier: {tier}. Known: {sorted(tiers)}")
    primary_model = tiers[tier]

    last_err: Exception | None = None
    for provider in ("openrouter", "openai"):
        if not _cooldown_ok(provider):
            log.debug("Skip %s: cooled down for %.1fs more", provider, _cooldowns[provider] - time.time())
            continue
        if not _cap_ok(provider, cfg):
            log.warning("Skip %s: daily cap reached ($%.2f)", provider, _daily_spend.get((provider, _today()), 0))
            continue

        try:
            if provider == "openrouter":
                data = await _call_openrouter(primary_model, messages, tools, cfg, tier)
                model_used = data.get("model", primary_model)
            else:
                data = await _call_openai(primary_model, messages, tools, cfg, tier)
                model_used = data.get("model", primary_model)
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            last_err = e
            log.warning("Provider %s failed: %s", provider, e)
            continue

        usage = data.get("usage") or {}
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
        cost = price_for(model_used, usage)
        await _record_spend(provider, cost)

        await db.execute(
            "INSERT INTO cost_ledger("
            " ts, provider, model, tier, job_id,"
            " prompt_tokens, completion_tokens, cached_tokens, cost_usd"
            ") VALUES (strftime('%s','now'), ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                provider,
                model_used,
                tier,
                job_id,
                int(usage.get("prompt_tokens", 0) or 0),
                int(usage.get("completion_tokens", 0) or 0),
                cached,
                cost,
            ),
        )

        msg = (data.get("choices") or [{}])[0].get("message") or {}
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=msg.get("tool_calls") or [],
            cached_tokens=cached,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            model=model_used,
            provider=provider,
            cost_usd=cost,
        )

    raise CircuitOpen(f"All providers tripped or capped. Last error: {last_err}")
