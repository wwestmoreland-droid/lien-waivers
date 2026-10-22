"""Entry point used by the scheduler, the Sync-now button and the CLI."""
from __future__ import annotations

import structlog

from app.db.session import session_scope
from app.services.locks import job_lock
from app.smartsheet.client import SheetSource, SmartsheetClient
from app.smartsheet.sync import run_sync

log = structlog.get_logger()

_source_factory = SmartsheetClient


def set_source_factory(factory) -> None:
    """Tests swap the Smartsheet client for a fake."""
    global _source_factory
    _source_factory = factory


def sync_now(trigger: str = "schedule", backfill_projects: set[str] | None = None,
             source: SheetSource | None = None) -> int:
    with job_lock("sync"):
        src = source or _source_factory()
        with session_scope() as db:
            run = run_sync(db, src, trigger=trigger, backfill_projects=backfill_projects)
            return run.id


def mail_now() -> dict:
    """Check every connected mailbox and retry queued sends."""
    from app.mail import inbound

    with job_lock("mail"):
        with session_scope() as db:
            return inbound.poll_all(db)


def reminders_now() -> dict:
    from app.mail import reminders

    with job_lock("reminders"):
        with session_scope() as db:
            return reminders.run(db)
