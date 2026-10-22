"""In-process scheduler. Railway runs one web replica, and the job lock keeps a
scheduled sync and a Sync-now click from overlapping even if that changes."""
from __future__ import annotations

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.db.session import session_scope
from app.services.locks import JobAlreadyRunning
from app.services.settings_store import get_setting
from app.services.sync_runner import contacts_backfill_now, mail_now, reminders_now, sync_now

log = structlog.get_logger()

_scheduler: BackgroundScheduler | None = None
SYNC_JOB_ID = "smartsheet_sync"
MAIL_JOB_ID = "mail_poll"
REMINDER_JOB_ID = "daily_reminders"


def _scheduled_sync() -> None:
    try:
        sync_now(trigger="schedule")
    except JobAlreadyRunning:
        log.info("sync.skipped_already_running")
    except Exception:
        log.exception("sync.scheduled_failed")


def _guarded(name: str, fn) -> None:
    try:
        fn()
    except JobAlreadyRunning:
        log.info(f"{name}.skipped_already_running")
    except Exception:
        log.exception(f"{name}.failed")


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
    _scheduler.add_job(lambda: _guarded("mail", mail_now), IntervalTrigger(minutes=current_interval_minutes()),
                       id=MAIL_JOB_ID, max_instances=1, coalesce=True, replace_existing=True)
    # Reminders and escalations once a day on weekdays, so subs don't get them at odd hours.
    _scheduler.add_job(lambda: _guarded("reminders", reminders_now), CronTrigger(day_of_week="mon-fri", hour=8, minute=0),
                       id=REMINDER_JOB_ID, max_instances=1, coalesce=True, replace_existing=True)
    _scheduler.add_job(lambda: _guarded("contacts_backfill", contacts_backfill_now), IntervalTrigger(minutes=1),
                       id="contacts_backfill", max_instances=1, coalesce=True, replace_existing=True)
    _scheduler.start()
    log.info("scheduler.started", interval=current_interval_minutes())


def reschedule(minutes: int) -> None:
    if _scheduler:
        _scheduler.reschedule_job(SYNC_JOB_ID, trigger=IntervalTrigger(minutes=minutes))
        _scheduler.reschedule_job(MAIL_JOB_ID, trigger=IntervalTrigger(minutes=minutes))


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
