"""Module-level holder for scheduler job runtime state.

APScheduler's SQLAlchemyJobStore pickles the job and its args so it can survive
restarts. But our `agent`, `db`, and `cfg` aren't picklable (they hold open
aiosqlite connections, etc.). Standard idiom: jobs take no live-state args.
Instead, they look up the singletons here at execution time.

set_runtime() is called once at scheduler build time. Jobs call get_runtime()
when they fire. Crash if used before init — better loud than silent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class _Runtime:
    agent: Any
    db: Any
    cfg: Any


_runtime: _Runtime | None = None


def set_runtime(agent, db, cfg) -> None:
    global _runtime
    _runtime = _Runtime(agent=agent, db=db, cfg=cfg)


def get_runtime() -> _Runtime:
    if _runtime is None:
        raise RuntimeError(
            "scheduler runtime not initialized — set_runtime() must run "
            "before any job fires"
        )
    return _runtime
