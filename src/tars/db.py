"""Database layer.

Single aiosqlite connection + sqlite-vec extension + migration runner.

Invariants (do not break without an ADR; see PLAN.md §3):
  - One connection per process (this Database instance).
  - WAL mode, NORMAL synchronous, busy_timeout 5000ms.
  - All writes go through `db.writer_lock` (asyncio.Lock).
    Many concurrent readers are fine; serializing writes prevents
    SQLITE_BUSY contention between scheduled jobs and the Telegram bot.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import aiosqlite
import sqlite_vec

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
VEC_EMBEDDING_DIM = 1024  # voyage-3-large int8, 1024-dim


class Database:
    """Wrapper around an aiosqlite connection with extension load + migration runner."""

    def __init__(self, conn: aiosqlite.Connection, path: Path) -> None:
        self.conn = conn
        self.path = path
        self.writer_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def connect(cls, db_path: str | Path) -> Database:
        """Open the database, load sqlite-vec, set pragmas. Idempotent."""
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row

        # Load sqlite-vec extension.
        await conn.enable_load_extension(True)
        try:
            await conn.load_extension(sqlite_vec.loadable_path())
        finally:
            # Best practice: disable extension loading once we're done,
            # to prevent SQL injection from loading arbitrary .so/.dll later.
            await conn.enable_load_extension(False)

        # Pragmas. WAL must be set per-file; the others are per-connection.
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.commit()

        return cls(conn, path)

    async def close(self) -> None:
        await self.conn.close()

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    async def migrate(self) -> int:
        """Apply pending SQL migrations from ./migrations/*.sql in numeric order.

        Plus the programmatic vec0 virtual table (which can't live in .sql
        because the sqlite-vec extension must already be loaded — which it is
        after connect(), but we want to keep the vec_docs DDL self-documenting
        in code rather than implicit).

        Returns the new schema version.
        """
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_versions ("
            " v INTEGER PRIMARY KEY,"
            " applied_at INTEGER NOT NULL"
            ")"
        )

        cur = await self.conn.execute("SELECT COALESCE(MAX(v), 0) FROM schema_versions")
        row = await cur.fetchone()
        current: int = row[0] if row else 0

        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        applied = current
        for f in files:
            try:
                n = int(f.name.split("_", 1)[0])
            except ValueError:
                log.warning("Skipping non-numeric migration file: %s", f.name)
                continue
            if n <= current:
                continue
            log.info("Applying migration %s", f.name)
            await self.conn.executescript(f.read_text(encoding="utf-8"))
            await self.conn.execute(
                "INSERT INTO schema_versions(v, applied_at) VALUES (?, strftime('%s','now'))",
                (n,),
            )
            await self.conn.commit()
            applied = n

        # Programmatic vec0 table — depends on sqlite-vec being loaded.
        # Idempotent (IF NOT EXISTS), so safe to re-run.
        await self.conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_docs USING vec0("
            f" doc_id INTEGER PRIMARY KEY,"
            f" embedding FLOAT[{VEC_EMBEDDING_DIM}]"
            f")"
        )
        await self.conn.commit()

        return applied

    # ------------------------------------------------------------------
    # Convenience helpers (more added in later phases)
    # ------------------------------------------------------------------

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        cur = await self.conn.execute(sql, params)
        return await cur.fetchone()

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(sql, params)
        return list(await cur.fetchall())

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        """Execute under the writer lock. Use for INSERT/UPDATE/DELETE."""
        async with self.writer_lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur
