"""Inbound email: poll mailboxes, keep waiver-related mail, match it, file attachments.

Matching order: Gmail thread -> [LW-123] token in the subject -> sender + project.
Anything unmatched lands in the Unmatched inbox. Read-only mailboxes are never changed.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    EmailAttachment,
    EmailMessage,
    Mailbox,
    Project,
    Subcontract,
    Vendor,
    Waiver,
)
from app.mail import mailboxes, outbound
from app.mail.provider import CursorExpired, Incoming, MessageGone, TokenError
from app.services import filestore
from app.services.reviews import upsert_review
from app.smartsheet.sync import PUBLIC_MAIL_DOMAINS
from app.waivers.types import WaiverStatus

log = structlog.get_logger()

TOKEN_RE = re.compile(r"\[(LW|SD)-(\d+)\]", re.I)
WAIVER_WORDS = re.compile(r"lien|waiver|release", re.I)
AUTO_REPLY_SUBJECT = re.compile(r"^(automatic reply|auto[- ]?reply|out of (the )?office|ooo\b|away from)", re.I)
BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster", re.I)
BOUNCE_SUBJECT = re.compile(r"delivery status notification|undeliverable|delivery has failed|returned mail|"
                            r"failure notice|could not be delivered", re.I)
INTERNAL_DOMAIN = "buildoutpros.com"


def now() -> datetime:
    return datetime.now(timezone.utc)


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def is_auto_reply(msg: Incoming) -> bool:
    h = msg.headers
    if h.get("auto-submitted", "no").lower() not in ("no", ""):
        return True
    if h.get("x-autoreply") or h.get("x-autorespond") or "auto_reply" in h.get("x-auto-response-suppress", "").lower():
        return True
    return bool(AUTO_REPLY_SUBJECT.search(msg.subject or ""))


def is_bounce(msg: Incoming) -> bool:
    return bool(BOUNCE_FROM.search(msg.from_addr)) or bool(BOUNCE_SUBJECT.search(msg.subject or ""))


def looks_like_waiver_file(name: str) -> bool:
    return name.lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff"))


# ---------------------------------------------------------------- vendor lookup

def vendor_for_address(db: Session, addr: str) -> Vendor | None:
    """Exact known address first, then a company domain known for the vendor. Our own people
    are never a vendor, even when Smartsheet lists them in a sub's PM column."""
    addr = addr.lower()
    if _domain(addr) == INTERNAL_DOMAIN:
        return None
    from sqlalchemy import or_
    sc = db.scalar(select(Subcontract).where(or_(
        Subcontract.billing_email == addr, Subcontract.pm_email == addr,
        Subcontract.estimator_email == addr, Subcontract.foreman_email == addr)))
    if sc:
        return sc.vendor
    v = db.scalar(select(Vendor).where(Vendor.billing_email == addr))
    if v:
        return v
    dom = _domain(addr)
    if not dom or dom in PUBLIC_MAIL_DOMAINS or dom == INTERNAL_DOMAIN:
        return None
    for v in db.scalars(select(Vendor).where(Vendor.email_domains.is_not(None))):
        if dom in (v.email_domains or []):
            return v
    return None


def open_waivers_for_vendor(db: Session, vendor_id: int) -> list[Waiver]:
    return list(db.scalars(
        select(Waiver).join(Subcontract).join(Project)
        .where(Subcontract.vendor_id == vendor_id, Project.is_tracked.is_(True), Waiver.supplier_id.is_(None),
               Waiver.status.in_((WaiverStatus.SENT.value, WaiverStatus.REJECTED.value)))))


# ---------------------------------------------------------------- polling

CATCH_UP_DAYS = 14


def poll_all(db: Session) -> dict:
    stats = {"mailboxes": 0, "new": 0, "kept": 0}
    boxes = db.scalars(select(Mailbox).where(Mailbox.status == "connected",
                                             Mailbox.role.in_(("sender", "listening", "readonly")))).all()
    for mb in boxes:
        stats["mailboxes"] += 1
        try:
            new, kept = poll(db, mb)
            stats["new"] += new
            stats["kept"] += kept
            mb.last_error = None
        except TokenError as exc:
            mailboxes.mark_broken(db, mb, str(exc))
        except Exception as exc:  # noqa: BLE001
            # One mailbox's problem never stops the others; it's shown on the Mailboxes page.
            log.exception("mail.poll_failed", mailbox=mb.email)
            db.rollback()
            mb = db.get(Mailbox, mb.id)
            mb.last_error = f"Last mail check failed: {type(exc).__name__}: {exc}"[:500]
        db.commit()
    try:
        stats["set_aside"] = tidy_inbox(db)
        db.commit()
    except Exception:
        log.exception("mail.tidy_failed")
        db.rollback()
    try:
        stats["filed"] = file_waiting_replies(db)
        db.commit()
    except Exception:
        log.exception("mail.file_waiting_failed")
        db.rollback()
    outbound.send_queued(db)
    db.commit()
    return stats


def tidy_inbox(db: Session) -> int:
    """Set aside stored mail that isn't about a waiver (kept under the older, wider rule): kind
    becomes "other", which the Inbox hides. Nothing is deleted; it's one click to see it."""
    n = 0
    for e in db.scalars(select(EmailMessage).where(EmailMessage.direction == "in", EmailMessage.kind == "inbound",
                                                   EmailMessage.waiver_id.is_(None),
                                                   EmailMessage.found_in.is_(None))).all():
        if e.from_addr and _domain(e.from_addr) == INTERNAL_DOMAIN:
            e.vendor_id = None
        mb = db.get(Mailbox, e.mailbox_id) if e.mailbox_id else None
        names = " ".join(a.filename for a in e.attachments)
        if TOKEN_RE.search(e.subject or "") or WAIVER_WORDS.search((e.subject or "") + " " + names):
            continue
        if mb is not None and e.provider_thread_id and db.scalar(select(EmailMessage.id).where(
                EmailMessage.mailbox_id == mb.id, EmailMessage.provider_thread_id == e.provider_thread_id,
                EmailMessage.direction == "out", EmailMessage.waiver_id.is_not(None)).limit(1)):
            continue  # a reply in one of our waiver threads
        if e.vendor_id and e.attachments and open_waivers_for_vendor(db, e.vendor_id):
            continue  # files from a sub that owes us a waiver: could be the signed copy
        e.kind = "other"
        n += 1
    return n


def file_waiting_replies(db: Session, days: int = 45) -> int:
    """Safety net: a reply tied to a waiver that's still waiting, with a PDF or image, gets
    filed as the signed copy even if it wasn't when the mail first came in. A reply whose
    attachments weren't saved has them downloaded again first."""
    from datetime import timedelta

    since = now() - timedelta(days=days)
    filed = 0
    rows = db.scalars(select(EmailMessage).join(Waiver, EmailMessage.waiver_id == Waiver.id).where(
        EmailMessage.direction == "in", EmailMessage.is_auto_reply.is_(False), EmailMessage.kind == "inbound",
        EmailMessage.found_in.is_(None), EmailMessage.received_at >= since,
        Waiver.status.in_((WaiverStatus.SENT.value, WaiverStatus.REJECTED.value)))
        .order_by(EmailMessage.id)).all()
    for e in rows:
        w = e.waiver
        if w.status not in (WaiverStatus.SENT, WaiverStatus.REJECTED):
            continue  # filed by an earlier reply in this loop
        if w.status == WaiverStatus.REJECTED and w.received_at and e.received_at and \
                _aware(e.received_at) <= _aware(w.received_at):
            continue  # the copy that was rejected; wait for a new one
        if not e.attachments:
            _refetch_attachments(db, e)
        if not any(a.is_pdf or a.is_image for a in e.attachments):
            continue
        with db.begin_nested():
            if receive_waiver_copy(db, w, e, actor="system:mail"):
                outbound.log_email(db, e, "system:mail", "filed_as_signed_copy", waiver_id=w.id)
                filed += 1
    return filed


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _refetch_attachments(db: Session, e: EmailMessage) -> None:
    mb = db.get(Mailbox, e.mailbox_id) if e.mailbox_id else None
    if mb is None or mb.status != "connected" or not e.provider_message_id:
        return
    try:
        msg = mailboxes.provider_for(mb).get_message(e.provider_message_id)
    except Exception as exc:
        log.warning("mail.refetch_failed", email_id=e.id, error=str(exc)[:200])
        return
    if not msg.attachments:
        return
    folder = get_settings().data_dir / "emails" / str(e.id)
    for i, a in enumerate(msg.attachments):
        safe = "".join(ch for ch in Path(a.filename).name if ch.isalnum() or ch in "._- ")[-150:] or "attachment"
        path = folder / f"{i}-{safe}"
        filestore.write(db, path, a.data)
        db.add(EmailAttachment(email_id=e.id, filename=a.filename[:500], content_type=(a.content_type or "")[:100] or None,
                               size=a.size, path=str(path)))
    db.flush()
    db.refresh(e)


MAX_RETRIES = 5


def poll(db: Session, mb: Mailbox) -> tuple[int, int]:
    """Read new mail. Each message is handled on its own: a deleted draft or one odd message
    never stops the rest, and the place in the mailbox is always saved."""
    provider = mailboxes.provider_for(mb)
    catch_up = mb.last_polled_at is None
    try:
        ids, cursor = provider.changes_since(mb.history_id)
    except CursorExpired:
        log.warning("mail.cursor_expired", mailbox=mb.email)
        _, cursor = provider.changes_since(None)
        ids, catch_up = [], True  # look back instead of skipping what we missed
    if catch_up:
        # After connecting (or a long gap): also look back, so nothing that arrived meanwhile
        # is missed. Already-stored messages are skipped below.
        ids = [i for i in provider.recent_ids(CATCH_UP_DAYS) if i not in ids] + ids
    from app.contacts import ai

    retries = dict(mb.retry_ids or {})
    ids = [i for i in retries if i not in ids] + ids
    budget = ai.AiBudget(10)
    kept = 0
    for n, mid in enumerate(ids, 1):
        if n % 25 == 0:
            db.commit()  # keep what's done if the app restarts partway through a long catch-up
        if db.scalar(select(EmailMessage.id).where(EmailMessage.mailbox_id == mb.id,
                                                   EmailMessage.provider_message_id == mid)):
            retries.pop(mid, None)
            continue
        try:
            with db.begin_nested():
                meta = provider.get_metadata(mid)
                full = provider.get_message(mid) if worth_reading(db, mb, meta) else None
                text = full
                if text is None and wanted_for_contacts(db, mb, meta):
                    # A vendor's own mail: read the text (no attachments) to keep their contact details current.
                    text = provider.get_message(mid, attachments=False)
                _log_contacts(db, mb, text or meta,
                              fetch_body=lambda mid=mid: provider.get_message(mid, attachments=False),
                              budget=budget)
                if full is not None and process(db, mb, full, provider):
                    kept += 1
            retries.pop(mid, None)
        except MessageGone:
            retries.pop(mid, None)  # deleted before we got to it (usually a draft)
        except TokenError:
            raise
        except Exception as exc:
            n = retries.get(mid, 0) + 1
            log.exception("mail.message_failed", mailbox=mb.email, message=mid, attempt=n)
            if n >= MAX_RETRIES:
                retries.pop(mid, None)
                mb.last_error = f"Skipped a message after {n} tries: {type(exc).__name__}: {exc}"[:500]
            else:
                retries[mid] = n
    mb.retry_ids = retries or None
    mb.history_id = cursor
    mb.last_polled_at = now()
    return len(ids), kept


def _log_contacts(db: Session, mb: Mailbox, msg: Incoming, fetch_body=None, budget=None) -> None:
    """Contact logging (section 6B) never gets in the way of handling the mail itself."""
    from app.contacts import ai
    from app.contacts.harvest import harvest

    try:
        with db.begin_nested():
            harvest(db, mb, msg, fetch_body=fetch_body, budget=budget or ai.AiBudget(3))
    except Exception:
        log.exception("contacts.harvest_failed", mailbox=mb.email, message=msg.message_id)


def wanted_for_contacts(db: Session, mb: Mailbox, meta: Incoming) -> bool:
    if mb.role == "readonly" and mb.only_to and mb.only_to.lower() not in meta.recipients_all:
        return False
    if "SENT" in meta.label_ids or meta.from_addr == mb.email:
        return False
    return vendor_for_address(db, meta.from_addr) is not None


def worth_reading(db: Session, mb: Mailbox, meta: Incoming) -> bool:
    """From the headers alone: could this message be about a waiver? Only those are downloaded
    in full, so a busy (or personal) mailbox stays quick to check."""
    readonly = mb.role == "readonly"
    if readonly and mb.only_to and mb.only_to.lower() not in meta.recipients_all:
        return False
    if TOKEN_RE.search(meta.subject or ""):
        return True
    if not readonly and _known_thread(db, mb, meta.thread_id) is not None:
        return True
    if "SENT" in meta.label_ids or meta.from_addr == mb.email:
        return False
    if not readonly and is_bounce(meta):
        return True
    vendor = vendor_for_address(db, meta.from_addr)
    if vendor is None or not _vendor_on_tracked_project(db, vendor.id):
        return False
    # Attachment names aren't in the headers, so a sub that owes us a waiver is read in full.
    return bool(WAIVER_WORDS.search(meta.subject or "")) or bool(open_waivers_for_vendor(db, vendor.id))


def _known_thread(db: Session, mb: Mailbox, thread_id: str) -> EmailMessage | None:
    return db.scalar(select(EmailMessage).where(EmailMessage.mailbox_id == mb.id,
                                                EmailMessage.provider_thread_id == thread_id)
                     .order_by(EmailMessage.waiver_id.is_(None), EmailMessage.id))


def process(db: Session, mb: Mailbox, msg: Incoming, provider=None) -> EmailMessage | None:
    """Store the message if it's waiver-related, match it, and act on it."""
    readonly = mb.role == "readonly"
    if readonly and mb.only_to and mb.only_to.lower() not in msg.recipients_all:
        return None  # e.g. personal mail in a mailbox that receives ap@ forwards

    known = None if readonly else _known_thread(db, mb, msg.thread_id)
    token = TOKEN_RE.search(msg.subject or "")

    if "SENT" in msg.label_ids or msg.from_addr == mb.email:
        if known is None:
            return None
        # Someone replied from Gmail in a Lien Waivers thread: keep it in the app's history.
        e = _store(db, mb, msg, direction="out", status="sent")
        e.kind, e.created_by = "free", "gmail"
        e.waiver_id, e.project_number, e.vendor_id = known.waiver_id, known.project_number, known.vendor_id
        return e

    vendor = vendor_for_address(db, msg.from_addr)
    bounce = is_bounce(msg)
    has_files = any(looks_like_waiver_file(a.filename) for a in msg.attachments)
    waiverish = bool(WAIVER_WORDS.search((msg.subject or "") + " " + " ".join(a.filename for a in msg.attachments)))
    tracked_vendor = vendor is not None and _vendor_on_tracked_project(db, vendor.id)

    if readonly:
        keep = bool(token) or (tracked_vendor and has_files and waiverish)
    else:
        # A sub's everyday mail (RFIs, drawings, invoices) stays in Gmail. Kept: waiver threads, [LW-]
        # tags, bounces, mail that talks about a waiver, and files from a sub that owes us one.
        keep = known is not None or bool(token) or bounce and _bounce_original(db, mb, msg) is not None \
            or (tracked_vendor and (waiverish or (has_files and bool(open_waivers_for_vendor(db, vendor.id)))))
    if not keep:
        return None

    e = _store(db, mb, msg, direction="in", status="received")
    e.vendor_id = vendor.id if vendor else None
    if readonly:
        e.found_in = mb.label
    e.is_auto_reply = is_auto_reply(msg)

    if bounce and not readonly:
        _handle_bounce(db, mb, msg, e)
        return e

    sc_list = _supplier_list_thread(db, known, token)
    if sc_list is not None and not e.is_auto_reply:
        # A reply to our supplier-list request: read the list into yes/no questions.
        from app.suppliers import service as suppliers
        e.kind, e.match_method = "supplier_list_reply", "thread" if known else "token"
        e.project_number, e.vendor_id = sc_list.project.number, sc_list.vendor_id
        if not readonly and provider is not None:
            outbound._label(db, mb, provider, msg.thread_id)
        suppliers.handle_list_reply(db, sc_list, e)
        outbound.log_email(db, e, "system:mail", "received", matched_by=e.match_method, found_in=e.found_in)
        return e

    w, method = _match(db, mb, msg, known, token, vendor)
    if w:
        e.waiver_id, e.match_method = w.id, method
        e.project_number = w.subcontract.project.number
        e.vendor_id = w.subcontract.vendor_id
    elif known is not None:
        e.project_number, e.vendor_id = known.project_number, known.vendor_id or e.vendor_id

    if not readonly and (w or known) and provider is not None:
        outbound._label(db, mb, provider, msg.thread_id)

    if w and not readonly and not e.is_auto_reply and e.attachments:
        receive_waiver_copy(db, w, e, actor="system:mail")
    outbound.log_email(db, e, "system:mail", "received", matched_by=method, found_in=e.found_in)
    return e


def _supplier_list_thread(db: Session, known: EmailMessage | None, token) -> Subcontract | None:
    ref = None
    if token and token.group(1).upper() == "SD":
        ref = token.group(2)
    elif known is not None and known.kind == "supplier_list" and (known.token or "").upper().startswith("SD-"):
        ref = known.token.split("-", 1)[1]
    return db.get(Subcontract, int(ref)) if ref and ref.isdigit() else None


def _vendor_on_tracked_project(db: Session, vendor_id: int) -> bool:
    return db.scalar(select(Subcontract.id).join(Project).where(Subcontract.vendor_id == vendor_id,
                                                                Project.is_tracked.is_(True)).limit(1)) is not None


def _match(db: Session, mb: Mailbox, msg: Incoming, known: EmailMessage | None, token, vendor: Vendor | None):
    if known is not None and known.waiver_id:
        return db.get(Waiver, known.waiver_id), "thread"
    if token and token.group(1).upper() == "LW":
        w = db.get(Waiver, int(token.group(2)))
        if w:
            return w, "token"
    if vendor is not None:
        candidates = open_waivers_for_vendor(db, vendor.id)
        if len(candidates) == 1:
            return candidates[0], "sender"
    return None, None


def _store(db: Session, mb: Mailbox, msg: Incoming, direction: str, status: str) -> EmailMessage:
    def cut(s, n):
        return s[:n] if s else s
    e = EmailMessage(
        mailbox_id=mb.id, direction=direction, kind="inbound" if direction == "in" else "free", status=status,
        provider_message_id=msg.message_id, provider_thread_id=msg.thread_id,
        rfc_message_id=cut(msg.rfc_message_id, 500), in_reply_to=cut(msg.in_reply_to, 500), references=msg.references,
        from_addr=cut(msg.from_addr, 255), from_name=cut(msg.from_name, 255),
        to_addrs=msg.to, cc_addrs=msg.cc, subject=cut(msg.subject, 1000), body_text=msg.body_text,
        received_at=msg.date or now(), sent_at=msg.date if direction == "out" else None,
    )
    m = TOKEN_RE.search(msg.subject or "")
    if m:
        e.token = f"{m.group(1).upper()}-{m.group(2)}"
    db.add(e)
    db.flush()
    folder = get_settings().data_dir / "emails" / str(e.id)
    for a in msg.attachments:
        folder.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch for ch in Path(a.filename).name if ch.isalnum() or ch in "._- ")[-150:] or "attachment"
        path = folder / safe
        i = 1
        while path.exists():
            path = folder / f"{i}-{safe}"
            i += 1
        filestore.write(db, path, a.data)
        db.add(EmailAttachment(email_id=e.id, filename=a.filename[:500], content_type=(a.content_type or "")[:100] or None,
                               size=a.size,
                               path=str(path)))
    db.flush()
    db.refresh(e)
    return e


# ---------------------------------------------------------------- acting on mail

def receive_waiver_copy(db: Session, w: Waiver, e: EmailMessage, actor: str,
                        attachment: EmailAttachment | None = None) -> bool:
    """File the returned copy against the waiver and run the advisory check. When the sub has
    suppliers, each attachment is checked: supplier waivers are filed under that supplier."""
    from app.mail import verify
    from app.suppliers import service as suppliers
    from app.waivers import service as waivers

    if w.status in (WaiverStatus.ACCEPTED, WaiverStatus.VOID):
        return False
    if attachment is None and not w.supplier_id and suppliers.active(db, w.subcontract):
        return _receive_with_suppliers(db, w, e, actor)
    att = attachment or next((a for a in e.attachments if a.is_pdf), None) or \
        next((a for a in e.attachments if a.is_image), None)
    if att is None:
        return False
    data = filestore.read(db, att.path)
    if data is None:
        return False
    try:
        waivers.mark_received(db, w, actor, att.filename, data)
    except ValueError:
        return False
    tmpl = waivers.registry().templates.get(w.template_key or "")
    result = verify.verify(w, att)
    if result.get("found") is not None and tmpl is not None and not tmpl.notary_required:
        result["problems"] = verify.compare(w, result["found"], notary_required=False)
        result["status"] = "issues" if result["problems"] else "looks_good"
    w.verification = result | {"email_id": e.id}
    waivers.log_event(db, w, actor, "checked", note=result.get("status"))
    return True


def _receive_with_suppliers(db: Session, w: Waiver, e: EmailMessage, actor: str) -> bool:
    """The sub's email may carry its suppliers' waivers too. Check each attachment; file the sub's
    own as its waiver and each supplier's under that supplier. Without the automatic check, the
    first attachment is taken as the sub's (as before)."""
    from app.mail import verify
    from app.suppliers import service as suppliers
    from app.waivers import service as waivers

    files = [a for a in e.attachments if a.is_pdf] + [a for a in e.attachments if a.is_image]
    own_filed = False
    for att in files:
        result = verify.verify(w, att)
        found = result.get("found")
        if found is None:  # not checked (no API key, or an error): old behaviour for the first file
            if not own_filed:
                own_filed = receive_waiver_copy(db, w, e, actor, attachment=att)
            continue
        if found.get("is_supplier_waiver"):
            s = suppliers.match_supplier(db, w.subcontract, found.get("signer_company"))
            if s is not None:
                suppliers.file_supplier_waiver(db, w, s, att, result, actor)
            else:
                upsert_review(db, "supplier_waiver_unknown", "email", e.id, w.subcontract.project.number,
                              f"A supplier waiver from {found.get('signer_company') or 'an unknown company'} came in with "
                              f"{w.subcontract.vendor.name}'s email, but they aren't on {w.subcontract.vendor.name}'s "
                              "supplier list. Add them, then file it from the email.", True)
            continue
        if own_filed:
            continue
        data = filestore.read(db, att.path)
        if data is None:
            continue
        try:
            waivers.mark_received(db, w, actor, att.filename, data)
        except ValueError:
            continue
        w.verification = result | {"email_id": e.id}
        waivers.log_event(db, w, actor, "checked", note=result.get("status"))
        own_filed = True
    return own_filed


def _bounce_original(db: Session, mb: Mailbox, msg: Incoming) -> EmailMessage | None:
    refs = " ".join(x for x in (msg.in_reply_to, msg.references) if x)
    for rfc in re.findall(r"<[^>]+>", refs):
        e = db.scalar(select(EmailMessage).where(EmailMessage.rfc_message_id == rfc, EmailMessage.direction == "out"))
        if e:
            return e
    known = _known_thread(db, mb, msg.thread_id)
    if known and known.direction == "out":
        return known
    # Gmail bounces quote the failed address in the body; match it to a recent send.
    addrs = {a.lower() for a in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", msg.body_text or "")}
    if addrs:
        recent = db.scalars(select(EmailMessage).where(EmailMessage.direction == "out", EmailMessage.status == "sent",
                                                       EmailMessage.mailbox_id == mb.id)
                            .order_by(EmailMessage.id.desc()).limit(200))
        for e in recent:
            if addrs & {a.lower() for a in (e.to_addrs or [])}:
                return e
    return None


def _handle_bounce(db: Session, mb: Mailbox, msg: Incoming, e: EmailMessage) -> None:
    original = _bounce_original(db, mb, msg)
    e.kind = "bounce"
    if original is None:
        return
    original.status = "bounced"
    original.error = "Bounced: " + (msg.subject or "")[:200]
    outbound.log_email(db, original, "system:mail", "bounced", bounce_email_id=e.id)
    e.waiver_id, e.vendor_id, e.project_number = original.waiver_id, original.vendor_id, original.project_number
    v = original.vendor
    if v:
        v.last_bounce_at = now()
        v.last_bounce_address = (original.to_addrs or [None])[0]
        others = set()
        for sc in db.scalars(select(Subcontract).where(Subcontract.vendor_id == v.id)):
            others |= {x for x in (sc.billing_email, sc.pm_email, sc.estimator_email, sc.foreman_email) if x}
        from app.contacts.service import vendor_contacts
        others |= {c.email for c in vendor_contacts(db, v.id)}
        others -= {a.lower() for a in (original.to_addrs or [])}
        suggestion = f" Other addresses on file: {', '.join(sorted(others))}." if others else ""
        upsert_review(db, "email_bounced", "email", original.id, original.project_number,
                      f"Email to {v.name} ({', '.join(original.to_addrs or [])}) bounced.{suggestion} "
                      "Fix the address on the draft and resend.", True)
