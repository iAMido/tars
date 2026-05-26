"""Router behavior: daily spend caps, cooldowns on 5xx/429, CircuitOpen when all tripped.

These tests do NOT call real providers — they monkeypatch _post() to return
synthetic responses or raise. Router state is reset between tests.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tars import router


@pytest.fixture
def cfg() -> SimpleNamespace:
    """Minimal config-like object the router will read."""
    return SimpleNamespace(
        openrouter=SimpleNamespace(api_key="sk-or-fake", daily_cap_usd=0.50),
        openai=SimpleNamespace(api_key="sk-fake", daily_cap_usd=0.50),
        tiers=SimpleNamespace(
            model_dump=lambda: {
                "interactive_fast": "openai/gpt-5-mini",
                "cron_default": "deepseek/deepseek-v3.2",
                "ingest": "deepseek/deepseek-v3.2",
                "web_research": "openai/gpt-5",
            }
        ),
    )


class FakeDB:
    """db.execute is the only thing the router calls; capture inserts."""
    def __init__(self) -> None:
        self.inserts: list[tuple] = []

    async def execute(self, sql: str, params: tuple):  # noqa: D401
        self.inserts.append((sql, params))
        return None


@pytest.fixture(autouse=True)
def _reset_router_state() -> None:
    router._reset_state()
    yield
    router._reset_state()


def _ok_response(model: str = "openai/gpt-5-mini", text: str = "pong") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": text, "tool_calls": []}}],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }


@pytest.mark.asyncio
async def test_happy_path_uses_openrouter(cfg, monkeypatch) -> None:
    db = FakeDB()
    mock_post = AsyncMock(return_value=_ok_response())
    monkeypatch.setattr(router, "_post", mock_post)

    resp = await router.call(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        tier="interactive_fast",
        cfg=cfg,
        db=db,
    )
    assert resp.text == "pong"
    assert resp.provider == "openrouter"
    assert mock_post.await_count == 1
    # Verify cost_ledger got one insert.
    assert len(db.inserts) == 1


@pytest.mark.asyncio
async def test_openrouter_5xx_trips_cooldown_and_falls_to_openai(cfg, monkeypatch) -> None:
    db = FakeDB()
    call_count = {"n": 0}

    async def fake_post(provider, url, body, headers):
        call_count["n"] += 1
        if provider == "openrouter":
            # Simulate 503 — _post trips cooldown then raises.
            await router._trip("openrouter")
            req = httpx.Request("POST", url)
            res = httpx.Response(503, request=req)
            raise httpx.HTTPStatusError("503", request=req, response=res)
        return _ok_response()

    monkeypatch.setattr(router, "_post", fake_post)
    resp = await router.call(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        tier="interactive_fast",
        cfg=cfg,
        db=db,
    )
    assert resp.provider == "openai"
    assert call_count["n"] == 2
    snap = router._state_snapshot()
    assert snap["cooldowns"]["openrouter"] > time.time()


@pytest.mark.asyncio
async def test_daily_cap_skips_provider(cfg, monkeypatch) -> None:
    db = FakeDB()
    # Prefill spend to exceed the openrouter cap; openai should be tried.
    await router._record_spend("openrouter", 1.00)

    captured = {"providers": []}

    async def fake_post(provider, url, body, headers):
        captured["providers"].append(provider)
        return _ok_response()

    monkeypatch.setattr(router, "_post", fake_post)
    resp = await router.call(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        tier="interactive_fast",
        cfg=cfg,
        db=db,
    )
    assert resp.provider == "openai"
    assert captured["providers"] == ["openai"]


@pytest.mark.asyncio
async def test_circuit_open_when_all_capped(cfg, monkeypatch) -> None:
    db = FakeDB()
    await router._record_spend("openrouter", 1.00)
    await router._record_spend("openai", 1.00)

    monkeypatch.setattr(router, "_post", AsyncMock(return_value=_ok_response()))

    with pytest.raises(router.CircuitOpen):
        await router.call(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            tier="interactive_fast",
            cfg=cfg,
            db=db,
        )


@pytest.mark.asyncio
async def test_unknown_tier_raises(cfg, monkeypatch) -> None:
    db = FakeDB()
    monkeypatch.setattr(router, "_post", AsyncMock(return_value=_ok_response()))
    with pytest.raises(ValueError, match="Unknown tier"):
        await router.call(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            tier="bogus_tier",
            cfg=cfg,
            db=db,
        )


@pytest.mark.asyncio
async def test_openai_substitutes_deepseek_model(cfg, monkeypatch) -> None:
    """When OpenAI direct gets a deepseek model, it substitutes to gpt-5-mini."""
    db = FakeDB()
    captured: dict = {}

    async def fake_post(provider, url, body, headers):
        captured.setdefault("calls", []).append({"provider": provider, "model": body["model"]})
        if provider == "openrouter":
            await router._trip("openrouter")
            req = httpx.Request("POST", url)
            raise httpx.HTTPStatusError("503", request=req, response=httpx.Response(503, request=req))
        return _ok_response(model="gpt-5-mini")

    monkeypatch.setattr(router, "_post", fake_post)
    resp = await router.call(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        tier="cron_default",   # -> deepseek/deepseek-v3.2 primary
        cfg=cfg,
        db=db,
    )
    assert resp.provider == "openai"
    # The openai call must NOT carry "deepseek/..." through.
    openai_calls = [c for c in captured["calls"] if c["provider"] == "openai"]
    assert openai_calls and not openai_calls[0]["model"].startswith("deepseek/")
