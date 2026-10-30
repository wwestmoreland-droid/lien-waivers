"""What the Today page shows: everything that needs a person, in four sections.

Read-only: this module only gathers and words things. Every button on Today posts to an
existing route, which calls the existing service code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    ChangeOrder,
    Contact,
    EmailMessage,
    Payable,
    Project,
    ReviewItem,
    Subcontract,
    Waiver,
    WaiverEvent,
)
from app.services import filestore
from app.services.settings_store import get_setting
from app.waivers.types import WaiverStatus, WaiverType

EDITORS = ("Admin", "AP")
UNMATCHED = ("unmatched_vendor", "no_subcontract", "ambiguous")
OUTBOX = ("draft", "queued", "failed")


def local_today() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def _local(dt: datetime) -> date:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(get_settings().timezone)).date()


def _short(d: date | None) -> str:
    return f"{d.month}/{d.day}" if d else ""


# ---------------------------------------------------------------- 1. ready to send

@dataclass
class ToSend:
    email: EmailMessage
    waiver: Waiver | None
    note: str = ""
    problem: str = ""   # why it can't go out; shown instead of Send
    fix_url: str = ""

    @property
    def sendable(self) -> bool:
        return not self.problem


def ready_to_send(db: Session) -> list[ToSend]:
    """Unsent emails: waiver requests first, then reminders, corrections and other drafts."""
    rows = db.scalars(select(EmailMessage).where(EmailMessage.direction == "out", EmailMessage.status.in_(OUTBOX))
                      .order_by(EmailMessage.kind != "waiver_request", EmailMessage.id)).all()
    out = []
    for e in rows:
        w = e.waiver
        if w is not None and not w.subcontract.project.is_tracked:
            continue
        if w is not None and (w.status == WaiverStatus.VOID
                              or (e.kind == "waiver_request" and w.status != WaiverStatus.DRAFT)
                              or (e.kind in ("reminder_1", "reminder_2", "escalation") and w.status != WaiverStatus.SENT)):
            continue  # moved on (e.g. marked sent by hand, or already back): nothing to send
        item = ToSend(email=e, waiver=w)
        notes = []
        if w is not None:
            p = w.payable
            if p.paid_date:
                notes.append(f"Paid {_short(p.paid_date)}")
            if w.exceptions_text:
                n = len([x for x in w.exceptions_text.split(";") if x.strip()])
                notes.append(f"{n} open item{'s' if n != 1 else ''} listed as exceptions")
            if w.through_date_is_fallback and not WaiverType(w.type).is_final and e.kind == "waiver_request":
                notes.append("Through date is a fallback, check it")
        item.note = " · ".join(notes)
        if not e.to_addrs:
            item.problem = "No email address for this sub."
            item.fix_url = f"/subs/{w.subcontract_id}" if w else f"/email/{e.id}/edit"
        elif w is not None and e.kind in ("waiver_request", "reminder_1") and not filestore.has(db, w.generated_pdf_path):
            item.problem = "The waiver PDF is missing."
            item.fix_url = f"/waivers/{w.id}"
        elif e.status == "failed":
            item.problem = e.error or "The last try to send it failed."
            item.fix_url = f"/email/{e.id}/edit"
        elif e.status == "queued":
            item.problem = e.error or "Waiting for the mailbox to be reconnected."
            item.fix_url = "/settings/mailboxes"
        out.append(item)
    return out


# ---------------------------------------------------------------- 2. signed copies back

def can_review(user, w: Waiver) -> bool:
    if user.role in EDITORS:
        return True
    return user.role == "PM" and (w.subcontract.project.pm_email or "").lower() == user.email


def signed_back(db: Session) -> list[Waiver]:
    return list(db.scalars(select(Waiver).join(Subcontract).join(Project).where(
        Project.is_tracked.is_(True), Waiver.status == WaiverStatus.RECEIVED.value).order_by(Waiver.received_at)))


def check_words(w: Waiver, notary_required: bool = True) -> tuple[bool | None, str]:
    """(passed?, plain words) for the automatic check of a returned copy."""
    v = w.verification or {}
    if v.get("status") == "looks_good":
        parts = ["signed"] + (["notarized"] if notary_required else []) + ["amount and through date match"]
        return True, ", ".join(parts[:-1]) + ", " + parts[-1] + "."
    if v.get("status") == "issues":
        return False, "; ".join(v.get("problems") or ["something didn't match"]) + "."
    return None, v.get("reason") or "Not checked automatically. Look it over."


# ---------------------------------------------------------------- 3. quick questions

@dataclass
class Question:
    kind: str          # which buttons to show (see today.html)
    text: str          # one plain sentence (may contain <strong>, escaped parts are pre-escaped)
    blocks: bool       # holds up a waiver
    more_url: str = ""
    obj: object = None
    extra: dict = field(default_factory=dict)


def _vendor_label(p: Payable) -> str:
    return p.vendor_parsed or p.vendor_raw or "A vendor"


def quick_questions(db: Session, user) -> list[Question]:
    from markupsafe import escape as e

    tracked = select(Project.number).where(Project.is_tracked.is_(True))
    qs: list[Question] = []
    subs_by_project: dict[str, list[Subcontract]] = {}
    for sc in db.scalars(select(Subcontract).join(Project).where(Project.is_tracked.is_(True),
                                                                Subcontract.deleted_in_source.is_(False))):
        subs_by_project.setdefault(sc.project.number, []).append(sc)

    # Blocked waivers.
    for w in db.scalars(select(Waiver).join(Subcontract).join(Project).where(
            Project.is_tracked.is_(True), Waiver.status == WaiverStatus.NEEDS_REVIEW.value)):
        qs.append(Question("open", f"Waiver for <strong>{e(w.subcontract.vendor.name)}</strong> on {e(w.subcontract.project.number)} "
                                   f"is blocked: {e(w.review_reason or 'needs a look')}", True, f"/waivers/{w.id}"))

    # AP rows the app couldn't place.
    for p in db.scalars(select(Payable).where(Payable.match_status.in_(UNMATCHED), Payable.deleted_in_source.is_(False),
                                              Payable.project_number.in_(tracked))
                        .order_by(Payable.project_number, Payable.vendor_parsed)):
        subs = sorted(subs_by_project.get(p.project_number, []), key=lambda s: s.vendor.name.lower())
        name, amt = e(_vendor_label(p)), f"${p.amount or 0:,.2f}"
        if p.match_status == "no_subcontract":
            qs.append(Question("no_sub", f"<strong>{name}</strong> billed {amt} on {e(p.project_number)} with no subcontract. "
                                         "Material only?", True, "/queues#ap", p, {"subs": subs}))
        elif p.match_status == "unmatched_vendor":
            top = (p.match_candidates or [None])[0]
            if top:
                text = f"AP has '{name}' on {e(p.project_number)}. Is that <strong>{e(top['name'])}</strong>?"
            else:
                text = f"AP has '{name}' ({amt}) on {e(p.project_number)}, and no vendor by that name. Which sub is it?"
            qs.append(Question("vendor", text, True, "/queues#ap", p, {"top": top}))
        else:
            qs.append(Question("ambiguous", f"Two or more subcontracts for <strong>{name}</strong> on {e(p.project_number)}. "
                                            f"Which one is invoice {e(p.invoice_no or '')} ({amt}) for?", True, "/queues#ap",
                               p, {"subs": [s for s in subs if s.vendor_id == p.vendor_id] or subs}))

    # Internal change orders with no sub.
    for co in db.scalars(select(ChangeOrder).where(ChangeOrder.match_status.in_(UNMATCHED),
                                                   ChangeOrder.deleted_in_source.is_(False),
                                                   ChangeOrder.project_number.in_(tracked))):
        qs.append(Question("co", f"Change order '{e(co.description or 'no description')}' (${co.amount or 0:,.2f}) on "
                                 f"{e(co.project_number)} has no sub. Which sub?", False, "/queues#co", co,
                           {"subs": sorted(subs_by_project.get(co.project_number or "", []),
                                           key=lambda s: s.vendor.name.lower())}))

    # Review items.
    for r in db.scalars(select(ReviewItem).where(ReviewItem.status == "open", ReviewItem.project_number.in_(tracked))
                        .order_by(ReviewItem.created_at)):
        if r.kind in ("statutory_form_missing", "template_missing"):
            continue  # shown as a blocked waiver above
        project = db.scalar(select(Project).where(Project.number == r.project_number))
        pm = (project.pm_email or "").lower() if project else ""
        if r.assignee_email and pm and r.assignee_email.lower() == pm and user.email != pm and user.role in EDITORS:
            continue  # sent to the PM; it's theirs now
        if r.kind == "retention_check" and r.ref_type == "payable":
            p = db.get(Payable, r.ref_id)
            if p is None:
                continue
            who = p.subcontract.vendor.name if p.subcontract else _vendor_label(p)
            qs.append(Question("retention", f"<strong>{e(who)}</strong> hit $0 left on {e(p.project_number)}. Is invoice "
                                            f"{e(p.invoice_no or '')} (${p.amount or 0:,.2f}) their retention invoice?",
                               True, f"/subs/{p.subcontract_id}" if p.subcontract_id else "/queues#review", r,
                               {"payable": p}))
            continue
        url = {"subcontract": f"/subs/{r.ref_id}", "waiver": f"/waivers/{r.ref_id}",
               "email": f"/email/{r.ref_id}"}.get(r.ref_type, "/queues#review")
        if r.ref_type == "payable":
            p = db.get(Payable, r.ref_id)
            url = f"/subs/{p.subcontract_id}" if p and p.subcontract_id else "/queues#review"
        qs.append(Question("review", _review_sentence(r), r.kind in ("hold", "final_exceptions", "email_bounced",
                                                                     "missing_supplier_waivers"),
                           url, r))

    # Suppliers read from a sub's emailed list.
    from app.db.models import SupplierCandidate
    for c in db.scalars(select(SupplierCandidate).join(Subcontract).join(Project).where(
            SupplierCandidate.status == "pending", Project.is_tracked.is_(True),
            Subcontract.track_suppliers.is_(True)).order_by(SupplierCandidate.id)):
        bits = ", ".join(x for x in (c.supplies, f"~${c.approx_amount:,.0f}" if c.approx_amount else None) if x)
        qs.append(Question("candidate", f"{e(c.subcontract.vendor.name)} lists <strong>{e(c.name)}</strong>"
                                        f"{' (' + e(bits) + ')' if bits else ''}. Add as their supplier?", False,
                           f"/jobs/{c.subcontract.project.number}#waivers", c))

    # New contacts (Admin/AP only).
    if user.role in EDITORS:
        from app.contacts.signature import ROLE_NAMES
        for c in db.scalars(select(Contact).where(Contact.status == "new", Contact.is_active.is_(True),
                                                  Contact.vendor_id.is_not(None))
                            .order_by(Contact.last_seen_at.desc().nulls_last()).limit(25)):
            role = ROLE_NAMES.get(c.role or "", "a contact")
            qs.append(Question("contact", f"Use {e(c.email)}{' (' + e(c.name) + ')' if c.name else ''} as <strong>{e(role)}</strong> "
                                          f"for <strong>{e(c.vendor.name)}</strong>?", False, f"/contacts/{c.id}", c))

    # Waiver mail not tied to a waiver.
    for m in db.scalars(select(EmailMessage).where(EmailMessage.direction == "in", EmailMessage.waiver_id.is_(None),
                                                   EmailMessage.kind == "inbound")
                        .order_by(EmailMessage.id.desc()).limit(25)):
        where = f" (found in {e(m.found_in)})" if m.found_in else ""
        files = " with an attachment" if m.attachments else ""
        qs.append(Question("mail", f"Email from <strong>{e(m.from_name or m.from_addr)}</strong>{files}{where}, "
                                   f"not tied to a waiver: '{e(m.subject or '(no subject)')}'.", False, f"/email/{m.id}", m))

    qs.sort(key=lambda q: (not q.blocks, q.kind != "contact"))
    return qs


def _review_sentence(r: ReviewItem) -> str:
    from markupsafe import escape as e
    return str(e(r.message))


# ---------------------------------------------------------------- 4. waiting on subs

WEEKDAY = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _due(start: date, days: int) -> date:
    d = start + timedelta(days=days)
    while d.weekday() >= 5:  # reminders only go out on weekdays
        d += timedelta(days=1)
    return d


def _when(d: date, today: date) -> str:
    if d <= today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    if (d - today).days < 7:
        return WEEKDAY[d.weekday()]
    return f"{d:%b} {d.day}"


def next_step(db: Session, w: Waiver, today: date | None = None) -> str:
    today = today or local_today()
    if w.status == WaiverStatus.REJECTED:
        return "Waiting for a corrected copy."
    if not w.sent_at:
        return ""
    sent = _local(w.sent_at)
    days = sorted(int(d) for d in get_setting(db, "reminder_days"))[:2]
    esc = int(get_setting(db, "escalation_day"))
    count = w.reminder_count or 0
    labels = ["First reminder", "Second reminder"]
    for n, day in enumerate(days):
        if count <= n:
            return f"{labels[n]} {_when(_due(sent, day), today)}"
    if not w.escalated_at:
        return f"PM gets flagged {_when(_due(sent, esc), today)}"
    return "PM has been flagged."


def waiting_lists(db: Session) -> list[Subcontract]:
    """Supplier lists asked for and not back yet."""
    return list(db.scalars(select(Subcontract).join(Project).where(
        Project.is_tracked.is_(True), Subcontract.track_suppliers.is_(True), Subcontract.supplier_list_status == "requested",
        Subcontract.supplier_list_requested_at.is_not(None)).order_by(Subcontract.supplier_list_requested_at)))


def list_next_step(db: Session, sc: Subcontract, today: date | None = None) -> str:
    today = today or local_today()
    days = sorted(int(d) for d in get_setting(db, "reminder_days"))[:2]
    done = sc.supplier_list_reminders or 0
    if done < len(days):
        return f"{['First', 'Second'][done]} reminder {_when(_due(_local(sc.supplier_list_requested_at), days[done]), today)}"
    return "Both reminders sent."


def waiting_on_subs(db: Session) -> list[Waiver]:
    return list(db.scalars(select(Waiver).join(Subcontract).join(Project).where(
        Project.is_tracked.is_(True), Waiver.status.in_((WaiverStatus.SENT.value, WaiverStatus.REJECTED.value)))
        .order_by(Waiver.sent_at.asc().nulls_last(), Waiver.id)))


# ---------------------------------------------------------------- totals

def accepted_this_week(db: Session) -> int:
    today = local_today()
    monday = today - timedelta(days=today.weekday())
    start = datetime.combine(monday, datetime.min.time(), tzinfo=ZoneInfo(get_settings().timezone))
    return len(set(db.scalars(select(WaiverEvent.waiver_id).where(WaiverEvent.to_status == WaiverStatus.ACCEPTED.value,
                                                                  WaiverEvent.at >= start))))


def action_count(db: Session, user) -> int:
    """For the Today badge: sections 1 to 3."""
    send = len(ready_to_send(db)) if user.role in EDITORS else 0
    signed = len([w for w in signed_back(db) if can_review(user, w)])
    return send + signed + len(quick_questions(db, user))
