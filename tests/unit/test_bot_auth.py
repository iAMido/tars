"""Authorization filter for the Telegram bot.

Real polling/integration tests would need a Telegram test bot and a chat ID,
which we keep out of CI. Instead: unit-test the filter behavior in isolation.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tars.bot.handlers import AuthFilter


def _fake_message(chat_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        text="hello",
        from_user=SimpleNamespace(id=chat_id, username="ido"),
    )


@pytest.mark.asyncio
async def test_allowed_chat_passes() -> None:
    f = AuthFilter([123, 456])
    assert await f(_fake_message(123)) is True
    assert await f(_fake_message(456)) is True


@pytest.mark.asyncio
async def test_disallowed_chat_dropped() -> None:
    f = AuthFilter([123])
    assert await f(_fake_message(999)) is False


@pytest.mark.asyncio
async def test_empty_allowlist_drops_everyone() -> None:
    f = AuthFilter([])
    assert await f(_fake_message(123)) is False
    assert await f(_fake_message(456)) is False
