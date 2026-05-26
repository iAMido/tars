"""Cost calculation tests."""

from __future__ import annotations

from tars.util.cost import CACHED_INPUT_MULTIPLIER, FALLBACK_PRICE, price_for


def test_known_model_basic() -> None:
    # gpt-5-mini = $0.25 in / $2.00 out per 1M
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    cost = price_for("openai/gpt-5-mini", usage)
    assert abs(cost - 2.25) < 1e-6


def test_cached_tokens_get_discount() -> None:
    # 1M prompt tokens, all cached -> in cost = 1M * 0.25 * 0.25 / 1M = 0.0625
    usage = {
        "prompt_tokens": 1_000_000,
        "completion_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 1_000_000},
    }
    cost = price_for("openai/gpt-5-mini", usage)
    expected = 1_000_000 * 0.25 * CACHED_INPUT_MULTIPLIER / 1_000_000
    assert abs(cost - expected) < 1e-6


def test_unknown_model_uses_fallback() -> None:
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    cost = price_for("totally/unknown-model", usage)
    expected = FALLBACK_PRICE[0]  # input cost for 1M tokens
    assert abs(cost - expected) < 1e-6


def test_zero_usage_zero_cost() -> None:
    assert price_for("openai/gpt-5-mini", {}) == 0.0
    assert price_for("openai/gpt-5-mini", None) == 0.0


def test_normalize_handles_bare_names() -> None:
    # Bare "gpt-5-mini" should be treated as openai/gpt-5-mini
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    assert price_for("gpt-5-mini", usage) == price_for("openai/gpt-5-mini", usage)
