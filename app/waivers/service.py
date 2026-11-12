"""Turn payable events into waivers, generate their PDFs, and record every status change."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.ctc.engine import compute_for_subcontract, order_key
from app.db.models import Payable, PayableEvent, Project, Subcontract, Waiver, WaiverEvent
from app.matching.normalize import state_from_project_number
from app.services import filestore, paper
from app.services.audit import audit
from app.services.reviews import upsert_review
from app.services.settings_store import get_setting
from app.waivers import pdf
from app.waivers.decision import Flags, PayableFacts, SubFacts, decide, waiver_amount
from app.waivers.mapper import exceptions_for, field_values
from app.waivers.registry import Registry, load_registry
from app.waivers.types import OPEN_STATUSES, WaiverStatus, WaiverType

log = structlog.get_logger()

SYSTEM = "system:waivers"
REVIEW_KINDS = ("no_contract", "over_billed", "retention_check", "hold")


def now() -> datetime:
    return datetime.now(timezone.utc)


@lru_cache
def registry() -> Registry:
    return load_registry()


def reload_registry() -> Registry:
    registry.cache_clear()
    return registry()


def project_state(project: Project) -> str | None:
    return (project.state or state_from_project_number(project.number) or "").upper() or None


def flags(db: Session) -> Flags:
    return Flags(conditional_partial_enabled=bool(get_setting(db, "conditional_partial_enabled")))


def facts(p: Payable) -> PayableFacts:
    return PayableFacts(match_status=p.match_status, approval_status=p.approval_status, is_paid=p.paid_date is not None,
                        is_retention_invoice=p.is_retention_invoice, invoice_no=p.invoice_no)


def log_event(db: Session, w: Waiver, actor: str, action: str, to_status: str | None = None,
              note: str | None = None, **details) -> None:
    db.add(WaiverEvent(waiver_id=w.id, actor=actor, action=action, from_status=w.status,
                       to_status=to_status or w.status, note=note, details=details or None))
    if to_status:
        w.status = to_status


# ---------------------------------------------------------------- event processing

def process_events(db: Session, stats=None) -> None:
    """Handle every unprocessed payable event on tracked projects. Safe to re-run."""
    q = (select(PayableEvent, Payable).join(Payable, PayableEvent.payable_id == Payable.id)
         .join(Project, Project.number == Payable.project_number)
         .where(PayableEvent.processed_at.is_(None), Project.is_tracked.is_(True),
                PayableEvent.kind.in_(("approved", "paid", "held")))
         .order_by(PayableEvent.id))
    for ev, p in db.execute(q).all():
        if ev.from_backfill:
            ev.processed_at = now()
            ev.outcome = "history: no waiver created"
            continue
        handle_event(db, ev, p, stats)
    db.flush()


def handle_event(db: Session, ev: PayableEvent, p: Payable, stats=None) -> None:
    if p.match_status == "direct_purchase":
        ev.processed_at = now()
        ev.outcome = "direct purchase: no waiver"
        return
    if p.match_status != "matched" or not p.subcontract_id:
        return  # stays unprocessed until someone maps the row in the Unmatched queue
    sc = db.get(Subcontract, p.subcontract_id)
    ctc = compute_for_subcontract(sc, as_of=p)
    kind = "approved" if ev.kind == "held" else ev.kind
    d = decide(kind, facts(p), ctc, SubFacts(has_retention=sc.has_retention), flags(db))
    if d.defer:
        return
    for r in d.reviews:
        upsert_review(db, r.kind, "payable", p.id, p.project_number,
                      f"{sc.vendor.name}, invoice {p.invoice_no or '-'}: {r.message}", True, stats)
    outcome = d.note
    uf = current_unconditional_final(db, sc) if d.waiver else None
    if uf is not None:
        # Paid in full is on file: nothing more is generated for this sub.
        ev.processed_at = now()
        ev.outcome = f"no waiver: unconditional final #{uf.id} is on file"
        return
    if d.waiver:
        w = create_waiver(db, p, sc, d.waiver, ev)
        outcome = f"{d.waiver.value} #{w.id} ({w.status})"
        if stats is not None:
            stats[f"waivers_{d.waiver.value.lower()}"] += 1
    ev.processed_at = now()
    ev.outcome = outcome or "no waiver"


def refresh_payable_reviews(db: Session) -> None:
    """Close Hold / retention reviews whose payable has moved on."""
    from app.db.models import ReviewItem

    for item in db.scalars(select(ReviewItem).where(ReviewItem.ref_type == "payable", ReviewItem.status == "open",
                                                    ReviewItem.kind.in_(("hold", "retention_check")))):
        p = db.get(Payable, item.ref_id)
        if p is None:
            continue
        still = (p.approval_status or "").lower() == "hold" if item.kind == "hold" else not p.is_retention_invoice
        if not still:
            upsert_review(db, item.kind, "payable", p.id, p.project_number, item.message, False)


# ---------------------------------------------------------------- creating waivers

PARTIALS = (WaiverType.CONDITIONAL_PARTIAL, WaiverType.UNCONDITIONAL_PARTIAL)


def paid_through(sc: Subcontract, p: Payable) -> Decimal:
    """Everything paid to the sub on this job up to and including this payment (in paid-date
    order). For a payment not made yet (a conditional partial), all payments so far plus this one."""
    def key(x: Payable):
        return (x.paid_date, order_key(x))
    total = p.amount or Decimal(0)
    for x in sc.payables:
        if x.id == p.id or x.deleted_in_source or not x.paid_date:
            continue
        if (x.approval_status or "").strip().lower() == "rejected":
            continue
        if p.paid_date is None or key(x) <= key(p):
            total += x.amount or Decimal(0)
    return total


def amount_for(wtype: WaiverType, sc: Subcontract, p: Payable) -> Decimal | None:
    """A partial waiver covers the total paid out to date, not just this invoice."""
    if wtype in PARTIALS:
        return paid_through(sc, p)
    return waiver_amount(wtype, p.amount)


def refresh_partial_amounts(db: Session, actor: str = SYSTEM) -> int:
    """Waivers not sent yet follow the paid-to-date total (payments can land in any order)."""
    from app.mail import outbound

    n = 0
    for w in db.scalars(select(Waiver).where(Waiver.type.in_([t.value for t in PARTIALS]), Waiver.supplier_id.is_(None),
                                             Waiver.status.in_([s.value for s in EDITABLE]))).all():
        if w.payable.is_manual:
            continue  # entered by hand: the amount is whatever was typed in
        new = paid_through(w.subcontract, w.payable)
        if w.amount == new:
            continue
        old, w.amount = w.amount, new
        log_event(db, w, actor, "amount_changed", note="total paid to date", old=str(old), new=str(new))
        if w.template_key:
            render_pdf(db, w, actor)
        outbound.rerender_request_draft(db, w)
        n += 1
    return n


# ---------------------------------------------------------------- monthly partials

def partials_monthly(db: Session) -> bool:
    return (get_setting(db, "partial_schedule") or "monthly") == "monthly"


def partial_send_day(db: Session) -> int:
    try:
        return min(max(int(get_setting(db, "partial_send_day") or 1), 1), 28)
    except (TypeError, ValueError):
        return 1


def next_partial_send(db: Session, today: date | None = None) -> date:
    """The next monthly send: this month's send day if it hasn't gone yet, else next month's."""
    from app.services.today import local_today
    today = today or local_today()
    day = partial_send_day(db)
    this = today.replace(day=day)
    if today < this or (today >= this and get_setting(db, "partials_released_month") != f"{today:%Y-%m}"):
        return max(this, today)
    return (this.replace(day=1) + timedelta(days=32)).replace(day=day)


def is_held(db: Session, w: Waiver) -> bool:
    """A partial waiting for the monthly send (no request email yet)."""
    actions = set(db.scalars(select(WaiverEvent.action).where(
        WaiverEvent.waiver_id == w.id, WaiverEvent.action.in_(("held_monthly", "released_monthly")))))
    return "held_monthly" in actions and "released_monthly" not in actions


def held_partials(db: Session) -> list[Waiver]:
    released = select(WaiverEvent.waiver_id).where(WaiverEvent.action == "released_monthly")
    held = select(WaiverEvent.waiver_id).where(WaiverEvent.action == "held_monthly")
    return list(db.scalars(select(Waiver).join(Subcontract).join(Project).where(
        Waiver.id.in_(held), Waiver.id.not_in(released), Waiver.status == WaiverStatus.DRAFT.value,
        Waiver.supplier_id.is_(None), Project.is_tracked.is_(True)).order_by(Waiver.id)))


def release_monthly_partials(db: Session, actor: str = SYSTEM, now_: bool = False,
                             today: date | None = None, sc_id: int | None = None) -> int:
    """On the send day (or now_, from "Send now" on Today), each sub with held partials gets one
    partial for everything paid so far (see up_to_date_partial); the held ones are cancelled.
    Returns how many request emails were drafted."""
    from app.db.models import EmailMessage
    from app.services.today import local_today

    today = today or local_today()
    month = f"{today:%Y-%m}"
    if not now_ and partials_monthly(db):
        if today.day < partial_send_day(db) or get_setting(db, "partials_released_month") == month:
            return 0
    by_sub: dict[int, list[Waiver]] = {}
    for w in held_partials(db):
        if sc_id is None or w.subcontract_id == sc_id:
            by_sub.setdefault(w.subcontract_id, []).append(w)
    n = 0
    for ws in by_sub.values():
        sc = ws[0].subcontract
        uf = current_unconditional_final(db, sc)
        if uf is not None:
            for old in ws:
                void(db, old, actor, f"Unconditional final #{uf.id} is on file")
            continue
        # One partial for everything paid so far, on the sub's last paid invoice. (Not just the
        # newest held one: those can be for older invoices entered after the fact.)
        try:
            w = up_to_date_partial(db, sc, actor)
        except UpToDate as exc:
            for old in ws:
                void(db, old, actor, str(exc))
            continue
        log_event(db, w, actor, "note", note=f"Monthly partial; covers {len(ws)} held payment{'s' if len(ws) != 1 else ''}")
        if db.scalar(select(EmailMessage.id).where(EmailMessage.waiver_id == w.id,
                                                   EmailMessage.kind == "waiver_request",
                                                   EmailMessage.status.in_(("draft", "queued", "failed")))):
            n += 1
    if not now_ and partials_monthly(db):
        from app.services.settings_store import set_setting
        set_setting(db, "partials_released_month", month, actor)
    return n


class UpToDate(ValueError):
    """The sub's latest partial already went out and still covers everything paid."""


def up_to_date_partial(db: Session, sc: Subcontract, actor: str) -> Waiver:
    """The partial for everything paid to the sub so far: on their last paid invoice, for the
    total paid through it. Waivers held for the monthly send and unsigned ones already sent for
    less are cancelled (they're covered). When the last invoice's partial already went out but
    the total has grown since (payments entered after the fact), a corrected one is made."""
    last = final_checks(sc)["last_paid"]
    if last is None:
        raise ValueError(f"{sc.vendor.name} has no paid invoice on this job yet.")
    total = paid_through(sc, last)
    gone_out = (WaiverStatus.SENT, WaiverStatus.RECEIVED, WaiverStatus.ACCEPTED, WaiverStatus.REJECTED)
    # Any partial already covering this total (e.g. a corrected one) counts.
    for x in db.scalars(select(Waiver).where(
            Waiver.subcontract_id == sc.id, Waiver.supplier_id.is_(None), Waiver.amount == total,
            Waiver.type.in_([t.value for t in PARTIALS]),
            Waiver.status.in_([st.value for st in (*gone_out, WaiverStatus.DRAFT)])).order_by(Waiver.id.desc())):
        if x.status in gone_out:
            when = f" on {x.sent_at:%b} {x.sent_at.day}" if x.sent_at else ""
            raise UpToDate(f"{sc.vendor.name}'s latest partial (#{x.id}, ${x.amount:,.2f}) already went out{when} "
                           "and covers everything paid so far.")
        if not is_held(db, x):
            return _finish_partial(db, sc, x, total, actor)  # already in Ready to send
    w = db.scalar(select(Waiver).where(Waiver.payable_id == last.id, Waiver.supplier_id.is_(None),
                                       Waiver.type.in_([t.value for t in PARTIALS])).order_by(Waiver.id.desc()))
    if w is not None and w.status in gone_out:
        if w.amount == total:
            when = f" on {w.sent_at:%b} {w.sent_at.day}" if w.sent_at else ""
            raise UpToDate(f"{sc.vendor.name}'s latest partial (#{w.id}, ${w.amount:,.2f}) already went out{when} "
                           f"and covers everything paid, through invoice {last.invoice_no or '(no number)'}.")
        before = w
        w = create_manual_waiver(
            db, sc, WaiverType.UNCONDITIONAL_PARTIAL, total, last.through_date or last.paid_date, actor,
            invoice_no=last.invoice_no or "paid to date", paid_date=last.paid_date, payment_amount=last.amount,
            note=f"corrected total paid to date (#{before.id} went out for ${before.amount or 0:,.2f})")
    elif w is None:
        # Not SYSTEM, so it isn't held for the monthly send.
        w = create_waiver(db, last, sc, WaiverType.UNCONDITIONAL_PARTIAL, None,
                          actor if actor != SYSTEM else "system:monthly")
    elif w.status == WaiverStatus.VOID:
        restore(db, w, actor)
    return _finish_partial(db, sc, w, total, actor)


def _finish_partial(db: Session, sc: Subcontract, w: Waiver, total: Decimal, actor: str) -> Waiver:
    """Bring w to the total, cancel what it covers, and draft its request email."""
    if w.amount != total and not w.payable.is_manual:
        log_event(db, w, actor, "amount_changed", note="total paid to date", old=str(w.amount), new=str(total))
        w.amount = total
        if w.template_key:
            render_pdf(db, w, actor)
        from app.mail import outbound
        outbound.rerender_request_draft(db, w)
    if is_held(db, w):
        log_event(db, w, actor, "released_monthly", note="Monthly partial for everything paid so far")
    for old in held_partials(db):
        if old.subcontract_id == sc.id and old.id != w.id:
            void(db, old, actor, f"Covered by partial #{w.id} (everything paid so far)")
    for old in db.scalars(select(Waiver).where(
            Waiver.subcontract_id == sc.id, Waiver.supplier_id.is_(None), Waiver.id != w.id,
            Waiver.type.in_([t.value for t in PARTIALS]),
            Waiver.status.in_((WaiverStatus.SENT.value, WaiverStatus.REJECTED.value)))).all():
        if (old.amount or 0) < total:
            void(db, old, actor, f"Replaced by partial #{w.id} for ${total:,.2f}, everything paid so far")
    if w.status == WaiverStatus.DRAFT:
        from app.mail import outbound
        outbound.queue_waiver_request(db, w, actor if actor != SYSTEM else "system:mail")
    return w


def latest_partial(db: Session, sc: Subcontract, actor: str) -> Waiver:
    """'Send latest partial' on a sub: one partial for everything paid so far, now, whatever the
    monthly schedule."""
    w = up_to_date_partial(db, sc, actor)
    log_event(db, w, actor, "prepared", note="latest partial requested by hand from the job page")
    return w


def create_waiver(db: Session, p: Payable, sc: Subcontract, wtype: WaiverType, ev: PayableEvent | None,
                  actor: str = SYSTEM) -> Waiver:
    existing = db.scalar(select(Waiver).where(Waiver.payable_id == p.id, Waiver.type == wtype.value,
                                              Waiver.supplier_id.is_(None)))
    if existing:
        return existing
    state = project_state(sc.project)
    w = Waiver(payable_id=p.id, subcontract_id=sc.id, type=wtype.value, state=state,
               amount=amount_for(wtype, sc, p), through_date=p.through_date,
               through_date_is_fallback=p.through_date_is_fallback,
               exceptions_text=exceptions_for(sc, as_of=p) if wtype == WaiverType.CONDITIONAL_FINAL else None,
               status=WaiverStatus.DRAFT.value, trigger_event_id=ev.id if ev else None,
               reviewer_email=get_setting(db, "default_reviewer_email"))
    db.add(w)
    db.flush()
    log_event(db, w, actor, "created", note=f"from {ev.kind} event" if ev else None)
    if wtype in PARTIALS and actor == SYSTEM and partials_monthly(db):
        d = next_partial_send(db)
        log_event(db, w, actor, "held_monthly", note=f"Goes out with the monthly partials on {d:%b} {d.day}")
    if w.exceptions_text:
        upsert_review(db, "final_exceptions", "waiver", w.id, sc.project.number,
                      f"Conditional final for {sc.vendor.name} lists exceptions: {w.exceptions_text}. "
                      "PM should confirm before it goes out.", True)
    attach_template_and_render(db, w, actor)
    if wtype in (WaiverType.CONDITIONAL_FINAL, WaiverType.UNCONDITIONAL_FINAL):
        from app.suppliers import service as suppliers
        gone = suppliers.missing(db, sc)
        if gone:
            log_event(db, w, actor, "note", note="Supplier waivers still missing: " + ", ".join(s.name for s in gone))
        suppliers.recheck_finals(db, sc)
    return w


def attach_template_and_render(db: Session, w: Waiver, actor: str) -> None:
    t = registry().lookup(w.state, w.type)
    if t is None:
        reg = registry()
        if reg.requires_statutory(w.state):
            reason = f"Statutory form required for {w.state}; add template."
            kind = "statutory_form_missing"
        else:
            reason = f"No template registered for {WaiverType(w.type).label}."
            kind = "template_missing"
        w.review_reason = reason
        if w.status != WaiverStatus.NEEDS_REVIEW:
            log_event(db, w, actor, "blocked", WaiverStatus.NEEDS_REVIEW.value, note=reason)
        upsert_review(db, kind, "waiver", w.id, w.subcontract.project.number,
                      f"{w.subcontract.vendor.name}, {WaiverType(w.type).label}: {reason}", True)
        return
    w.template_key = t.key
    w.review_reason = None
    render_pdf(db, w, actor)
    if w.status == WaiverStatus.NEEDS_REVIEW:
        log_event(db, w, actor, "unblocked", WaiverStatus.DRAFT.value, note=f"template {t.key}")
    for kind in ("statutory_form_missing", "template_missing"):
        upsert_review(db, kind, "waiver", w.id, None, "", False)
    if w.status == WaiverStatus.DRAFT:
        from app.mail import outbound
        outbound.queue_waiver_request(db, w)


def waiver_dir() -> Path:
    d = get_settings().data_dir / "waivers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def render_pdf(db: Session, w: Waiver, actor: str, today: date | None = None) -> Path:
    t = registry().templates[w.template_key]
    data = pdf.fill(t, field_values(w, today))
    w.pdf_version += 1
    folder = waiver_dir() / str(w.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{WaiverType(w.type).value.lower()}-v{w.pdf_version}.pdf"
    filestore.write(db, path, data)
    w.generated_pdf_path = str(path)
    log_event(db, w, actor, "pdf_generated", version=w.pdf_version, template=t.key)
    from app.mail import outbound
    outbound.refresh_draft_attachments(db, w)
    return path


def recheck_blocked(db: Session, actor: str = SYSTEM) -> int:
    """Retry waivers blocked on a missing template (after a template is added)."""
    n = 0
    for w in db.scalars(select(Waiver).where(Waiver.status == WaiverStatus.NEEDS_REVIEW.value)):
        attach_template_and_render(db, w, actor)
        n += w.status == WaiverStatus.DRAFT
    return n


# ---------------------------------------------------------------- edits and status changes

EDITABLE = {WaiverStatus.NEEDS_REVIEW, WaiverStatus.DRAFT}


def _require_editable(w: Waiver) -> None:
    if w.status not in EDITABLE:
        raise ValueError("This waiver has already gone out; void it and create a new one to change it.")


def set_through_date(db: Session, w: Waiver, d: date, actor: str) -> None:
    _require_editable(w)
    old = w.through_date
    w.through_date = d
    w.through_date_is_fallback = False
    w.payable.billed_through_date = d
    log_event(db, w, actor, "through_date_changed", old=str(old), new=str(d))
    audit(db, actor, "waiver.through_date", "waiver", w.id, old=str(old), new=str(d))
    if w.template_key:
        render_pdf(db, w, actor)


def set_exceptions(db: Session, w: Waiver, text: str, actor: str) -> None:
    _require_editable(w)
    old = w.exceptions_text
    w.exceptions_text = text.strip() or None
    log_event(db, w, actor, "exceptions_changed", old=old, new=w.exceptions_text)
    if w.template_key:
        render_pdf(db, w, actor)


def regenerate(db: Session, w: Waiver, actor: str) -> None:
    _require_editable(w)
    attach_template_and_render(db, w, actor)


def mark_sent(db: Session, w: Waiver, actor: str) -> None:
    """Phase 3 calls this when the email goes out. Exposed now for waivers sent by hand."""
    if w.status not in (WaiverStatus.DRAFT, WaiverStatus.REJECTED):
        raise ValueError("Only a ready waiver can be marked sent.")
    w.sent_at = w.sent_at or now()
    w.requested_at = w.requested_at or w.sent_at
    log_event(db, w, actor, "sent", WaiverStatus.SENT.value)


def mark_received(db: Session, w: Waiver, actor: str, filename: str, content: bytes) -> None:
    if w.status in (WaiverStatus.VOID, WaiverStatus.ACCEPTED):
        raise ValueError(f"Waiver is {w.status}.")
    folder = waiver_dir() / str(w.id)
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in Path(filename).name if ch.isalnum() or ch in "._- ") or "signed.pdf"
    stamp = f"{now():%Y%m%d%H%M%S}"
    original = folder / f"original-{stamp}-{safe}"
    filestore.write(db, original, content)
    # The copy on record is an 8.5 x 11 PDF; the file as sent is kept alongside it.
    path = original
    letter = paper.to_letter_pdf(content, safe)
    if letter is not None:
        path = folder / f"signed-{stamp}-{Path(safe).stem}.pdf"
        filestore.write(db, path, letter)
    w.signed_pdf_path = str(path)
    w.received_at = now()
    log_event(db, w, actor, "received", WaiverStatus.RECEIVED.value, file=path.name, original=str(original))


def undo_received(db: Session, w: Waiver, actor: str, note: str) -> None:
    """It wasn't a signed copy after all: back to waiting on the sub. No email is drafted."""
    if w.status != WaiverStatus.RECEIVED:
        raise ValueError("Only a waiver that came back can be put back to waiting.")
    email_id = (w.verification or {}).get("email_id")
    restore_requested_values(w)
    log_event(db, w, actor, "not_signed", WaiverStatus.SENT.value, note=note, file=w.signed_pdf_path)
    w.signed_pdf_path = None
    w.received_at = None
    w.verification = None
    audit(db, actor, "waiver.not_signed", "waiver", w.id, note=note)
    # Their email may have said something worth answering (it was read as a signed copy instead).
    from app.db.models import EmailMessage
    e = db.get(EmailMessage, email_id) if email_id else None
    if e is not None and e.direction == "in":
        from app.mail import outbound
        outbound.log_email(db, e, actor, "not_filed", reason=note)  # never filed from this email again
        from app.mail import replies
        words = replies.own_words(e)
        if words.strip():
            replies._respond(db, w, e, words, e.vendor_id, w.subcontract.vendor.name)


def accept(db: Session, w: Waiver, actor: str, note: str = "") -> None:
    if w.status != WaiverStatus.RECEIVED:
        raise ValueError("Only a received waiver can be accepted.")
    w.verified_at = now()
    log_event(db, w, actor, "accepted", WaiverStatus.ACCEPTED.value, note=note or None)
    audit(db, actor, "waiver.accepted", "waiver", w.id)


def reject(db: Session, w: Waiver, actor: str, reason: str) -> None:
    if w.status != WaiverStatus.RECEIVED:
        raise ValueError("Only a received waiver can be rejected.")
    restore_requested_values(w)  # the correction asks for what we asked for
    log_event(db, w, actor, "rejected", WaiverStatus.REJECTED.value, note=reason)
    audit(db, actor, "waiver.rejected", "waiver", w.id, reason=reason)


def void(db: Session, w: Waiver, actor: str, reason: str, notify_sub: bool = False):
    """Cancel a waiver that's no longer valid. Unsent emails for it are discarded and no more
    reminders go out. If it was already sent, a "please disregard" reply can be drafted.
    Returns that draft, if one was made."""
    from app.db.models import EmailMessage
    from app.mail import outbound

    if w.status == WaiverStatus.VOID:
        return None
    was_sent = w.sent_at is not None
    log_event(db, w, actor, "voided", WaiverStatus.VOID.value, note=reason)
    for e in db.scalars(select(EmailMessage).where(EmailMessage.waiver_id == w.id,
                                                   EmailMessage.status.in_(("draft", "queued", "failed")))):
        e.status = "discarded"
    audit(db, actor, "waiver.voided", "waiver", w.id, reason=reason)
    for kind in ("statutory_form_missing", "template_missing", "final_exceptions"):
        upsert_review(db, kind, "waiver", w.id, None, "", False)
    root = outbound.thread_root(db, w) if was_sent and notify_sub else None
    if root is not None:
        return outbound.draft_reply(db, root, "cancelled", actor=actor, everyone=True)
    return None


def restore(db: Session, w: Waiver, actor: str) -> None:
    """Undo a cancel: the waiver goes back to a draft with a fresh PDF and request email."""
    if w.status != WaiverStatus.VOID:
        raise ValueError("Only a cancelled waiver can be restored.")
    w.sent_at = w.requested_at = w.received_at = w.verified_at = None
    w.reminder_count, w.last_reminder_at, w.escalated_at = 0, None, None
    log_event(db, w, actor, "restored", WaiverStatus.DRAFT.value)
    audit(db, actor, "waiver.restored", "waiver", w.id)
    attach_template_and_render(db, w, actor)
    if w.status == WaiverStatus.DRAFT:
        from app.mail import outbound
        outbound.queue_waiver_request(db, w, actor, fresh=True)


# ---------------------------------------------------------------- reporting helpers

def expected_waiver_types(db: Session, p: Payable) -> list[WaiverType]:
    """What the rules say this payable should have had, given its current state. Used by backfill."""
    if p.match_status != "matched" or not p.subcontract_id:
        return []
    sc = db.get(Subcontract, p.subcontract_id)
    ctc = compute_for_subcontract(sc, as_of=p)
    out = []
    for kind in ("approved", "paid"):
        if kind == "paid" and not p.paid_date:
            continue
        if kind == "approved" and (p.approval_status or "").lower() != "approved":
            continue
        d = decide(kind, facts(p), ctc, SubFacts(has_retention=sc.has_retention), flags(db))
        if d.waiver:
            out.append(d.waiver)
    return out


def sub_waiver_summary(db: Session) -> dict[int, dict]:
    """Per subcontract: open waiver count and the status of the last waiver received."""
    out: dict[int, dict] = {}
    for w in db.scalars(select(Waiver).order_by(Waiver.id)):
        s = out.setdefault(w.subcontract_id, {"open": 0, "last_received": ""})
        if w.status in OPEN_STATUSES:
            s["open"] += 1
        if w.received_at:
            s["last_received"] = f"{WaiverType(w.type).label} {w.status} {w.received_at:%b %-d, %Y}"
    return out


def reprocess_payable(db: Session, p: Payable, actor: str) -> None:
    """Re-run the decision for a payable's events that didn't produce a waiver, e.g. after
    someone marks it as the retention invoice or maps it to a subcontract."""
    made = {w.trigger_event_id for w in db.scalars(select(Waiver).where(Waiver.payable_id == p.id))}
    for ev in db.scalars(select(PayableEvent).where(PayableEvent.payable_id == p.id).order_by(PayableEvent.id)):
        if ev.from_backfill or ev.id in made:
            continue
        ev.processed_at = None
        ev.outcome = None
        handle_event(db, ev, p)
    refresh_payable_reviews(db)
    audit(db, actor, "payable.reprocessed", "payable", p.source_row_id)


def reject_and_reply(db: Session, w: Waiver, actor: str, reason: str):
    """Reject a returned copy and draft the "needs correction" reply in the same thread."""
    from app.db.models import EmailMessage
    from app.mail import outbound

    reject(db, w, actor, reason)
    original = db.scalar(select(EmailMessage).where(EmailMessage.waiver_id == w.id, EmailMessage.direction == "in",
                                                    EmailMessage.found_in.is_(None))
                         .order_by(EmailMessage.id.desc())) or outbound.thread_root(db, w)
    if original is None and w.supplier_id and (w.verification or {}).get("email_id"):
        original = db.get(EmailMessage, w.verification["email_id"])  # the sub's email it came in on
    if original is None:
        return None
    if w.supplier_id:
        reason = f"{reason} (the waiver from {w.supplier.name})"
    e = outbound.draft_reply(db, original, "correction", values={"reason": reason}, actor=actor, everyone=True)
    outbound.maybe_auto_send(db, e)
    return e


def accept_and_ack(db: Session, w: Waiver, actor: str, note: str = ""):
    """Accept, and if the optional "received" reply is turned on, draft it."""
    from app.db.models import EmailMessage
    from app.mail import outbound

    accept(db, w, actor, note)
    if not get_setting(db, "send_received_reply"):
        return None
    original = db.scalar(select(EmailMessage).where(EmailMessage.waiver_id == w.id, EmailMessage.direction == "in",
                                                    EmailMessage.found_in.is_(None))
                         .order_by(EmailMessage.id.desc()))
    if original is None:
        return None
    e = outbound.draft_reply(db, original, "received", actor=actor, everyone=True)
    outbound.maybe_auto_send(db, e)
    return e


# ---------------------------------------------------------------- past paid invoices

@dataclass
class PastInvoice:
    payable: Payable
    sc: Subcontract | None
    wtype: WaiverType | None  # what the rules give for "paid", at that point in time
    existing: Waiver | None   # a waiver of that type already exists
    reason: str               # why it can't be prepared, when it can't

    @property
    def can_prepare(self) -> bool:
        return self.wtype is not None and self.existing is None


def past_paid_invoices(db: Session, project: str = "", vendor_id: int | None = None,
                       paid_from: date | None = None, paid_to: date | None = None,
                       only_missing: bool = True) -> list[PastInvoice]:
    """Paid invoices on tracked jobs, with the waiver each one should have. Invoices that were
    already paid when a job was first tracked are kept as history and get no waiver on their
    own; this is where a person picks the ones that should."""
    q = (select(Payable).join(Project, Project.number == Payable.project_number)
         .where(Project.is_tracked.is_(True), Payable.paid_date.is_not(None), Payable.deleted_in_source.is_(False))
         .order_by(Payable.paid_date.desc(), Payable.id.desc()))
    if project:
        q = q.where(Payable.project_number == project)
    if vendor_id:
        q = q.where(Payable.vendor_id == vendor_id)
    if paid_from:
        q = q.where(Payable.paid_date >= paid_from)
    if paid_to:
        q = q.where(Payable.paid_date <= paid_to)
    out = []
    for p in db.scalars(q.limit(1000)):
        if p.match_status == "direct_purchase":
            continue
        if p.match_status != "matched" or not p.subcontract_id:
            out.append(PastInvoice(p, None, None, None, "not matched to a subcontract yet (Queues → Unmatched AP)"))
            continue
        sc = db.get(Subcontract, p.subcontract_id)
        d = decide("paid", facts(p), compute_for_subcontract(sc, as_of=p),
                   SubFacts(has_retention=sc.has_retention), flags(db))
        existing = None
        if d.waiver:
            existing = db.scalar(select(Waiver).where(Waiver.payable_id == p.id, Waiver.type == d.waiver.value,
                                                      Waiver.supplier_id.is_(None)))
        reason = "" if d.waiver else (d.reviews[0].message if d.reviews else d.note or "no waiver for this one")
        out.append(PastInvoice(p, sc, d.waiver, existing, reason))
    if only_missing:
        out = [x for x in out if x.existing is None]
    return out


def prepare_past(db: Session, payable_ids: list[int], actor: str) -> list[Waiver]:
    """Create waivers (and their request drafts in the Outbox) for the picked paid invoices."""
    made = []
    wanted = set(payable_ids)
    for item in past_paid_invoices(db, only_missing=True):
        if item.payable.id not in wanted or not item.can_prepare:
            continue
        p = item.payable
        ev = db.scalar(select(PayableEvent).where(PayableEvent.payable_id == p.id, PayableEvent.kind == "paid"))
        w = create_waiver(db, p, item.sc, item.wtype, ev, actor)
        log_event(db, w, actor, "prepared", note=f"past invoice paid {p.paid_date:%b %-d, %Y}")
        if ev is not None:
            ev.processed_at = ev.processed_at or now()
            ev.outcome = f"{item.wtype.value} #{w.id} (prepared by {actor})"
        audit(db, actor, "waiver.prepared_past", "payable", p.source_row_id, waiver_id=w.id, type=item.wtype.value)
        made.append(w)
    db.flush()
    return made


# ---------------------------------------------------------------- signed waivers per job

SIGNED_STATUSES = (WaiverStatus.RECEIVED.value, WaiverStatus.ACCEPTED.value)


def best_signed_waiver(db: Session, sc: Subcontract) -> Waiver | None:
    """The waiver that counts for a sub on a job: a signed unconditional final, else a signed
    conditional final, else the most recent signed partial (latest through date)."""
    signed = db.scalars(select(Waiver).where(Waiver.subcontract_id == sc.id, Waiver.supplier_id.is_(None),
                                             Waiver.status.in_(SIGNED_STATUSES),
                                             Waiver.signed_pdf_path.is_not(None))).all()

    def recent(w: Waiver):
        return (w.through_date or date.min, w.payable.paid_date or date.min, w.received_at or now(), w.id)
    for t in (WaiverType.UNCONDITIONAL_FINAL, WaiverType.CONDITIONAL_FINAL):
        of_type = [w for w in signed if w.type == t.value]
        if of_type:
            return max(of_type, key=recent)
    partials = [w for w in signed if w.type in [x.value for x in PARTIALS]]
    return max(partials, key=recent) if partials else None


def job_waiver_rows(db: Session, project: Project) -> list[dict]:
    """Per sub on the job: their latest invoice and the signed waiver that counts."""
    rows = []
    subs = db.scalars(select(Subcontract).where(Subcontract.project_id == project.id,
                                                Subcontract.deleted_in_source.is_(False))).all()
    for sc in sorted(subs, key=lambda s: s.vendor.name.lower()):
        pays = [p for p in sc.payables if not p.deleted_in_source
                and (p.approval_status or "").strip().lower() != "rejected"]
        latest = max(pays, key=order_key) if pays else None
        from app.suppliers import service as suppliers
        sups = [{"supplier": s, "waiver": suppliers.best_waiver(db, s), "words": suppliers.status_words(db, s)}
                for s in suppliers.active(db, sc)]
        in_progress = db.scalars(select(Waiver).where(
            Waiver.subcontract_id == sc.id, Waiver.supplier_id.is_(None),
            Waiver.status.in_(("needs_review", "draft", "sent", "rejected", "received"))).order_by(Waiver.id)).all()
        rows.append({"sc": sc, "invoice": latest, "waiver": best_signed_waiver(db, sc), "suppliers": sups,
                     "major": suppliers.is_major_trade(db, sc),
                     "in_progress": [{"w": w, "held": is_held(db, w)} for w in in_progress]})
    return rows


def combined_signed_pdf(db: Session, waivers: list[Waiver]) -> bytes:
    """One PDF with every waiver's signed copy, each starting on its own page. Scans sent as
    images become a page each."""
    import io

    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    def page_with(draw) -> PdfReader:
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=letter)
        draw(c)
        c.showPage()
        c.save()
        return PdfReader(io.BytesIO(buf.getvalue()))

    out = PdfWriter()
    for w in waivers:
        data = filestore.read(db, w.signed_pdf_path)
        who = f"{w.supplier.name} (supplier to {w.subcontract.vendor.name})" if w.supplier_id else w.subcontract.vendor.name
        label = f"{who}: {WaiverType(w.type).label} waiver #{w.id}"
        reader = None
        if data and data[:5] == b"%PDF-":
            try:
                reader = PdfReader(io.BytesIO(data))
                _ = len(reader.pages)
            except Exception:
                reader = None
        elif data:
            try:
                img = ImageReader(io.BytesIO(data))
                iw, ih = img.getSize()
                pw, ph = letter
                scale = min((pw - 72) / iw, (ph - 72) / ih)
                reader = page_with(lambda c: c.drawImage(img, (pw - iw * scale) / 2, (ph - ih * scale) / 2,
                                                         iw * scale, ih * scale))
            except Exception:
                reader = None
        if reader is None:
            reader = page_with(lambda c: (c.setFont("Helvetica", 12),
                                          c.drawString(72, 700, f"{label}: the signed copy couldn't be included."),
                                          c.drawString(72, 680, "Open it from the waiver's page in the app.")))
        start = len(out.pages)
        for page in reader.pages:
            out.add_page(page)
        out.add_outline_item(label, start)
    buf = io.BytesIO()
    out.write(buf)
    return buf.getvalue()


def _cover_page(project: Project, entries: list[dict]) -> bytes:
    """Page 1 of a job's waiver download: who's included and who's missing."""
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=9, leading=11)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=48, rightMargin=48, topMargin=48, bottomMargin=48,
                            title=f"Lien waivers {project.number}")
    today = now().astimezone(__import__("zoneinfo").ZoneInfo(get_settings().timezone)).date()
    story = [Paragraph(f"Lien waivers: {escape(project.number)}", styles["Title"]),
             Paragraph(escape(project.name or ""), styles["Heading3"]),
             Paragraph(escape(project.address or ""), styles["BodyText"]),
             Paragraph(f"Generated {today:%b} {today.day}, {today.year}", small), Spacer(1, 12)]
    data = [[Paragraph("<b>Company</b>", small), Paragraph("<b>Waiver included</b>", small)]]
    for e in entries:
        name = ("&nbsp;&nbsp;&nbsp;&nbsp;" if e.get("supplier") else "") + escape(e["name"])
        if e.get("supplier"):
            name += f" <font size=7 color='#667085'>(supplier to {escape(e['parent'])})</font>"
        w = e["waiver"]
        if w is None:
            what = "<b>No signed waiver yet</b>"
        else:
            bits = [WaiverType(w.type).label]
            if w.amount is not None:
                bits.append(f"${w.amount:,.2f}")
            if w.through_date and not WaiverType(w.type).is_final:
                bits.append(f"through {w.through_date:%b} {w.through_date.day}, {w.through_date.year}")
            if w.received_at:
                bits.append(f"received {w.received_at:%b} {w.received_at.day}, {w.received_at.year}")
            what = escape(", ".join(bits))
        data.append([Paragraph(name, small), Paragraph(what, small)])
    if len(data) == 1:
        data.append([Paragraph("No subcontracts on this job.", small), ""])
    t = Table(data, colWidths=[230, 286], repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d0d5dd")),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f4f7")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t)
    doc.build(story)
    return buf.getvalue()


def job_packet_entries(db: Session, project: Project, include_suppliers: bool = False) -> list[dict]:
    """Cover-page rows in download order: subs alphabetically (suppliers under each, when asked)."""
    entries = []
    for r in job_waiver_rows(db, project):
        entries.append({"name": r["sc"].vendor.name, "waiver": r["waiver"], "supplier": False})
        if include_suppliers and r["sc"].track_suppliers:
            for s in r["suppliers"]:
                entries.append({"name": s["supplier"].name, "waiver": s["waiver"], "supplier": True,
                                "parent": r["sc"].vendor.name})
    return entries


def job_waiver_packet(db: Session, project: Project, include_suppliers: bool = False) -> bytes:
    """A cover page listing everyone (and who's missing), then each signed waiver from a new page."""
    import io

    from pypdf import PdfReader, PdfWriter

    entries = job_packet_entries(db, project, include_suppliers)
    out = PdfWriter()
    out.append(PdfReader(io.BytesIO(_cover_page(project, entries))), outline_item="Cover: who's included")
    waivers = [e["waiver"] for e in entries if e["waiver"] is not None]
    if waivers:
        # combined_signed_pdf bookmarks each waiver; append keeps those bookmarks.
        out.append(PdfReader(io.BytesIO(combined_signed_pdf(db, waivers))))
    buf = io.BytesIO()
    out.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- unconditional final by hand

def current_unconditional_final(db: Session, sc: Subcontract) -> Waiver | None:
    """The sub's own unconditional final on this job, if one exists (cancelled ones don't count)."""
    return db.scalar(select(Waiver).where(Waiver.subcontract_id == sc.id, Waiver.supplier_id.is_(None),
                                          Waiver.type == WaiverType.UNCONDITIONAL_FINAL.value,
                                          Waiver.status != WaiverStatus.VOID.value).order_by(Waiver.id.desc()))


def final_checks(sc: Subcontract) -> dict:
    """What a person should know before sending an unconditional final ("paid in full")."""
    ctc = compute_for_subcontract(sc)
    pays = [p for p in sc.payables if not p.deleted_in_source and (p.approval_status or "").strip().lower() != "rejected"]
    paid = [p for p in pays if p.paid_date]
    return {"ctc": ctc.cost_to_complete, "unpaid": [p for p in pays if not p.paid_date],
            "last_paid": max(paid, key=lambda p: (p.paid_date, order_key(p))) if paid else None}


def create_manual_waiver(db: Session, sc: Subcontract, wtype: WaiverType, amount: Decimal, through: date,
                         actor: str, invoice_no: str | None = None, paid_date: date | None = None,
                         note: str | None = None, payment_amount: Decimal | None = None) -> Waiver:
    """A waiver entered by hand, for something Smartsheet doesn't have (a payment that was never
    entered, a corrected amount). It hangs off its own stand-in invoice that is never synced or
    counted: marked removed-from-source, so cost to complete and paid-to-date ignore it, and the
    amount typed in is kept as is. Its request email is drafted like any other waiver's."""
    from sqlalchemy import func

    if wtype == WaiverType.UNCONDITIONAL_FINAL:
        amount = None  # an unconditional final says "paid in full"; it carries no amount
    elif amount is None or amount <= 0:
        raise ValueError("Enter the waiver amount.")
    if through is None:
        raise ValueError("Enter the through date.")
    if wtype in (WaiverType.UNCONDITIONAL_PARTIAL, WaiverType.UNCONDITIONAL_FINAL) and paid_date is None:
        raise ValueError("An unconditional waiver is for money already paid: enter the paid date.")
    if wtype in PARTIALS and not (invoice_no or "").strip():
        raise ValueError("Enter the invoice number the payment was for (it goes in the email).")
    lowest = db.scalar(select(func.min(Payable.source_row_id))) or 0
    p = Payable(source_row_id=min(lowest, 0) - 1, project_number=sc.project.number, vendor_id=sc.vendor_id,
                subcontract_id=sc.id, match_status="matched", vendor_raw="(entered by hand)",
                invoice_no=(invoice_no or "").strip() or None, amount=payment_amount or amount, date_received=through,
                billed_through_date=through, approval_status="Approved", paid_date=paid_date,
                material_sub_other="Sub", deleted_in_source=True)
    db.add(p)
    db.flush()
    w = Waiver(payable_id=p.id, subcontract_id=sc.id, type=wtype.value, state=project_state(sc.project),
               amount=amount, through_date=through, through_date_is_fallback=False,
               status=WaiverStatus.DRAFT.value, reviewer_email=get_setting(db, "default_reviewer_email"))
    db.add(w)
    db.flush()
    log_event(db, w, actor, "created", note="entered by hand" + (f": {note.strip()}" if note and note.strip() else ""))
    if wtype == WaiverType.UNCONDITIONAL_FINAL:
        # Paid in full: unsent waivers the app made for this sub aren't needed any more.
        for old in db.scalars(select(Waiver).where(
                Waiver.subcontract_id == sc.id, Waiver.supplier_id.is_(None), Waiver.id != w.id,
                Waiver.status.in_((WaiverStatus.DRAFT.value, WaiverStatus.NEEDS_REVIEW.value)))).all():
            if not old.payable.is_manual:
                void(db, old, actor, f"Unconditional final #{w.id} was added by hand")
    audit(db, actor, "waiver.manual", "subcontract", sc.id, waiver_id=w.id, type=wtype.value, amount=str(amount))
    attach_template_and_render(db, w, actor)
    return w


def original_file(db: Session, w: Waiver) -> str | None:
    """The signed copy exactly as it was sent (before it was made into a Letter PDF)."""
    ev = db.scalar(select(WaiverEvent).where(WaiverEvent.waiver_id == w.id, WaiverEvent.action == "received")
                   .order_by(WaiverEvent.id.desc()))
    return ((ev.details or {}).get("original") if ev else None) or w.signed_pdf_path


def straighten(db: Session, w: Waiver, turn, actor: str = SYSTEM) -> bool:
    """The automatic check said the page reads sideways or upside down: re-make the Letter PDF
    turned the right way. Photos whose phone already recorded the orientation are left alone
    (that's applied anyway, and turning again would overshoot)."""
    try:
        turn = int(turn or 0) % 360
    except (TypeError, ValueError):
        return False
    if turn not in (90, 180, 270) or not w.signed_pdf_path:
        return False
    src = original_file(db, w)
    data = filestore.read(db, src) if src else None
    if not data or (not paper.is_pdf(data) and _phone_oriented(data)):
        return False
    letter = paper.to_letter_pdf(data, Path(src).name, rotate=turn)
    if letter is None:
        return False
    path = Path(w.signed_pdf_path).with_name(f"{Path(w.signed_pdf_path).stem}-upright.pdf")
    filestore.write(db, path, letter)
    w.signed_pdf_path = str(path)
    log_event(db, w, actor, "straightened", note=f"turned {turn}° so it reads upright", file=path.name)
    return True


def _phone_oriented(data: bytes) -> bool:
    try:
        import io

        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return im.getexif().get(274, 1) not in (1, None)
    except Exception:  # noqa: BLE001
        return False


def letter_size_all(db: Session, limit: int = 50) -> int:
    """Catch up signed copies filed before they were made into 8.5 x 11 PDFs."""
    n = 0
    for w in db.scalars(select(Waiver).where(Waiver.signed_pdf_path.is_not(None),
                                             Waiver.status.in_((WaiverStatus.RECEIVED.value,
                                                                WaiverStatus.ACCEPTED.value)))).all():
        v = dict(w.verification or {})
        if v.get("letter"):
            continue
        data = filestore.read(db, w.signed_pdf_path)
        if data and not paper.is_letter(data):
            letter = paper.to_letter_pdf(data, Path(w.signed_pdf_path).name)
            if letter is not None:
                old = w.signed_pdf_path
                path = Path(old).with_name(f"{Path(old).stem}-letter.pdf")
                filestore.write(db, path, letter)
                w.signed_pdf_path = str(path)
                log_event(db, w, SYSTEM, "letter_size", note="made into an 8.5 x 11 PDF", original=old)
                n += 1
        v["letter"] = True
        w.verification = v
        if n >= limit:
            break
    return n


def restore_requested_values(w: Waiver) -> None:
    """Undo apply_signed_values: back to what we asked for (the copy was sent back, or wasn't
    the signed copy after all)."""
    req = (w.verification or {}).get("requested") or {}
    if "amount" in req:
        w.amount = Decimal(req["amount"]) if req["amount"] is not None else None
    if "through_date" in req:
        w.through_date = date.fromisoformat(req["through_date"]) if req["through_date"] else None


def apply_signed_values(db: Session, w: Waiver, actor: str = SYSTEM) -> bool:
    """Record what the signed copy actually says. The automatic check reads the amount and
    through date off the document; when they differ from what we asked for (the sub signed a
    corrected or more recent waiver), the waiver on file takes the document's values, and the
    review note says what we had asked for. Done once per signed copy. Returns whether it changed."""
    from app.mail.verify import _to_decimal

    v = dict(w.verification or {})
    found = v.get("found") or {}
    if v.get("signed_values_applied") or not found or w.supplier_id:
        return False
    if w.status not in (WaiverStatus.RECEIVED, WaiverStatus.ACCEPTED):
        return False
    if not found.get("is_lien_waiver") or not found.get("appears_signed") or found.get("is_supplier_waiver"):
        return False
    final = WaiverType(w.type).is_final
    ftype = found.get("waiver_type") or "unknown"
    if ftype != "unknown" and ftype.endswith("final") != final:
        return False  # a different kind of waiver: leave it to the reviewer
    amount = _to_decimal(found.get("amount"))
    through = None
    if found.get("through_date") and not final:
        try:
            through = date.fromisoformat(found["through_date"])
        except ValueError:
            through = None
    if w.type == WaiverType.UNCONDITIONAL_FINAL.value:
        amount = None  # carries no amount
    changes = {}
    if amount is not None and amount > 0 and amount != w.amount:
        changes["amount"] = (w.amount, amount)
    if through is not None and through != w.through_date:
        changes["through_date"] = (w.through_date, through)
    v["signed_values_applied"] = True
    if changes:
        asked = []
        if "amount" in changes:
            old, new = changes["amount"]
            asked.append(f"${old:,.2f}" if old is not None else "no amount")
            w.amount = new
        if "through_date" in changes:
            old, new = changes["through_date"]
            asked.append(f"through {old:%b} {old.day}, {old:%Y}" if old else "no through date")
            w.through_date, w.through_date_is_fallback = new, False
        says = (f"${w.amount:,.2f}" if w.amount is not None else "") + \
               (f" through {w.through_date:%b} {w.through_date.day}, {w.through_date:%Y}" if w.through_date else "")
        lower = "amount" in changes and changes["amount"][0] is not None and changes["amount"][1] < changes["amount"][0]
        v["requested"] = {k: (str(o) if o is not None else None) for k, (o, _) in changes.items()}
        v["note"] = (f"Recorded from the signed copy: {says.strip()}. We had asked for {' '.join(asked)}."
                     + (" That's less than we asked for, so check it before accepting." if lower else ""))
        # Mismatches with the old request no longer apply; anything else still does.
        v["problems"] = [x for x in v.get("problems") or []
                         if not x.startswith("the amount should be") and not x.startswith("the date should be")]
        if v.get("status") in ("issues", "looks_good"):
            v["status"] = "issues" if v["problems"] else "looks_good"
        log_event(db, w, actor, "signed_values", note=v["note"],
                  **{k: [str(o) if o is not None else None, str(n)] for k, (o, n) in changes.items()})
    w.verification = v
    return bool(changes)


def apply_signed_values_all(db: Session) -> int:
    """Catch up signed copies filed before their values were recorded."""
    n = 0
    for w in db.scalars(select(Waiver).where(Waiver.supplier_id.is_(None), Waiver.status.in_(
            (WaiverStatus.RECEIVED.value, WaiverStatus.ACCEPTED.value)))).all():
        n += apply_signed_values(db, w)
    return n


def accept_by_hand(db: Session, w: Waiver, actor: str, filename: str, content: bytes) -> None:
    """A waiver entered by hand with its signed copy: accepted on the spot. A person has it in
    hand, so there's no review, no automatic check and nothing that blocks it (a missing form
    template doesn't matter once it's signed). It stays the one on file until a newer signed
    waiver comes in (best_signed_waiver picks by through date)."""
    mark_received(db, w, actor, filename, content)
    w.review_reason = None
    w.verification = {"status": "entered_by_hand", "note": "Signed copy added by hand; not checked."}
    for kind in ("statutory_form_missing", "template_missing", "final_exceptions"):
        upsert_review(db, kind, "waiver", w.id, None, "", False)
    accept(db, w, actor, note="Signed copy added by hand")


def prepare_unconditional_final(db: Session, sc: Subcontract, actor: str) -> Waiver:
    """Create the sub's unconditional final against its most recent paid invoice. Its request
    email is drafted like any other waiver's."""
    existing = current_unconditional_final(db, sc)
    if existing is not None:
        raise ValueError(f"{sc.vendor.name} already has an unconditional final (#{existing.id}, {existing.status}).")
    last = final_checks(sc)["last_paid"]
    if last is None:
        raise ValueError(f"{sc.vendor.name} has no paid invoice on this job yet.")
    # A cancelled one on the same invoice is brought back rather than duplicated.
    void = db.scalar(select(Waiver).where(Waiver.payable_id == last.id, Waiver.supplier_id.is_(None),
                                          Waiver.type == WaiverType.UNCONDITIONAL_FINAL.value))
    if void is not None:
        restore(db, void, actor)
        w = void
    else:
        w = create_waiver(db, last, sc, WaiverType.UNCONDITIONAL_FINAL, None, actor)
    log_event(db, w, actor, "prepared", note="unconditional final requested by hand from the job page")
    audit(db, actor, "waiver.final_by_hand", "subcontract", sc.id, waiver_id=w.id)
    return w
