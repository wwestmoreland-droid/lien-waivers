"""Replies that aren't a signed copy yet: "forwarded to our billing department", "will sign
and send back", or our own unsigned PDF coming back attached. Those aren't put in for
review; the waiver keeps waiting, with a note of what the sub said.
"""
from __future__ import annotations

import hashlib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EmailEvent, EmailMessage, StoredFile, Waiver
from app.services import filestore

NOT_SIGNED = re.compile(
    r"forward(ed|ing)? (this |it )?(to|over to)|sent (this |it )?(over )?to (our|my|the) (billing|accounting|accounts|ap|office)"
    r"|(billing|accounting|accounts payable|ap) (dept|department|team|office) (will|to) "
    r"|will (sign|get (this|it) signed|have (this|it) signed|send (it|this) back|get back)"
    r"|(get|getting|have|having) (this|it) (signed|notarized)|not (yet )?(signed|notarized)"
    r"|(once|when) (it'?s|it is) (signed|notarized)|working on (it|this)|looking into (it|this)"
    r"|(passed|passing) (this|it) (along|on)|cc'?d (our|my) (billing|accounting)", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def own_words(e: EmailMessage) -> str:
    from app.contacts.signature import own_text
    return " ".join(x for x in own_text(e.body_text or "") if x)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def our_pdf_digests(db: Session, w: Waiver) -> set[str]:
    """Every version of the PDF we generated for this waiver."""
    out = set()
    for f in db.scalars(select(StoredFile).where(StoredFile.key.like(f"waivers/{w.id}/%"))):
        if "/signed-" not in f.key:
            out.add(_digest(f.content))
    return out


def reason_not_signed(db: Session, w: Waiver, e: EmailMessage) -> str | None:
    """Why this reply shouldn't be filed as the signed copy, or None if it looks like one."""
    files = [a for a in e.attachments if a.is_pdf or a.is_image]
    if not files:
        return None
    ours = our_pdf_digests(db, w)
    if ours and all(_digest(filestore.read(db, a.path) or b"") in ours for a in files):
        return "They sent back our own unsigned copy."
    words = own_words(e)
    m = NOT_SIGNED.search(words)
    if not m:
        return None
    # They say it isn't signed yet. If the automatic check can read the file, trust a signature it finds.
    from app.mail import verify
    result = verify.verify(w, next((a for a in files if a.is_pdf), files[0]))
    found = result.get("found") or {}
    if found and found.get("appears_signed") and found.get("is_lien_waiver"):
        return None
    snippet = words[max(0, m.start() - 60): m.end() + 80].strip()
    return f'They wrote: "{snippet}"'


def forwarded_addresses(db: Session, w: Waiver, e: EmailMessage) -> list[str]:
    """Addresses the sub sent the waiver on to (copied on the reply, or named in it)."""
    from app.contacts.harvest import skip_address

    words = own_words(e)
    if not NOT_SIGNED.search(words) and "forward" not in words.lower():
        return []
    seen = {(e.from_addr or "").lower(), *[a.lower() for a in (w.subcontract.vendor.waiver_email,
                                                                w.subcontract.billing_email) if a]}
    out = []
    for a in list(e.to_addrs or []) + list(e.cc_addrs or []) + EMAIL.findall(words):
        a = a.lower().strip(".")
        if a in seen or (e.mailbox and skip_address(a, e.mailbox)):
            continue
        seen.add(a)
        out.append(a)
    return out


def note_reply(db: Session, w: Waiver, e: EmailMessage, reason: str, actor: str = "system:mail") -> None:
    """Keep the waiver waiting; record what the sub said, and offer their billing address."""
    from app.mail import outbound
    from app.services.reviews import upsert_review
    from app.waivers import service as waivers

    waivers.log_event(db, w, actor, "reply_not_signed", note=reason, email_id=e.id)
    outbound.log_email(db, e, actor, "not_filed", reason=reason)
    for addr in forwarded_addresses(db, w, e)[:1]:
        upsert_review(db, "waiver_forwarded", "email", e.id, w.subcontract.project.number,
                      f"{w.subcontract.vendor.name} sent the waiver on to {addr}. Send their waivers there from now on?",
                      True)


def was_held(db: Session, e: EmailMessage) -> bool:
    return db.scalar(select(EmailEvent.id).where(EmailEvent.email_id == e.id, EmailEvent.action == "not_filed")) \
        is not None


def last_word(db: Session, w: Waiver) -> str | None:
    """What the sub last said without sending a signed copy (for Waiting on subs)."""
    from app.db.models import WaiverEvent
    ev = db.scalar(select(WaiverEvent).where(WaiverEvent.waiver_id == w.id, WaiverEvent.action == "reply_not_signed")
                   .order_by(WaiverEvent.id.desc()))
    return f"{ev.note} ({ev.at:%b} {ev.at.day})" if ev else None
