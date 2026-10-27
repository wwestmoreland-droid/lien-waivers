"""One-time 12-month contact backfill per mailbox (section 6B).

Runs a slice every minute from the scheduler. Each slice stays under Gmail's per-mailbox
quota (about 6,000 units a minute): listing costs 5 units, a headers-only read 20, a full
read 60. Headers come first; a full message is read only for a new external sender whose
signature hasn't been parsed. Progress is saved after every page, so a restart resumes.
By default it only runs overnight (8 PM to 6 AM Central).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.contacts import ai
from app.contacts.harvest import Stats, harvest
from app.db.models import ContactBackfill, ContactMessage, Mailbox
from app.mail import mailboxes
from app.mail.provider import TokenError
from app.services.audit import audit

log = structlog.get_logger()

UNITS_PER_SLICE = 4500
COST_LIST, COST_META, COST_FULL = 5, 20, 60
PAGE_SIZE = 100
AI_CALLS_PER_SLICE = 10
NIGHT_START, NIGHT_END = 20, 6
ACTIVE = ("queued", "running")


def now() -> datetime:
    return datetime.now(timezone.utc)


def query_for(mb: Mailbox, since: date) -> str:
    q = f"after:{since:%Y/%m/%d} -in:chats -category:promotions -category:social -category:forums"
    if mb.role == "readonly" and mb.only_to:
        a = mb.only_to
        q += f" {{to:{a} cc:{a} deliveredto:{a}}}"
    return q


def is_night(at: datetime | None = None) -> bool:
    local = (at or now()).astimezone(ZoneInfo(get_settings().timezone))
    return local.hour >= NIGHT_START or local.hour < NIGHT_END


def start(db: Session, mb: Mailbox, actor: str, overnight_only: bool = True, months: int = 12) -> ContactBackfill:
    bf = db.scalar(select(ContactBackfill).where(ContactBackfill.mailbox_id == mb.id))
    if bf is None:
        bf = ContactBackfill(mailbox_id=mb.id, since=date.today())
        db.add(bf)
    bf.status, bf.overnight_only, bf.requested_by = "queued", overnight_only, actor
    bf.since = date.today() - timedelta(days=round(months * 30.5))
    bf.page_token, bf.estimate, bf.error, bf.finished_at = None, None, None, None
    bf.scanned = bf.skipped = bf.bodies_fetched = bf.contacts_new = 0
    bf.started_at = None
    audit(db, actor, "contacts.backfill_started", "mailbox", mb.email, overnight_only=overnight_only, months=months)
    db.flush()
    return bf


def set_status(db: Session, bf: ContactBackfill, action: str, actor: str) -> None:
    if action == "pause" and bf.status in ACTIVE:
        bf.status = "paused"
    elif action == "resume" and bf.status in ("paused", "error"):
        bf.status, bf.error = "queued", None
    elif action == "run_now":
        bf.overnight_only = False
        if bf.status in ("paused", "error"):
            bf.status, bf.error = "queued", None
    elif action == "overnight":
        bf.overnight_only = True
    audit(db, actor, f"contacts.backfill_{action}", "mailbox", str(bf.mailbox_id))


def run_due(db: Session, at: datetime | None = None, budget: int = UNITS_PER_SLICE) -> dict:
    done = {}
    for bf in db.scalars(select(ContactBackfill).where(ContactBackfill.status.in_(ACTIVE))).all():
        if bf.overnight_only and not is_night(at):
            continue
        mb = db.get(Mailbox, bf.mailbox_id)
        if mb is None or mb.status != "connected":
            continue
        try:
            done[mb.email] = run_slice(db, bf, mb, budget)
        except TokenError as exc:
            mailboxes.mark_broken(db, mb, str(exc))
            bf.status, bf.error = "paused", "The mailbox needs reconnecting; the backfill picks up where it stopped."
        except Exception as exc:  # keep going next minute; Gmail errors are usually brief
            log.exception("contacts.backfill_failed", mailbox=mb.email)
            bf.error = f"{type(exc).__name__}: {exc}"[:500]
        db.commit()
    return done


def run_slice(db: Session, bf: ContactBackfill, mb: Mailbox, budget: int = UNITS_PER_SLICE) -> int:
    """Process pages until this minute's quota is used. Returns units used."""
    provider = mailboxes.provider_for(mb)
    q = query_for(mb, bf.since)
    stats, ai_budget = Stats(), ai.AiBudget(AI_CALLS_PER_SLICE)
    used = 0
    if bf.status == "queued":
        bf.status, bf.started_at = "running", bf.started_at or now()
    while used + COST_LIST + COST_META <= budget:
        size = max(1, min(PAGE_SIZE, (budget - used - COST_LIST) // (COST_META + 10)))
        ids, nxt, estimate = provider.list_page(q, bf.page_token, size)
        used += COST_LIST
        if estimate and not bf.estimate:
            bf.estimate = estimate
        for mid in ids:
            bf.scanned += 1
            if db.scalar(select(ContactMessage.id).where(ContactMessage.mailbox_id == mb.id,
                                                         ContactMessage.provider_message_id == mid).limit(1)):
                continue
            meta = provider.get_metadata(mid)
            used += COST_META
            before = stats.bodies
            harvest(db, mb, meta, fetch_body=lambda mid=mid: provider.get_message(mid, attachments=False),
                    budget=ai_budget,
                    stats=stats)
            used += COST_FULL * (stats.bodies - before)
        bf.page_token = nxt
        bf.skipped += stats.skipped
        bf.bodies_fetched += stats.bodies
        bf.contacts_new += stats.new
        stats.skipped = stats.bodies = stats.new = 0
        bf.last_run_at, bf.error = now(), None
        db.commit()  # progress is saved after every page
        if not nxt:
            bf.status, bf.finished_at = "done", now()
            log.info("contacts.backfill_done", mailbox=mb.email, scanned=bf.scanned)
            break
    return used


def progress(bf: ContactBackfill | None) -> int | None:
    if bf is None:
        return None
    if bf.status == "done":
        return 100
    if not bf.estimate:
        return 0
    return min(99, round(100 * bf.scanned / max(bf.estimate, 1)))
