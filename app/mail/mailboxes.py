"""Connected mailboxes: roles, the active Sender, encrypted tokens, broken-token handling."""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone

import structlog
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import EmailMessage, Mailbox, Project
from app.mail.provider import MailProvider, TokenError
from app.services.audit import audit

log = structlog.get_logger()

LABEL_NAME = "Lien Waivers"


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- token encryption

def _fernet() -> Fernet:
    s = get_settings()
    if s.fernet_key:
        return Fernet(s.fernet_key.encode())
    # No separate key set: derive one from SECRET_KEY so there's one less variable to manage.
    digest = hashlib.sha256(("mailbox-tokens:" + s.secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise TokenError("Stored token can't be decrypted (SECRET_KEY or FERNET_KEY changed)") from exc


# ---------------------------------------------------------------- providers

def _default_factory(mailbox: Mailbox) -> MailProvider:
    if get_settings().mail_provider == "fake":
        from app.mail.fake import FakeMailProvider
        return FakeMailProvider(mailbox.email)
    from app.mail.gmail import GmailProvider
    if not mailbox.refresh_token_enc:
        raise TokenError("Mailbox isn't connected")
    return GmailProvider(mailbox.email, decrypt(mailbox.refresh_token_enc))


_factory = _default_factory


def set_provider_factory(factory) -> None:
    global _factory
    _factory = factory or _default_factory


def provider_for(mailbox: Mailbox) -> MailProvider:
    return _factory(mailbox)


# ---------------------------------------------------------------- lookups

def active_sender(db: Session) -> Mailbox | None:
    return db.scalar(select(Mailbox).where(Mailbox.role == "sender", Mailbox.status != "disconnected"))


def sender_for_project(db: Session, project: Project | None) -> Mailbox | None:
    if project is not None and project.sender_mailbox_id:
        mb = db.get(Mailbox, project.sender_mailbox_id)
        if mb and mb.can_send:
            return mb
    return active_sender(db)


def broken_mailboxes(db: Session) -> list[Mailbox]:
    return list(db.scalars(select(Mailbox).where(Mailbox.status == "broken")))


def mailboxes(db: Session) -> list[Mailbox]:
    return list(db.scalars(select(Mailbox).where(Mailbox.status != "disconnected").order_by(Mailbox.email)))


def has_send_scope(mb: Mailbox) -> bool:
    return "gmail.send" in (mb.scopes or "") or get_settings().mail_provider == "fake"


def open_threads(db: Session, mb: Mailbox) -> int:
    """Threads this mailbox started whose waiver isn't closed yet."""
    from app.db.models import Waiver
    from app.waivers.types import OPEN_STATUSES

    return db.scalar(
        select(EmailMessage.provider_thread_id).join(Waiver, EmailMessage.waiver_id == Waiver.id)
        .where(EmailMessage.mailbox_id == mb.id, EmailMessage.direction == "out",
               Waiver.status.in_([s.value for s in OPEN_STATUSES]))
        .limit(1)
    ) is not None


# ---------------------------------------------------------------- changes

def connect(db: Session, email_addr: str, role: str, refresh_token: str | None, scopes: str, actor: str) -> Mailbox:
    email_addr = email_addr.lower()
    mb = db.scalar(select(Mailbox).where(Mailbox.email == email_addr))
    if mb is None:
        mb = Mailbox(email=email_addr, role=role)
        db.add(mb)
    if refresh_token:
        mb.refresh_token_enc = encrypt(refresh_token)
    elif not mb.refresh_token_enc:
        raise ValueError("Google didn't return a refresh token. Remove the app's access in your Google "
                         "account settings and connect again.")
    mb.scopes = scopes
    mb.status = "connected"
    mb.last_error = None
    mb.connected_by = actor
    mb.connected_at = now()
    if not mb.display_name:
        mb.display_name = "Buildout Pros – Accounts Payable"
    if mb.role == "disconnected":
        mb.role = role
    db.flush()
    if role == "sender":
        make_sender(db, mb, actor)
    elif mb.role != "sender":
        mb.role = role
    try:
        _, cursor = provider_for(mb).changes_since(None)
        mb.history_id = cursor
        mb.last_polled_at = None  # next check looks back CATCH_UP_DAYS
    except TokenError as exc:
        mark_broken(db, mb, str(exc))
    audit(db, actor, "mailbox.connected", "mailbox", mb.email, role=mb.role, scopes=scopes)
    if mb.status == "connected":
        from app.contacts import backfill
        from app.db.models import ContactBackfill
        if db.scalar(select(ContactBackfill.id).where(ContactBackfill.mailbox_id == mb.id)) is None:
            backfill.start(db, mb, actor)  # 12 months of contacts, overnight
    return mb


def make_sender(db: Session, mb: Mailbox, actor: str) -> None:
    if not has_send_scope(mb):
        raise ValueError(f"{mb.email} was connected read-only. Connect it again as a Sender to grant "
                         "permission to send.")
    old = active_sender(db)
    if old and old.id != mb.id:
        old.role = "listening"
        audit(db, actor, "mailbox.listening", "mailbox", old.email,
              note="Former Sender keeps reading replies on its open threads")
    mb.role = "sender"
    audit(db, actor, "mailbox.sender_changed", "mailbox", mb.email, previous=old.email if old else None)


def disconnect(db: Session, mb: Mailbox, actor: str) -> None:
    try:
        if mb.refresh_token_enc:
            provider_for(mb).revoke()
    except TokenError:
        pass
    mb.status = "disconnected"
    mb.role = "disconnected"
    mb.refresh_token_enc = None
    audit(db, actor, "mailbox.disconnected", "mailbox", mb.email)


def mark_broken(db: Session, mb: Mailbox, error: str) -> None:
    if mb.status != "broken":
        log.warning("mailbox.broken", mailbox=mb.email, error=error)
        audit(db, "system:mail", "mailbox.broken", "mailbox", mb.email, error=error)
    mb.status = "broken"
    mb.last_error = error
