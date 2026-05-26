"""Per-model token pricing for the cost ledger.

Prices are in USD per 1M tokens, taken from compass_artifact.md §6 (May 2026 snapshot).
Re-check provider pricing pages periodically — these will drift.

Cached input tokens are billed at a discount on supported providers (typically
10-25% of fresh-input price). We approximate at 25% to stay conservative on
cost reporting; the real number lives in `usage.prompt_tokens_details.cached_tokens`
from the provider response.
"""

from __future__ import annotations

# (input_per_1M_usd, output_per_1M_usd) -- normalize all model-id variants below.
PRICING: dict[str, tuple[float, float]] = {
    "openai/gpt-5-mini": (0.25, 2.00),
    "openai/gpt-5": (0.625, 5.00),
    "openai/gpt-4o-mini": (0.15, 0.60),     # safe fallback if gpt-5-mini unavailable
    "openai/gpt-4o": (2.50, 10.00),
    "deepseek/deepseek-v3.2": (0.26, 0.38),
    "deepseek/deepseek-chat": (0.27, 1.10),  # legacy alias
    "anthropic/claude-haiku-4.5": (1.00, 5.00),
}

# Models priced as gpt-4o-mini if we end up there for any reason.
FALLBACK_PRICE = (0.15, 0.60)

CACHED_INPUT_MULTIPLIER = 0.25


def _normalize(model: str) -> str:
    """Accept both 'openai/gpt-5-mini' and bare 'gpt-5-mini' as the same key."""
    if "/" in model:
        return model
    # Heuristic: bare names with 'gpt' assume openai, 'deepseek' assume deepseek.
    if model.startswith("gpt"):
        return f"openai/{model}"
    if model.startswith("deepseek"):
        return f"deepseek/{model}"
    return model


def price_for(model: str, usage: dict | None) -> float:
    """Compute USD cost of a single LLM call from a usage dict.

    `usage` is the provider-returned dict; expected keys:
      - prompt_tokens (int)
      - completion_tokens (int)
      - prompt_tokens_details.cached_tokens (int, optional)
    """
    if not usage:
        return 0.0

    norm = _normalize(model)
    in_rate, out_rate = PRICING.get(norm, FALLBACK_PRICE)

    pt = int(usage.get("prompt_tokens", 0) or 0)
    ct = int(usage.get("completion_tokens", 0) or 0)
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)

    fresh_in = max(pt - cached, 0)
    cost = (
        fresh_in * in_rate / 1_000_000
        + cached * in_rate * CACHED_INPUT_MULTIPLIER / 1_000_000
        + ct * out_rate / 1_000_000
    )
    return round(cost, 8)
