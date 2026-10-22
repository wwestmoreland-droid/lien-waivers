"""In-process scheduler. Railway runs one web replica, and the job lock keeps a
scheduled sync and a Sync-now click from overlapping even if that changes."""
from __future__ import annotations

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.db.session import session_scope
from app.services.locks import JobAlreadyRunning
from app.services.settings_store import get_setting
from app.services.sync_runner import sync_now

log = structlog.get_logger()

_scheduler: BackgroundScheduler | None = None
SYNC_JOB_ID = "smartsheet_sync"


def _scheduled_sync() -> None:
    try:
        sync_now(trigger="schedule")
    except JobAlreadyRunning:
        log.info("sync.skipped_already_running")
    except Exception:
        log.exception("sync.scheduled_failed")


def current_interval_minutes() -> int:
    with session_scope() as db:
        return int(get_setting(db, "sync_interval_minutes"))


def start() -> None:
    global _scheduler
    if _scheduler or not get_settings().run_scheduler:
        return
    _scheduler = BackgroundScheduler(timezone=get_settings().timezone)
    _scheduler.add_job(_scheduled_sync, IntervalTrigger(minutes=current_interval_minutes()), id=SYNC_JOB_ID,
                       max_instances=1, coalesce=True, replace_existing=True)
    _scheduler.start()
    log.info("scheduler.started", interval=current_interval_minutes())


def reschedule(minutes: int) -> None:
    if _scheduler:
        _scheduler.reschedule_job(SYNC_JOB_ID, trigger=IntervalTrigger(minutes=minutes))


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def next_run_time():
    if not _scheduler:
        return None
    job = _scheduler.get_job(SYNC_JOB_ID)
    return job.next_run_time if job else None
