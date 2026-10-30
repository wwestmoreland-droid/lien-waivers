"""Outbound email: waiver requests, reminders, corrections and free-form mail.

Everything the app writes waits in the Outbox (status "draft") for a person to review and
send, unless that email type is switched to auto-send in Settings.
"""
from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Contact, EmailAttachment, EmailEvent, EmailMessage, Mailbox, Waiver
from app.mail import mailboxes, templates
from app.mail.provider import OutAttachment, Outgoing, TokenError
from app.services import filestore
from app.services.settings_store import get_setting
from app.waivers.types import WaiverStatus, WaiverType

log = structlog.get_logger()

KIND_LABELS = {
    "waiver_request": "Waiver request",
    "reminder_1": "Reminder",
    "reminder_2": "Second reminder",
    "escalation": "Escalation to PM",
    "correction": "Needs correction",
    "received": "Received",
    "supplier_list": "Supplier list request",
    "supplier_list_reminder": "Supplier list reminder",
    "cancelled": "Waiver cancelled",
    "free": "Email",
    "inbound": "Received email",
}

AUTO_SEND_KINDS = ("waiver_request", "reminder_1", "reminder_2", "escalation", "correction", "received",
                   "supplier_list", "supplier_list_reminder")

REQUEST_TEMPLATE = {
    WaiverType.UNCONDITIONAL_PARTIAL: "partial",
    WaiverType.CONDITIONAL_PARTIAL: "partial",
    WaiverType.CONDITIONAL_FINAL: "conditional_final",
    WaiverType.UNCONDITIONAL_FINAL: "unconditional_final",
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def log_email(db: Session, e: EmailMessage, actor: str, action: str, **details) -> None:
    db.add(EmailEvent(email_id=e.id, actor=actor, action=action, details=details or None))


def token_for(w: Waiver) -> str:
    return f"LW-{w.id}"


def _dedupe(addrs, exclude=()) -> list[str]:
    out, seen = [], {a.lower() for a in exclude if a}
    for a in addrs:
        if a and "@" in a and a.lower() not in seen:
            out.append(a.strip())
            seen.add(a.lower())
    return out


def waiver_email(sc) -> str | None:
    from app.contacts.service import waiver_email as chosen
    return chosen(sc)


def recipients_for(w: Waiver) -> tuple[list[str], list[str]]:
    sc = w.subcontract
    to = _dedupe([waiver_email(sc)])
    cc = _dedupe([sc.pm_email, sc.estimator_email], exclude=to)
    if not to and cc:
        to, cc = cc[:1], cc[1:]
    return to, cc


def _recipient_name(db: Session, sc) -> str | None:
    if sc.vendor.waiver_email:
        c = db.scalar(select(Contact).where(Contact.email == sc.vendor.waiver_email))
        return c.name if c else None
    return sc.billing_name


def merge_values(db: Session, w: Waiver, mb: Mailbox | None) -> dict:
    sc, p = w.subcontract, w.payable
    project = sc.project
    signature = (mb.signature or mb.display_name) if mb else ""
    return {
        "id": w.id,
        "first_name": templates.first_name(_recipient_name(db, sc)),
        "project_name": project.name or project.number,
        "amount": w.amount if w.amount is not None else p.amount,
        "payment_amount": p.amount,
        "invoice_no": p.invoice_no,
        "paid_date": p.paid_date,
        "through_date": w.through_date,
        "exceptions": w.exceptions_text,
        "signature": signature,
        "vendor": sc.vendor.name,
        "waiver_type": (f"lien waiver from {w.supplier.name}" if w.supplier_id
                        else WaiverType(w.type).label.lower() + " lien waiver"),
        "requested_date": w.sent_at or w.requested_at,
        "billing_name": sc.billing_name,
        "billing_email": waiver_email(sc),
        "billing_phone": sc.billing_phone,
        "pm_first_name": templates.first_name(project.pm_name),
        # Payment gating is warn-only (decision 4), so emails never say payment is held.
        "gate": False,
        # The sub collects its suppliers' waivers and sends them with its own (we never email suppliers).
        **_supplier_values(db, sc, w),
    }


def _supplier_values(db: Session, sc, w: Waiver | None = None) -> dict:
    """The sub collects its suppliers' waivers and sends them to us; we never chase suppliers.
    With a partial, they're for the previous pay application (supplier_prev_through)."""
    from app.suppliers import service as suppliers

    names = [s.name for s in suppliers.active(db, sc)]
    gone = [s.name for s in suppliers.missing(db, sc)]
    prev = previous_pay_app(sc, w.payable) if (w is not None and names) else None
    return {"suppliers": bool(names), "supplier_names": ", ".join(names),
            "supplier_prev": prev is not None, "supplier_prev_through": prev.through_date if prev else None,
            "supplier_prev_invoice": prev.invoice_no if prev else None,
            "missing_supplier_waivers": bool(gone), "missing_supplier_names": ", ".join(gone)}


def previous_pay_app(sc, p) -> object | None:
    """The sub's pay application paid before this one on the job (None for the first)."""
    from app.ctc.engine import order_key

    if p is None:
        return None
    earlier = [x for x in sc.payables if x.id != p.id and not x.deleted_in_source and x.paid_date
               and (x.approval_status or "").strip().lower() != "rejected"
               and (p.paid_date is None or (x.paid_date, order_key(x)) < (p.paid_date, order_key(p)))]
    return max(earlier, key=lambda x: (x.paid_date, order_key(x))) if earlier else None


def recipients_for_sub(sc) -> tuple[list[str], list[str]]:
    to = _dedupe([waiver_email(sc)])
    cc = _dedupe([sc.pm_email, sc.estimator_email], exclude=to)
    if not to and cc:
        to, cc = cc[:1], cc[1:]
    return to, cc


def queue_supplier_list(db: Session, sc, actor: str = "system:suppliers") -> EmailMessage:
    """The supplier-list request (template 09) to a major-trade sub. Waits in the Outbox unless
    supplier_list is set to auto-send."""
    project = sc.project
    mb = mailboxes.sender_for_project(db, project)
    values = {"id": sc.id, "first_name": templates.first_name(_recipient_name(db, sc)),
              "project_name": project.name or project.number,
              "supplier_min": f"${Decimal(str(get_setting(db, 'supplier_min'))):,.0f}",
              "signature": (mb.signature or mb.display_name) if mb else "", "gate": False}
    r = templates.render(db, "supplier_list", values)
    to, cc = recipients_for_sub(sc)
    e = _new_email(db, kind="supplier_list", project_number=project.number, vendor_id=sc.vendor_id,
                   mailbox_id=mb.id if mb else None, to_addrs=to, cc_addrs=cc, subject=r.subject, body_text=r.body,
                   token=f"SD-{sc.id}", created_by=actor,
                   error=None if to else "No billing or PM email on this subcontract. Add a recipient before sending.")
    maybe_auto_send(db, e)
    return e


def _attach_waiver_pdf(db: Session, e: EmailMessage, w: Waiver) -> None:
    for a in list(e.attachments):
        db.delete(a)
    db.flush()
    local = filestore.ensure(db, w.generated_pdf_path)
    if local is not None:
        name = f"{WaiverType(w.type).label.replace(' ', '_')}_Lien_Waiver_{w.subcontract.project.number}.pdf"
        db.add(EmailAttachment(email_id=e.id, filename=name, content_type="application/pdf",
                               size=local.stat().st_size, path=w.generated_pdf_path))
    db.flush()
    db.refresh(e)


def _new_email(db: Session, **kw) -> EmailMessage:
    e = EmailMessage(direction="out", status="draft", **kw)
    db.add(e)
    db.flush()
    log_email(db, e, kw.get("created_by") or "system:mail", "drafted", kind=kw.get("kind"))
    return e


def queue_waiver_request(db: Session, w: Waiver, actor: str = "system:mail", fresh: bool = False) -> EmailMessage | None:
    """Draft the request email for a waiver that's ready (status draft). Idempotent.
    fresh: a new request even if one was sent before (a cancelled waiver brought back)."""
    if w.status != WaiverStatus.DRAFT:
        return None
    unsent = ("draft", "queued", "failed")
    existing = db.scalar(select(EmailMessage).where(EmailMessage.waiver_id == w.id,
                                                    EmailMessage.kind == "waiver_request",
                                                    EmailMessage.status.in_(unsent) if fresh
                                                    else EmailMessage.status != "discarded"))
    if existing:
        return existing
    mb = mailboxes.sender_for_project(db, w.subcontract.project)
    r = templates.render(db, REQUEST_TEMPLATE[WaiverType(w.type)], merge_values(db, w, mb))
    to, cc = recipients_for(w)
    e = _new_email(db, kind="waiver_request", waiver_id=w.id, project_number=w.subcontract.project.number,
                   vendor_id=w.subcontract.vendor_id, mailbox_id=mb.id if mb else None, to_addrs=to, cc_addrs=cc,
                   subject=r.subject, body_text=r.body, token=token_for(w), created_by=actor,
                   error=None if to else "No billing or PM email on this subcontract. Add a recipient before sending.")
    _attach_waiver_pdf(db, e, w)
    maybe_auto_send(db, e)
    return e


def rerender_request_draft(db: Session, w: Waiver) -> None:
    """Rewrite an untouched request draft after the waiver changed. Drafts a person edited are left alone."""
    for e in db.scalars(select(EmailMessage).where(EmailMessage.waiver_id == w.id, EmailMessage.kind == "waiver_request",
                                                   EmailMessage.status == "draft")):
        if db.scalar(select(EmailEvent.id).where(EmailEvent.email_id == e.id, EmailEvent.action == "edited")):
            continue
        r = templates.render(db, REQUEST_TEMPLATE[WaiverType(w.type)], merge_values(db, w, e.mailbox))
        e.subject, e.body_text = r.subject, r.body


def refresh_draft_attachments(db: Session, w: Waiver) -> None:
    """After a waiver PDF is regenerated, point unsent drafts at the new file."""
    for e in db.scalars(select(EmailMessage).where(EmailMessage.waiver_id == w.id, EmailMessage.direction == "out",
                                                   EmailMessage.status.in_(("draft", "queued")),
                                                   EmailMessage.kind.in_(("waiver_request", "reminder_1")))):
        _attach_waiver_pdf(db, e, w)


def thread_root(db: Session, w: Waiver) -> EmailMessage | None:
    """The sent request email whose thread reminders and replies continue."""
    return db.scalar(select(EmailMessage).where(EmailMessage.waiver_id == w.id, EmailMessage.direction == "out",
                                                EmailMessage.kind == "waiver_request",
                                                EmailMessage.status == "sent").order_by(EmailMessage.id))


def draft_reply(db: Session, original: EmailMessage, kind: str, values: dict | None = None, body: str | None = None,
                actor: str = "system:mail", reply_all: bool = False, attach_waiver: bool = False) -> EmailMessage:
    """A reply in the same thread, from the mailbox that owns the thread."""
    w = original.waiver
    mb = original.mailbox or mailboxes.active_sender(db)
    if body is None:
        vals = merge_values(db, w, mb) if w else {"signature": (mb.signature or mb.display_name) if mb else ""}
        if original.direction == "in":
            vals["first_name"] = templates.first_name(original.from_name) or vals.get("first_name", "")
        vals.update(values or {})
        body = templates.render(db, kind_template(kind), vals).body
    if original.direction == "in":
        to = [original.from_addr]
        cc = _dedupe((original.to_addrs or []) + (original.cc_addrs or []),
                     exclude=to + ([mb.email] if mb else [])) if reply_all else []
    else:
        to, cc = list(original.to_addrs or []), list(original.cc_addrs or [])
    subject = original.subject or ""
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    refs = " ".join(x for x in (original.references, original.rfc_message_id) if x) or None
    e = _new_email(db, kind=kind, waiver_id=original.waiver_id, project_number=original.project_number,
                   vendor_id=original.vendor_id, mailbox_id=mb.id if mb else None, to_addrs=to, cc_addrs=cc,
                   subject=subject, body_text=body, reply_to_email_id=original.id,
                   provider_thread_id=original.provider_thread_id, in_reply_to=original.rfc_message_id,
                   references=refs, token=original.token, created_by=actor)
    if attach_waiver and w:
        _attach_waiver_pdf(db, e, w)
    return e


def kind_template(kind: str) -> str:
    return {"reminder_1": "reminder_1", "reminder_2": "reminder_2", "correction": "correction",
            "received": "received", "escalation": "escalation", "cancelled": "cancelled",
            "supplier_list_reminder": "supplier_list_reminder"}[kind]


def draft_escalation(db: Session, w: Waiver, actor: str = "system:mail") -> EmailMessage | None:
    project = w.subcontract.project
    to = _dedupe([project.pm_email])
    cc = _dedupe([project.estimator_email], exclude=to)
    if not to:
        to = _dedupe([get_setting(db, "default_reviewer_email")])
    mb = mailboxes.sender_for_project(db, project)
    r = templates.render(db, "escalation", merge_values(db, w, mb))
    return _new_email(db, kind="escalation", waiver_id=w.id, project_number=project.number,
                      vendor_id=w.subcontract.vendor_id, mailbox_id=mb.id if mb else None, to_addrs=to, cc_addrs=cc,
                      subject=r.subject, body_text=r.body, created_by=actor)


def draft_free(db: Session, actor: str, to: list[str], cc: list[str], subject: str, body: str,
               waiver_id: int | None = None) -> EmailMessage:
    w = db.get(Waiver, waiver_id) if waiver_id else None
    mb = mailboxes.sender_for_project(db, w.subcontract.project if w else None)
    return _new_email(db, kind="free", waiver_id=waiver_id, mailbox_id=mb.id if mb else None, to_addrs=to,
                      cc_addrs=cc, subject=subject, body_text=body, created_by=actor,
                      project_number=w.subcontract.project.number if w else None,
                      vendor_id=w.subcontract.vendor_id if w else None, token=token_for(w) if w else None)


# ---------------------------------------------------------------- sending

def maybe_auto_send(db: Session, e: EmailMessage) -> None:
    if (get_setting(db, "auto_send") or {}).get(e.kind) and e.to_addrs:
        send(db, e, "system:auto-send")


def send(db: Session, e: EmailMessage, actor: str) -> bool:
    """Send now. Returns False (and leaves it queued) when the mailbox can't send."""
    if e.status not in ("draft", "queued", "failed"):
        raise ValueError(f"This email is {e.status}.")
    if not e.to_addrs:
        raise ValueError("Add at least one recipient.")
    mb = e.mailbox
    if mb is None or not mb.can_send:
        fallback = mailboxes.active_sender(db) if not e.provider_thread_id else None
        if fallback and fallback.can_send:
            mb = fallback
            e.mailbox_id = mb.id
        else:
            e.status = "queued"
            e.error = ("No Sender mailbox is connected." if mb is None and fallback is None
                       else f"{(mb or fallback).email} needs to be reconnected. The email will go out once it is.")
            log_email(db, e, actor, "queued", reason=e.error)
            return False
    if e.waiver and e.kind in ("waiver_request", "reminder_1"):
        _attach_waiver_pdf(db, e, e.waiver)
    files = []
    for a in e.attachments:
        data = filestore.read(db, a.path)
        if data is None:
            # Never send a waiver request without its PDF (or any email missing a file it lists).
            e.status = "failed"
            e.error = f"The attachment {a.filename} is missing, so this wasn't sent. Regenerate the PDF, then send."
            log_email(db, e, actor, "failed", error=e.error)
            return False
        files.append(OutAttachment(a.filename, a.content_type or mimetypes.guess_type(a.filename)[0]
                                   or "application/octet-stream", data))
    if e.waiver and e.kind == "waiver_request" and not files:
        e.status = "failed"
        e.error = "The waiver PDF is missing, so this wasn't sent. Regenerate the PDF on the waiver page, then send."
        log_email(db, e, actor, "failed", error=e.error)
        return False
    out = Outgoing(
        from_addr=mb.email, from_name=mb.display_name, to=list(e.to_addrs), cc=list(e.cc_addrs or []),
        subject=e.subject or "", body=e.body_text or "", thread_id=e.provider_thread_id,
        in_reply_to=e.in_reply_to, references=e.references, attachments=files,
    )
    provider = mailboxes.provider_for(mb)
    try:
        result = provider.send(out)
    except TokenError as exc:
        mailboxes.mark_broken(db, mb, str(exc))
        e.status = "queued"
        e.error = f"{mb.email} needs to be reconnected. The email will go out once it is."
        log_email(db, e, actor, "queued", reason=str(exc))
        return False
    except Exception as exc:  # network or API error: keep it, show it, let a person retry
        log.exception("mail.send_failed", email_id=e.id)
        e.status = "failed"
        e.error = str(exc)[:500]
        log_email(db, e, actor, "failed", error=e.error)
        return False
    e.status = "sent"
    e.error = None
    e.sent_at = now()
    e.from_addr = mb.email
    e.from_name = mb.display_name
    e.provider_message_id = result.message_id
    e.provider_thread_id = result.thread_id
    e.rfc_message_id = result.rfc_message_id
    log_email(db, e, actor, "sent", mailbox=mb.email)
    _label(db, mb, provider, result.thread_id)
    _after_send(db, e, actor)
    return True


def _label(db: Session, mb: Mailbox, provider, thread_id: str) -> None:
    try:
        if not mb.label_id:
            mb.label_id = provider.ensure_label(mailboxes.LABEL_NAME)
        provider.label_thread(thread_id, mb.label_id)
    except Exception:
        log.warning("mail.label_failed", mailbox=mb.email, thread=thread_id)


def _after_send(db: Session, e: EmailMessage, actor: str) -> None:
    from app.waivers import service as waivers

    if e.kind in ("supplier_list", "supplier_list_reminder") and (e.token or "").startswith("SD-"):
        from app.db.models import Subcontract
        sc = db.get(Subcontract, int(e.token.split("-", 1)[1]))
        if sc is not None and e.kind == "supplier_list":
            sc.supplier_list_requested_at = e.sent_at  # reminders count from when it actually went out
        elif sc is not None:
            sc.supplier_list_reminders = (sc.supplier_list_reminders or 0) + 1
        return
    w = e.waiver
    if not w:
        return
    if e.kind == "waiver_request" and w.status in (WaiverStatus.DRAFT, WaiverStatus.REJECTED):
        waivers.mark_sent(db, w, actor)
    elif e.kind in ("reminder_1", "reminder_2"):
        w.reminder_count = max(w.reminder_count or 0, 1 if e.kind == "reminder_1" else 2)
        w.last_reminder_at = e.sent_at
        waivers.log_event(db, w, actor, e.kind.replace("_", " ") + " sent")
    elif e.kind == "escalation":
        w.escalated_at = e.sent_at
        waivers.log_event(db, w, actor, "escalated", note=", ".join(e.to_addrs or []))
    elif e.kind == "correction":
        waivers.log_event(db, w, actor, "correction requested")


def send_queued(db: Session) -> int:
    n = 0
    for e in db.scalars(select(EmailMessage).where(EmailMessage.status == "queued")):
        try:
            n += send(db, e, "system:retry")
        except ValueError:
            continue
    return n


def ensure_request_drafts(db: Session) -> int:
    """Draft request emails for ready waivers that don't have one yet (e.g. created before email
    was set up). Runs after every sync; never resends anything."""
    from app.db.models import Project, Subcontract

    n = 0
    for w in db.scalars(select(Waiver).join(Subcontract).join(Project).where(
            Project.is_tracked.is_(True), Waiver.status == WaiverStatus.DRAFT.value)):
        # A waiver that's still a draft needs a request to send. If the last one was discarded,
        # draft it again (to stop it for good, cancel the waiver instead).
        if not db.scalar(select(EmailMessage.id).where(EmailMessage.waiver_id == w.id,
                                                       EmailMessage.kind == "waiver_request",
                                                       EmailMessage.status != "discarded")):
            queue_waiver_request(db, w, fresh=True)
            n += 1
    return n
