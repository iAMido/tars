"""Running Coach app — async fetcher for today's training plan.

Reads from the user's Next.js running-coach Vercel app via the
/api/cron/tars-today endpoint (which TARS authored). Auth: shared
CRON_SECRET as Bearer.

Configured per-user via [running_coach] block in config.toml. Empty
base_url or auth_token silently disables — briefing omits the section.

Endpoint returns:
  {
    "date": "2026-06-02",
    "day_of_week": "Tuesday",
    "has_plan": true | false,
    "plan": { type, current_week, total_weeks, week_date_range },
    "today": { ...workout... } | null,
    "week_outline": [ { day, summary } x 7 ]
  }
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("tars.integrations.running_coach")


async def fetch_today_training(cfg) -> dict[str, Any] | None:
    """Return the running-coach app's today response, or None on failure /
    when not configured. The dict is fed straight to the briefing payload."""
    rc = getattr(cfg, "running_coach", None)
    if rc is None or not rc.base_url or not rc.auth_token or not rc.user_id:
        return None

    url = rc.base_url.rstrip("/") + "/api/cron/tars-today"
    headers = {"Authorization": f"Bearer {rc.auth_token}"}
    params = {"user_id": rc.user_id}

    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("running_coach fetch failed (%s)", e)
        return None

    # The endpoint returns has_plan=false (200) when there's no active plan.
    # Treat that as "nothing to show" — return None so payload key is omitted.
    if not data.get("has_plan"):
        return None
    return data
