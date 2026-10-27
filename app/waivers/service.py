"""Turn payable events into waivers, generate their PDFs, and record every status change."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
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
from app.services import filestore
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
    for w in db.scalars(select(Waiver).where(Waiver.type.in_([t.value for t in PARTIALS]),
                                             Waiver.status.in_([s.value for s in EDITABLE]))).all():
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


def create_waiver(db: Session, p: Payable, sc: Subcontract, wtype: WaiverType, ev: PayableEvent | None,
                  actor: str = SYSTEM) -> Waiver:
    existing = db.scalar(select(Waiver).where(Waiver.payable_id == p.id, Waiver.type == wtype.value))
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
    if w.exceptions_text:
        upsert_review(db, "final_exceptions", "waiver", w.id, sc.project.number,
                      f"Conditional final for {sc.vendor.name} lists exceptions: {w.exceptions_text}. "
                      "PM should confirm before it goes out.", True)
    attach_template_and_render(db, w, actor)
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
    path = folder / f"signed-{now():%Y%m%d%H%M%S}-{safe}"
    filestore.write(db, path, content)
    w.signed_pdf_path = str(path)
    w.received_at = now()
    log_event(db, w, actor, "received", WaiverStatus.RECEIVED.value, file=path.name)


def accept(db: Session, w: Waiver, actor: str, note: str = "") -> None:
    if w.status != WaiverStatus.RECEIVED:
        raise ValueError("Only a received waiver can be accepted.")
    w.verified_at = now()
    log_event(db, w, actor, "accepted", WaiverStatus.ACCEPTED.value, note=note or None)
    audit(db, actor, "waiver.accepted", "waiver", w.id)


def reject(db: Session, w: Waiver, actor: str, reason: str) -> None:
    if w.status != WaiverStatus.RECEIVED:
        raise ValueError("Only a received waiver can be rejected.")
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
        return outbound.draft_reply(db, root, "cancelled", actor=actor)
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
    if original is None:
        return None
    e = outbound.draft_reply(db, original, "correction", values={"reason": reason}, actor=actor)
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
    e = outbound.draft_reply(db, original, "received", actor=actor)
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
            existing = db.scalar(select(Waiver).where(Waiver.payable_id == p.id, Waiver.type == d.waiver.value))
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
    signed = db.scalars(select(Waiver).where(Waiver.subcontract_id == sc.id, Waiver.status.in_(SIGNED_STATUSES),
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
        rows.append({"sc": sc, "invoice": latest, "waiver": best_signed_waiver(db, sc)})
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
        label = f"{w.subcontract.vendor.name}: {WaiverType(w.type).label} waiver #{w.id}"
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
