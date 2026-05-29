"""TARS read-only dashboard. FastAPI on the bot's event loop.

Endpoints:
  GET  /                    -- single-page HTML
  GET  /api/health          -- liveness + scheduler stats
  GET  /api/costs?days=N    -- cost ledger aggregated by day+tier+model
  GET  /api/jobs            -- snapshot of scheduled jobs and next_run_time
  GET  /api/jobs/stream     -- SSE stream of job snapshots, refreshes every 2s
  GET  /api/notes?limit=N   -- recent notes
  GET  /api/followups       -- open follow-ups
  GET  /api/briefings       -- recent briefings
  GET  /api/conversations   -- recent threads with message counts

Bound to cfg.network.dashboard_host (default 127.0.0.1). Production uses
`tailscale serve --bg --https=443 http://127.0.0.1:8088` to expose via MagicDNS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from tars import __version__
from tars.db import Database

log = logging.getLogger("tars.dashboard")

INDEX_PATH = Path(__file__).resolve().parent / "templates" / "index.html"


def build_app(db: Database, cfg) -> FastAPI:
    app = FastAPI(title="TARS", version=__version__, docs_url=None, redoc_url=None)
    # Stash db + cfg on app state so handlers can access without globals.
    app.state.db = db
    app.state.cfg = cfg
    app.state.boot_ts = time.time()

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        try:
            return INDEX_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "<h1>TARS dashboard</h1><p>index.html missing — check templates/.</p>"

    # ------------------------------------------------------------------
    # JSON APIs
    # ------------------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        # Cheap DB ping.
        row = await db.fetch_one("SELECT COUNT(*) AS n FROM notes")
        notes_count = int(row["n"]) if row else 0
        # Job count from APScheduler's persistent jobstore.
        try:
            jrow = await db.fetch_one("SELECT COUNT(*) AS n FROM apscheduler_jobs")
            jobs_count = int(jrow["n"]) if jrow else 0
        except Exception:  # noqa: BLE001
            jobs_count = 0
        return {
            "ok": True,
            "version": __version__,
            "uptime_s": time.time() - app.state.boot_ts,
            "tz": cfg.timezone,
            "notes": notes_count,
            "scheduled_jobs": jobs_count,
        }

    @app.get("/api/costs")
    async def costs(days: int = 30) -> list[dict]:
        cutoff = int(time.time()) - max(days, 1) * 86400
        rows = await db.fetch_all(
            "SELECT date(ts,'unixepoch','localtime') AS d, "
            " tier, model, "
            " SUM(cost_usd) AS cost, "
            " SUM(prompt_tokens) AS pt, "
            " SUM(completion_tokens) AS ct, "
            " SUM(cached_tokens) AS cached, "
            " COUNT(*) AS n "
            "FROM cost_ledger WHERE ts >= ? "
            "GROUP BY d, tier, model ORDER BY d DESC, cost DESC",
            (cutoff,),
        )
        return [dict(r) for r in rows]

    @app.get("/api/costs/daily")
    async def costs_daily(days: int = 14) -> list[dict]:
        """One row per day with total cost — for the headline chart."""
        cutoff = int(time.time()) - max(days, 1) * 86400
        rows = await db.fetch_all(
            "SELECT date(ts,'unixepoch','localtime') AS d, "
            "ROUND(SUM(cost_usd), 6) AS cost, "
            "SUM(prompt_tokens) AS pt, "
            "SUM(cached_tokens) AS cached, "
            "COUNT(*) AS n "
            "FROM cost_ledger WHERE ts >= ? "
            "GROUP BY d ORDER BY d ASC",
            (cutoff,),
        )
        return [dict(r) for r in rows]

    @app.get("/api/jobs")
    async def jobs() -> list[dict]:
        try:
            rows = await db.fetch_all(
                "SELECT id, next_run_time FROM apscheduler_jobs ORDER BY next_run_time"
            )
        except Exception:  # noqa: BLE001
            return []
        return [
            {
                "id": r["id"],
                "next_run_unix": float(r["next_run_time"]) if r["next_run_time"] else None,
            }
            for r in rows
        ]

    @app.get("/api/jobs/stream")
    async def jobs_stream(request: Request) -> EventSourceResponse:
        async def gen():
            while not await request.is_disconnected():
                try:
                    rows = await db.fetch_all(
                        "SELECT id, next_run_time FROM apscheduler_jobs ORDER BY next_run_time"
                    )
                    data = [
                        {"id": r["id"], "next_run_unix": float(r["next_run_time"]) if r["next_run_time"] else None}
                        for r in rows
                    ]
                except Exception as e:  # noqa: BLE001
                    data = {"error": str(e)}
                yield {"event": "jobs", "data": json.dumps(data, default=str)}
                await asyncio.sleep(2.0)
        return EventSourceResponse(gen())

    @app.get("/api/notes")
    async def notes(limit: int = 20) -> list[dict]:
        rows = await db.fetch_all(
            "SELECT id, datetime(created_at,'unixepoch','localtime') AS created, "
            "source, status, body "
            "FROM notes ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        )
        return [dict(r) for r in rows]

    @app.get("/api/followups")
    async def followups_endpoint() -> list[dict]:
        rows = await db.fetch_all(
            "SELECT fu.id, fu.note_id, fu.status, fu.promised_to, "
            "fu.due_at, fu.reopened_count, n.body "
            "FROM follow_ups fu JOIN notes n ON n.id = fu.note_id "
            "WHERE fu.status = 'open' "
            "ORDER BY COALESCE(fu.due_at, 9999999999) ASC LIMIT 50"
        )
        return [dict(r) for r in rows]

    @app.get("/api/briefings")
    async def briefings(limit: int = 10) -> list[dict]:
        rows = await db.fetch_all(
            "SELECT id, date, summary FROM briefings ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 50)),),
        )
        return [dict(r) for r in rows]

    @app.get("/api/conversations")
    async def conversations() -> list[dict]:
        rows = await db.fetch_all(
            "SELECT c.thread_key, "
            "  datetime(c.created_at, 'unixepoch', 'localtime') AS created, "
            "  (SELECT COUNT(*) FROM messages m WHERE m.thread_key = c.thread_key) AS msg_count, "
            "  (SELECT MAX(ts) FROM messages m WHERE m.thread_key = c.thread_key) AS last_ts, "
            "  (SELECT ROUND(SUM(cost_usd), 6) FROM messages m WHERE m.thread_key = c.thread_key) AS cost "
            "FROM conversations c "
            "ORDER BY last_ts DESC NULLS LAST"
        )
        return [dict(r) for r in rows]

    @app.get("/api/entities")
    async def entities_endpoint() -> list[dict]:
        rows = await db.fetch_all(
            "SELECT e.id, e.canonical, e.kind, "
            "GROUP_CONCAT(ea.alias, ', ') AS aliases "
            "FROM entities e LEFT JOIN entity_aliases ea ON ea.entity_id = e.id "
            "GROUP BY e.id ORDER BY e.canonical"
        )
        return [dict(r) for r in rows]

    return app


async def run_dashboard(db: Database, cfg) -> None:
    """Long-running uvicorn server. Cancellable via task.cancel()."""
    import uvicorn

    app = build_app(db, cfg)
    config = uvicorn.Config(
        app,
        host=cfg.network.dashboard_host,
        port=cfg.network.dashboard_port,
        log_level="warning",  # keep aiogram logs as primary
        access_log=False,
    )
    server = uvicorn.Server(config)
    log.info(
        "dashboard listening on http://%s:%d",
        cfg.network.dashboard_host, cfg.network.dashboard_port,
    )
    await server.serve()
