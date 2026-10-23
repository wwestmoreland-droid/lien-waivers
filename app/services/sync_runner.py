"""Entry point used by the scheduler, the Sync-now button and the CLI."""
from __future__ import annotations

import structlog

from app.db.session import session_scope
from app.services.locks import job_lock
from app.smartsheet.client import SheetSource, SmartsheetClient
from app.smartsheet.sync import run_sync

log = structlog.get_logger()

_source_factory = SmartsheetClient
_writer_factory = None


def set_source_factory(factory) -> None:
    """Tests swap the Smartsheet client for a fake."""
    global _source_factory
    _source_factory = factory


def set_writer_factory(factory) -> None:
    global _writer_factory
    _writer_factory = factory


def contact_writer():
    if _writer_factory is not None:
        return _writer_factory()
    from app.smartsheet.writer import ContactWriter
    return ContactWriter()


def sync_now(trigger: str = "schedule", backfill_projects: set[str] | None = None,
             source: SheetSource | None = None) -> int:
    with job_lock("sync"):
        src = source or _source_factory()
        with session_scope() as db:
            run = run_sync(db, src, trigger=trigger, backfill_projects=backfill_projects)
            run_id = run.id
        fill_contact_cells(run_id)
        return run_id


def fill_contact_cells(run_id: int | None = None) -> dict:
    """Fill blank contact cells in All Subcontracts; the result is added to the sync run's stats."""
    from app.contacts import writeback
    from app.db.models import SyncRun

    with session_scope() as db:
        try:
            stats = writeback.fill_blanks(db, contact_writer())
        except Exception as exc:
            log.exception("smartsheet.fill_failed")
            stats = {"rows": 0, "cells": 0, "errors": [f"{type(exc).__name__}: {exc}"[:300]]}
        run = db.get(SyncRun, run_id) if run_id else None
        if run is not None:
            run.stats = dict(run.stats or {}) | {"smartsheet_rows_filled": stats["rows"],
                                                 "smartsheet_cells_filled": stats["cells"]}
            if stats.get("errors"):
                run.errors = list(run.errors or []) + ["Smartsheet contact fill: " + e for e in stats["errors"]]
        return stats


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
    from app.db.models import ContactBackfill, Mailbox

    with session_scope() as db:
        # Mailboxes connected before contact logging existed get their overnight backfill too.
        for mb in db.scalars(select(Mailbox).where(Mailbox.status == "connected",
                                                   ~Mailbox.id.in_(select(ContactBackfill.mailbox_id)))):
            backfill.start(db, mb, "system")
        if not db.scalar(select(ContactBackfill.id).where(ContactBackfill.status.in_(backfill.ACTIVE)).limit(1)):
            return {}
    with job_lock("contacts_backfill"):
        with session_scope() as db:
            return backfill.run_due(db)


def recover_files_now() -> dict:
    from app.services.recover_files import recover

    with job_lock("recover_files"):
        with session_scope() as db:
            return recover(db)


def reminders_now() -> dict:
    from app.mail import reminders

    with job_lock("reminders"):
        with session_scope() as db:
            return reminders.run(db)
