"""What to do with a sub's reply once it's tied to a waiver.

The rules, in order:
- The usable files are PDFs and photos that aren't empty, aren't our own unsigned PDF coming
  back, aren't email-signature logos, and aren't repeats of each other.
- A reply that only says "forwarded to billing" / "will sign" (with no real file, or a file the
  automatic check says isn't signed) keeps the waiver waiting, with a note.
- Anything the app can't settle on its own becomes a Quick question instead of staying silent:
  a reply with a question and no file, a file it can't read (.docx, .zip...), "attached" with
  nothing attached, a link to DocuSign/Drive, extra signed files, a copy for a cancelled waiver,
  a file from a different company than the waiver's, the same file we already sent back.
"""
from __future__ import annotations

import hashlib
import re

from sqlalchemy.orm import Session

from app.db.models import EmailAttachment, EmailMessage, Waiver
from app.services import filestore
from app.services.reviews import upsert_review
from app.waivers.types import WaiverStatus

LOGO = re.compile(r"^(image\d{3}|logo|outlook-|signature|sig\d*|facebook|twitter|linkedin|instagram)[\w-]*\.(png|jpe?g|gif)$",
                  re.I)
SMALL_IMAGE = 15_000      # bytes; real photos of a signed page are far bigger than a logo
TINY_FILE = 16       # bytes; an empty or broken file
LINKS = re.compile(r"https?://\S*(docusign|dropbox|drive\.google|onedrive|sharepoint|box\.com|wetransfer|adobe|"
                   r"dochub|pandadoc|hellosign)\S*", re.I)
SAYS_ATTACHED = re.compile(r"\b(attached|attaching|see attachment|enclosed|here (it )?is|here you go)\b", re.I)
UNREADABLE = (".doc", ".docx", ".zip", ".rar", ".7z", ".xls", ".xlsx", ".pages", ".odt", ".heic", ".heif")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_pdf_bytes(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def candidates(db: Session, w: Waiver | None, e: EmailMessage) -> tuple[list[EmailAttachment], list[EmailAttachment], bool]:
    """(files that could be the signed copy, files the app can't read, whether our own PDF came back)."""
    from app.mail.unsigned import our_pdf_digests

    ours = our_pdf_digests(db, w) if w is not None else set()
    seen, good, unreadable, came_back = set(), [], [], False
    for a in e.attachments:
        data = filestore.read(db, a.path) or b""
        name = a.filename or ""
        pdf = a.is_pdf or is_pdf_bytes(data)
        image = a.is_image and not name.lower().endswith((".heic", ".heif"))
        if not (pdf or image):
            if name.lower().endswith(UNREADABLE) or (a.content_type or "").startswith("application/"):
                unreadable.append(a)
            continue
        if not data or (len(data) < TINY_FILE and not is_pdf_bytes(data)):
            continue  # empty or broken
        if image and (LOGO.match(name) or len(data) < SMALL_IMAGE):
            continue  # email-signature logo
        h = digest(data)
        if h in ours:
            came_back = True
            continue
        if h in seen:
            continue
        seen.add(h)
        good.append(a)
    good.sort(key=lambda a: (not (a.is_pdf or is_pdf_bytes(filestore.read(db, a.path) or b"")),))
    return good, unreadable, came_back


def own_words(e: EmailMessage) -> str:
    from app.mail.unsigned import own_words as words
    return words(e)


def ask(db: Session, kind: str, e: EmailMessage, w: Waiver | None, text: str) -> None:
    project = w.subcontract.project.number if w is not None else e.project_number
    upsert_review(db, kind, "email", e.id, project, text, True)


def who(w: Waiver | None, e: EmailMessage) -> str:
    if w is not None:
        return w.subcontract.vendor.name
    return e.from_name or e.from_addr or "Someone"


def same_as_sent_back(db: Session, w: Waiver, files: list[EmailAttachment]) -> bool:
    """The sub sent the exact file we already sent back."""
    from sqlalchemy import select

    from app.db.models import WaiverEvent
    if w.status != WaiverStatus.REJECTED:
        return False
    old = db.scalar(select(WaiverEvent).where(WaiverEvent.waiver_id == w.id, WaiverEvent.action == "received")
                    .order_by(WaiverEvent.id.desc()))
    path = w.signed_pdf_path
    if not path and old is not None and old.details:
        return False
    rejected = filestore.read(db, path) if path else None
    return bool(rejected) and any(digest(filestore.read(db, a.path) or b"") == digest(rejected) for a in files)


def handle(db: Session, w: Waiver, e: EmailMessage, sender_vendor_id: int | None, readonly: bool) -> None:
    """A reply tied to waiver w (by thread, tag or sender). File it, hold it, or ask about it."""
    from app.mail import unsigned
    from app.mail.inbound import receive_waiver_copy

    name = w.subcontract.vendor.name
    files, unreadable, came_back = candidates(db, w, e)
    words = own_words(e)

    if w.status == WaiverStatus.VOID:
        if files:
            ask(db, "copy_for_cancelled", e, w, f"{name} sent a signed copy for waiver #{w.id}, which was cancelled. "
                                                f"Open it to restore the waiver or file it elsewhere.")
        elif words.strip() and not readonly:
            _respond(db, w, e, words, sender_vendor_id, name)
        return
    if w.status == WaiverStatus.ACCEPTED:
        if files:
            ask(db, "copy_after_accepted", e, w, f"{name} sent another signed copy for waiver #{w.id}, which is already "
                                                 "accepted. Is it for a different waiver?")
        elif words.strip() and not readonly:
            _respond(db, w, e, words, sender_vendor_id, name)
        return
    if sender_vendor_id and sender_vendor_id != w.subcontract.vendor_id and files:
        ask(db, "other_company", e, w, f"A file from {e.from_addr} came in on {name}'s waiver #{w.id}, but that address "
                                       "belongs to a different sub. Open it and file it where it belongs.")
        return
    if readonly:
        if files:
            ask(db, "found_in_ap", e, w, f"A signed copy for {name}'s waiver #{w.id} came in to {e.found_in}. Open it "
                                         "and click File as signed copy.")
        return

    if not files:
        if came_back:
            said = unsigned.reason_from_words(words)
            unsigned.note_reply(db, w, e, "They sent back our own unsigned copy." + (f" {said}" if said else ""))
            _respond(db, w, e, words, sender_vendor_id, name, noted=True)
        elif unsigned.NOT_SIGNED.search(words):
            unsigned.note_reply(db, w, e, unsigned.reason_from_words(words) or "They replied without a signed copy.")
            _respond(db, w, e, words, sender_vendor_id, name, noted=True)
        elif LINKS.search(words):
            ask(db, "signed_by_link", e, w, f"{name} sent a link instead of a file for waiver #{w.id} "
                                            "(DocuSign, Drive or similar). Open it and save the signed copy.")
        elif unreadable:
            names = ", ".join(a.filename for a in unreadable[:3])
            ask(db, "unreadable_file", e, w, f"{name} sent {names} for waiver #{w.id}, which the app can't read as a "
                                             "signed copy. Open it.")
        elif SAYS_ATTACHED.search(words):
            ask(db, "nothing_attached", e, w, f"{name} says it's attached, but nothing came with their email for "
                                              f"waiver #{w.id}. Ask them to resend.")
        elif words.strip():
            if not _respond(db, w, e, words, sender_vendor_id, name):
                ask(db, "sub_replied", e, w, f'{name} replied about waiver #{w.id}: "{words.strip()[:160]}"')
        return

    if same_as_sent_back(db, w, files):
        unsigned.note_reply(db, w, e, "They sent the same copy we sent back.")
        return
    held = unsigned.hold_reason(db, w, e, files, words)
    if held:
        unsigned.note_reply(db, w, e, held)
        return
    if receive_waiver_copy(db, w, e, actor="system:mail", attachment=None, files=files):
        if unsigned.NOT_SIGNED.search(words):
            # Filed, but what they wrote suggests it may not be signed: say so next to it.
            w.verification = dict(w.verification or {}) | {
                "note": unsigned.reason_from_words(words) or "Their email suggests it may not be signed yet."}
    # More than one usable file that isn't a supplier waiver: say so instead of dropping the rest.
    extra = [a for a in files if a.id not in _filed_ids(db, w, e)]
    if extra and not _supplier_tracked(db, w):
        ask(db, "extra_files", e, w, f"{name}'s email had {len(files)} signed files; one was filed to waiver #{w.id}. "
                                     f"Open it to file {', '.join(a.filename for a in extra[:3])}.")


def _respond(db: Session, w: Waiver, e: EmailMessage, words: str, sender_vendor_id: int | None, name: str,
             noted: bool = False) -> bool:
    """A reply without a signed copy: draft our answer, wait quietly, or (returns False) leave it
    to a person. Replies from a different company are always left to a person."""
    from app.mail import answer
    if sender_vendor_id and sender_vendor_id != w.subcontract.vendor_id:
        return False
    d = answer.respond(db, w, e, words, noted=noted)
    if d is None:
        return False
    if d.action == "check_first":
        ask(db, "sub_replied", e, w, f'{name} asked about waiver #{w.id}: "{words.strip()[:160]}" '
                                     f"Check first: {d.check or 'confirm the facts in the reply'} "
                                     "A suggested reply is waiting in Ready to send.")
    return True


def _filed_ids(db: Session, w: Waiver, e: EmailMessage) -> set[int]:
    from sqlalchemy import select
    ids = set()
    v = w.verification or {}
    if v.get("attachment_id"):
        ids.add(v["attachment_id"])
    for sw in db.scalars(select(Waiver).where(Waiver.payable_id == w.payable_id, Waiver.supplier_id.is_not(None))):
        if (sw.verification or {}).get("attachment_id"):
            ids.add(sw.verification["attachment_id"])
    if not ids and w.signed_pdf_path:
        for a in e.attachments:
            if a.filename and w.signed_pdf_path.endswith("".join(ch for ch in a.filename if ch.isalnum() or ch in "._- ")):
                ids.add(a.id)
                break
    return ids


def _supplier_tracked(db: Session, w: Waiver) -> bool:
    from app.suppliers import service as suppliers
    return bool(suppliers.active(db, w.subcontract))
