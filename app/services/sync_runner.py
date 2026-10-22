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


def contacts_backfill_now() -> dict:
    """One quota-sized slice of each running contact backfill (called every minute)."""
    from sqlalchemy import select

    from app.contacts import backfill
    from app.db.models import ContactBackfill

    with session_scope() as db:
        if not db.scalar(select(ContactBackfill.id).where(ContactBackfill.status.in_(backfill.ACTIVE)).limit(1)):
            return {}
    with job_lock("contacts_backfill"):
        with session_scope() as db:
            return backfill.run_due(db)


def reminders_now() -> dict:
    from app.mail import reminders

    with job_lock("reminders"):
        with session_scope() as db:
            return reminders.run(db)
