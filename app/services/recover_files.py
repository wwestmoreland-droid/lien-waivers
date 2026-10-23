"""Put back PDFs and attachments that were lost from disk before files were kept in the database.

- Waiver PDFs are rebuilt from the waiver's data, dated the day it was sent (or created).
- Email attachments are downloaded again from Gmail.
- Signed copies are taken from the email they came in on.
Anything that can't be recovered (e.g. a signed copy uploaded by hand) is counted and left.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EmailAttachment, EmailMessage, Mailbox, Waiver
from app.services import filestore

log = structlog.get_logger()


def _waiver_id_from(path: str) -> int | None:
    parts = Path(path).parts
    if "waivers" in parts:
        i = parts.index("waivers")
        if i + 1 < len(parts) and parts[i + 1].isdigit():
            return int(parts[i + 1])
    return None


def recover(db: Session) -> dict:
    from app.mail import mailboxes
    from app.waivers import pdf
    from app.waivers import service as waivers
    from app.waivers.mapper import field_values

    stats = {"waiver_pdfs": 0, "attachments": 0, "signed": 0, "unrecoverable": 0}

    # 1. Waiver PDFs, and the copies attached to emails (same files).
    missing: dict[int, set[str]] = defaultdict(set)
    for w in db.scalars(select(Waiver).where(Waiver.generated_pdf_path.is_not(None))):
        if not filestore.has(db, w.generated_pdf_path):
            missing[w.id].add(w.generated_pdf_path)
    for a in db.scalars(select(EmailAttachment)):
        wid = _waiver_id_from(a.path)
        if wid and "signed-" not in Path(a.path).name and not filestore.has(db, a.path):
            missing[wid].add(a.path)
    for wid, paths in missing.items():
        w = db.get(Waiver, wid)
        t = waivers.registry().templates.get(w.template_key or "") if w else None
        if t is None:
            stats["unrecoverable"] += len(paths)
            continue
        day = (w.sent_at or w.created_at).date()
        data = pdf.fill(t, field_values(w, day))
        for p in paths:
            filestore.write(db, p, data)
            stats["waiver_pdfs"] += 1
        waivers.log_event(db, w, "system:recovery", "pdf_restored", note=f"rebuilt from the waiver's data, dated {day}")

    # 2. Inbound attachments, from Gmail.
    lost = [a for a in db.scalars(select(EmailAttachment).join(EmailMessage).where(EmailMessage.direction == "in"))
            if not filestore.has(db, a.path)]
    by_email: dict[int, list[EmailAttachment]] = defaultdict(list)
    for a in lost:
        by_email[a.email_id].append(a)
    for eid, atts in by_email.items():
        e = db.get(EmailMessage, eid)
        mb = db.get(Mailbox, e.mailbox_id) if e.mailbox_id else None
        if mb is None or mb.status != "connected" or not e.provider_message_id:
            stats["unrecoverable"] += len(atts)
            continue
        try:
            msg = mailboxes.provider_for(mb).get_message(e.provider_message_id)
        except Exception as exc:
            log.warning("recover.gmail_failed", email_id=eid, error=str(exc)[:200])
            stats["unrecoverable"] += len(atts)
            continue
        pool = list(msg.attachments)
        for a in atts:
            got = next((x for x in pool if x.filename == a.filename), None)
            if got is None:
                stats["unrecoverable"] += 1
                continue
            pool.remove(got)
            filestore.write(db, a.path, got.data)
            stats["attachments"] += 1

    # 3. Signed copies, from the email they came in on.
    for w in db.scalars(select(Waiver).where(Waiver.signed_pdf_path.is_not(None))):
        if filestore.has(db, w.signed_pdf_path):
            continue
        suffix = Path(w.signed_pdf_path).name.split("-", 2)[-1]
        source = None
        for a in db.scalars(select(EmailAttachment).join(EmailMessage).where(
                EmailMessage.waiver_id == w.id, EmailMessage.direction == "in")):
            safe = "".join(ch for ch in Path(a.filename).name if ch.isalnum() or ch in "._- ")
            if safe == suffix and filestore.has(db, a.path):
                source = a
                break
        data = filestore.read(db, source.path) if source else None
        if data is None:
            stats["unrecoverable"] += 1
            continue
        filestore.write(db, w.signed_pdf_path, data)
        stats["signed"] += 1
    db.flush()
    log.info("recover.done", **stats)
    return stats
