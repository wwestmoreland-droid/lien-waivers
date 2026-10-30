"""Daily reminders (8:00 AM Central, weekdays only). By default: day 7 and day 10 to the sub,
day 13 escalation to our PM (checks are mailed, so subs get time to receive them). The days
are set on Settings > Email. Days are calendar days from the request; anything that comes due
on a weekend goes out Monday morning.

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
from app.services.settings_store import get_setting
from app.waivers.types import WaiverStatus



def _local_date(dt: datetime) -> date:
    tz = ZoneInfo(get_settings().timezone)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).date()


def _pending(db: Session, w: Waiver, kind: str, since: datetime | None = None) -> bool:
    q = select(EmailMessage.id).where(EmailMessage.waiver_id == w.id, EmailMessage.kind == kind,
                                      EmailMessage.status.in_(("draft", "queued", "sent")))
    if since is not None:
        q = q.where(EmailMessage.created_at >= since)
    return db.scalar(q.limit(1)) is not None


def _clock_start(db: Session, w: Waiver) -> datetime:
    """Reminders count from the request, or, after a copy is sent back, from the send-back."""
    from app.db.models import WaiverEvent
    if w.status == WaiverStatus.REJECTED:
        ev = db.scalar(select(WaiverEvent).where(WaiverEvent.waiver_id == w.id, WaiverEvent.action == "rejected")
                       .order_by(WaiverEvent.id.desc()))
        if ev is not None:
            return ev.at
    return w.sent_at


def run(db: Session, today: date | None = None) -> dict:
    today = today or datetime.now(ZoneInfo(get_settings().timezone)).date()
    stats = {"reminder_1": 0, "reminder_2": 0, "escalation": 0, "supplier_list_reminder": 0}
    if today.weekday() >= 5:  # Saturday or Sunday
        return stats
    reminder_days = sorted(int(d) for d in get_setting(db, "reminder_days"))[:2]
    escalation_day = int(get_setting(db, "escalation_day"))
    waivers = db.scalars(select(Waiver).join(Subcontract).join(Project).where(
        Project.is_tracked.is_(True), Waiver.status.in_((WaiverStatus.SENT.value, WaiverStatus.REJECTED.value)),
        Waiver.sent_at.is_not(None), Waiver.supplier_id.is_(None)))
    for w in waivers:
        start = _clock_start(db, w)
        since = start if w.status == WaiverStatus.REJECTED else None
        age = (today - _local_date(start)).days
        root = outbound.thread_root(db, w)
        if age >= escalation_day and not w.escalated_at and not _pending(db, w, "escalation"):
            e = outbound.draft_escalation(db, w)
            if e:
                outbound.maybe_auto_send(db, e)
                stats["escalation"] += 1
        done = 0 if since else (w.reminder_count or 0)  # after a send-back the reminders start over
        for n, day in enumerate(reminder_days, start=1):
            kind = f"reminder_{n}"
            if age >= day and done < n and not _pending(db, w, kind, since):
                # Only the latest reminder that's due, so a long-overdue waiver doesn't get two at once.
                if n < len(reminder_days) and age >= reminder_days[n]:
                    continue
                if root is None:
                    # Marked sent by hand, so there's no email to reply to: send it as a new email.
                    e = outbound.draft_new_reminder(db, w, kind)
                else:
                    e = outbound.draft_reply(db, root, kind, attach_waiver=(n == 1), everyone=True)
                outbound.maybe_auto_send(db, e)
                stats[kind] += 1
    stats["supplier_list_reminder"] = _supplier_list_reminders(db, today, reminder_days)
    db.flush()
    return stats


def _supplier_list_reminders(db: Session, today: date, reminder_days: list[int]) -> int:
    """Same schedule for a supplier list that hasn't come back: a reminder in the request's thread."""
    from decimal import Decimal

    from app.mail import templates
    from app.suppliers.service import tracked_sql

    n = 0
    for sc in db.scalars(select(Subcontract).join(Project).where(
            Project.is_tracked.is_(True), tracked_sql(),
            Subcontract.supplier_list_status == "requested")):
        root = db.scalar(select(EmailMessage).where(EmailMessage.token == f"SD-{sc.id}", EmailMessage.kind == "supplier_list",
                                                    EmailMessage.status == "sent").order_by(EmailMessage.id))
        if root is None or not root.sent_at:
            continue
        age = (today - _local_date(root.sent_at)).days
        done = sc.supplier_list_reminders or 0
        due = [d for d in reminder_days if age >= d]
        pending = db.scalar(select(EmailMessage.id).where(EmailMessage.token == f"SD-{sc.id}",
                                                          EmailMessage.kind == "supplier_list_reminder",
                                                          EmailMessage.status.in_(("draft", "queued"))))
        if len(due) > done and not pending:
            values = {"first_name": templates.first_name(sc.billing_name), "project_name": sc.project.name or sc.project.number,
                      "supplier_min": f"${Decimal(str(get_setting(db, 'supplier_min'))):,.0f}"}
            e = outbound.draft_reply(db, root, "supplier_list_reminder", values=values, everyone=True)
            outbound.maybe_auto_send(db, e)
            n += 1
    return n
