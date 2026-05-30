"""Cost rollup — daily at midnight (local time).

Aggregates yesterday's cost_ledger rows into a single cost_rollups row.
Cheaper for the dashboard to read than re-aggregating the raw ledger on
every page load. Also gives us an immutable historical view in case we
ever prune old cost_ledger rows.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("tars.scheduler.cost_rollup_daily")


async def cost_rollup_daily_job() -> dict:
    from tars.scheduler.runtime import get_runtime
    rt = get_runtime()
    return await cost_rollup_daily(rt.db, rt.cfg)


async def cost_rollup_daily(db, cfg, target_date: str | None = None) -> dict:
    """Roll up the day specified (default: yesterday in cfg.timezone)."""
    tz = ZoneInfo(cfg.timezone)
    if target_date is None:
        yesterday = datetime.now(tz).date() - timedelta(days=1)
        target_date = yesterday.isoformat()

    # Compute day boundaries in local time, then convert to unix for SQL.
    day_start = datetime.fromisoformat(target_date).replace(tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    start_ts = int(day_start.timestamp())
    end_ts = int(day_end.timestamp())

    rows = await db.fetch_all(
        "SELECT tier, model, cost_usd, prompt_tokens, completion_tokens, cached_tokens "
        "FROM cost_ledger WHERE ts >= ? AND ts < ?",
        (start_ts, end_ts),
    )

    if not rows:
        log.info("cost_rollup_daily: no ledger rows for %s, skipping", target_date)
        return {"date": target_date, "rolled": 0}

    total = 0.0
    calls = 0
    pt = ct = cached = 0
    by_tier: dict[str, dict] = defaultdict(lambda: {
        "cost": 0.0, "calls": 0, "prompt": 0, "completion": 0, "cached": 0,
    })
    by_model: dict[str, float] = defaultdict(float)

    for r in rows:
        c = float(r["cost_usd"] or 0)
        p = int(r["prompt_tokens"] or 0)
        co = int(r["completion_tokens"] or 0)
        ca = int(r["cached_tokens"] or 0)
        tier = r["tier"] or "?"
        model = r["model"] or "?"

        total += c
        calls += 1
        pt += p; ct += co; cached += ca
        bt = by_tier[tier]
        bt["cost"] += c
        bt["calls"] += 1
        bt["prompt"] += p
        bt["completion"] += co
        bt["cached"] += ca
        by_model[model] += c

    await db.execute(
        "INSERT INTO cost_rollups("
        " date, total_usd, by_tier, by_model, calls, "
        " prompt_tokens, completion_tokens, cached_tokens, generated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(date) DO UPDATE SET "
        " total_usd=excluded.total_usd, by_tier=excluded.by_tier, by_model=excluded.by_model, "
        " calls=excluded.calls, prompt_tokens=excluded.prompt_tokens, "
        " completion_tokens=excluded.completion_tokens, cached_tokens=excluded.cached_tokens, "
        " generated_at=excluded.generated_at",
        (
            target_date,
            round(total, 6),
            json.dumps({k: {**v, "cost": round(v["cost"], 6)} for k, v in by_tier.items()}),
            json.dumps({k: round(v, 6) for k, v in by_model.items()}),
            calls, pt, ct, cached, int(time.time()),
        ),
    )

    log.info(
        "cost_rollup_daily: %s rolled $%.4f across %d calls (cached %d of %d prompt tokens = %.0f%%)",
        target_date, total, calls, cached, pt, (cached / pt * 100) if pt else 0,
    )
    return {
        "date": target_date,
        "rolled": calls,
        "total_usd": round(total, 6),
        "cache_pct": round((cached / pt * 100) if pt else 0, 1),
    }
