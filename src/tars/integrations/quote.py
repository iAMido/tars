"""ZenQuotes — async wrapper around their /api/today endpoint.

Free, no API key required. Returns a single "quote of the day" suitable
for the morning briefing's *Quote* section. Graceful degradation: any
error returns None and the briefing omits the section.

Endpoint: GET https://zenquotes.io/api/today → [{q, a, h}].
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("tars.integrations.quote")

ZENQUOTES_URL = "https://zenquotes.io/api/today"


async def fetch_quote_of_the_day() -> dict[str, Any] | None:
    """Return {"text": ..., "author": ...} or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(ZENQUOTES_URL)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("quote fetch failed (%s)", e)
        return None

    if not isinstance(data, list) or not data:
        return None
    q = data[0]
    text = (q.get("q") or "").strip()
    author = (q.get("a") or "").strip()
    if not text:
        return None
    return {"text": text, "author": author or "Unknown"}
