"""Daily reminders (8:00 AM Central): day 3 and day 7 to the sub, day 10 escalation to our PM.

Days count from when the request went out. Reminders are replies in the request's thread
with the waiver attached again. Like every other email, they wait in the Outbox unless
that type is set to auto-send.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import EmailMessage, Project, Subcontract, Waiver
from app.mail import outbound
from app.waivers.types import WaiverStatus

REMINDER_DAYS = (3, 7)
ESCALATION_DAY = 10


def _local_date(dt: datetime) -> date:
    tz = ZoneInfo(get_settings().timezone)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).date()


def _pending(db: Session, w: Waiver, kind: str) -> bool:
    return db.scalar(select(EmailMessage.id).where(EmailMessage.waiver_id == w.id, EmailMessage.kind == kind,
                                                   EmailMessage.status.in_(("draft", "queued", "sent")))) is not None


def run(db: Session, today: date | None = None) -> dict:
    today = today or datetime.now(ZoneInfo(get_settings().timezone)).date()
    stats = {"reminder_1": 0, "reminder_2": 0, "escalation": 0}
    waivers = db.scalars(select(Waiver).join(Subcontract).join(Project).where(
        Project.is_tracked.is_(True), Waiver.status == WaiverStatus.SENT.value, Waiver.sent_at.is_not(None)))
    for w in waivers:
        age = (today - _local_date(w.sent_at)).days
        root = outbound.thread_root(db, w)
        if age >= ESCALATION_DAY and not w.escalated_at and not _pending(db, w, "escalation"):
            e = outbound.draft_escalation(db, w)
            if e:
                outbound.maybe_auto_send(db, e)
                stats["escalation"] += 1
        if root is None:
            continue
        for n, day in enumerate(REMINDER_DAYS, start=1):
            kind = f"reminder_{n}"
            if age >= day and (w.reminder_count or 0) < n and not _pending(db, w, kind):
                # Only the latest reminder that's due, so a long-overdue waiver doesn't get two at once.
                if n < len(REMINDER_DAYS) and age >= REMINDER_DAYS[n]:
                    continue
                e = outbound.draft_reply(db, root, kind, attach_waiver=(n == 1))
                outbound.maybe_auto_send(db, e)
                stats[kind] += 1
    db.flush()
    return stats
