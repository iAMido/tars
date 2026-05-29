"""APScheduler setup. Shares the bot's asyncio event loop (AsyncIOScheduler).

Persistent jobstore is the same SQLite file the rest of TARS uses, so missed
jobs after a restart are picked up (within `misfire_grace_time`).

Phase 6 MVP ships with just morning_briefing. Other jobs (email_summary,
calendar_pull, brain_reindex, weekly_followup_reconcile) follow the same
shape and slot in later.
"""

from __future__ import annotations

import logging

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from tars.scheduler.morning_briefing import morning_briefing_job
from tars.scheduler.runtime import set_runtime

log = logging.getLogger("tars.scheduler")


def build_scheduler(agent, db, cfg) -> AsyncIOScheduler:
    """Build the AsyncIOScheduler. Caller is responsible for sched.start()."""
    # Stash runtime singletons so jobs can look them up at fire time.
    # Job functions must be parameter-free for SQLAlchemyJobStore pickling.
    set_runtime(agent=agent, db=db, cfg=cfg)

    jobstores = {
        "default": SQLAlchemyJobStore(url=f"sqlite:///{cfg.paths.db}")
    }
    sched = AsyncIOScheduler(
        jobstores=jobstores,
        job_defaults={
            "coalesce": True,        # collapse missed identical runs
            "max_instances": 1,      # never overlap a long-running job with itself
            "misfire_grace_time": 600,  # 10 minutes
        },
        timezone=cfg.timezone,
    )

    # 05:00 daily — the marquee feature. Configurable hour for testing.
    # NO args — job_runner reads agent/db/cfg from scheduler.runtime.
    sched.add_job(
        morning_briefing_job,
        CronTrigger(hour=5, minute=0, timezone=cfg.timezone),
        id="morning_briefing",
        replace_existing=True,
        misfire_grace_time=900,  # 15 min for the briefing specifically
    )

    log.info(
        "scheduler built with %d jobs: %s",
        len(sched.get_jobs()),
        [j.id for j in sched.get_jobs()],
    )
    return sched
