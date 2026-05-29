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
from apscheduler.triggers.interval import IntervalTrigger

from tars.scheduler.brain_reindex import brain_reindex_job
from tars.scheduler.calendar_pull import calendar_pull_job
from tars.scheduler.email_summary import email_summary_job
from tars.scheduler.morning_briefing import morning_briefing_job
from tars.scheduler.runtime import set_runtime
from tars.scheduler.weekly_followup_reconcile import weekly_followup_reconcile_job

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

    # 05:00 daily — the marquee feature.
    sched.add_job(
        morning_briefing_job,
        CronTrigger(hour=5, minute=0, timezone=cfg.timezone),
        id="morning_briefing",
        replace_existing=True,
        misfire_grace_time=900,
    )

    # Every 30 min — proactive email summary if >=3 new threads since last fire.
    sched.add_job(
        email_summary_job,
        IntervalTrigger(minutes=30),
        id="email_summary",
        replace_existing=True,
    )

    # Every 15 min — refresh cached cal_events for fast briefing/tool reads.
    sched.add_job(
        calendar_pull_job,
        IntervalTrigger(minutes=15),
        id="calendar_pull",
        replace_existing=True,
    )

    # Every 15 min — diff-mode reindex of brain_docs (FTS5 + vec0).
    sched.add_job(
        brain_reindex_job,
        IntervalTrigger(minutes=15),
        id="brain_reindex",
        replace_existing=True,
    )

    # Sunday 18:00 — reopen overdue follow-ups + send the open list to Telegram.
    sched.add_job(
        weekly_followup_reconcile_job,
        CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=cfg.timezone),
        id="weekly_followup_reconcile",
        replace_existing=True,
        misfire_grace_time=3600,  # 1h grace — it's the weekly nag, not time-critical
    )

    log.info(
        "scheduler built with %d jobs: %s",
        len(sched.get_jobs()),
        [j.id for j in sched.get_jobs()],
    )
    return sched
