"""Mail provider interface. Gmail is the only implementation today; Microsoft 365 can be added
behind the same interface later without touching the rest of the app."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class TokenError(Exception):
    """The mailbox's sign-in no longer works (password change, access revoked)."""


class CursorExpired(Exception):
    """The provider can't list changes since the saved cursor; start again from now."""


@dataclass
class OutAttachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class Outgoing:
    from_addr: str
    from_name: str | None
    to: list[str]
    cc: list[str]
    subject: str
    body: str
    attachments: list[OutAttachment] = field(default_factory=list)
    thread_id: str | None = None
    in_reply_to: str | None = None
    references: str | None = None


@dataclass
class SentResult:
    message_id: str
    thread_id: str
    rfc_message_id: str | None


@dataclass
class InAttachment:
    filename: str
    content_type: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class Incoming:
    message_id: str
    thread_id: str
    rfc_message_id: str | None
    in_reply_to: str | None
    references: str | None
    from_addr: str
    from_name: str | None
    to: list[str]
    cc: list[str]
    recipients_all: list[str]  # To, Cc, Delivered-To, X-Forwarded-To, X-Original-To
    subject: str
    body_text: str
    date: datetime | None
    label_ids: list[str]
    headers: dict[str, str]
    attachments: list[InAttachment] = field(default_factory=list)


@dataclass
class Summary:
    message_id: str
    thread_id: str
    from_addr: str
    from_name: str | None
    subject: str
    date: datetime | None
    snippet: str
    unread: bool


class MailProvider(Protocol):
    email: str

    def send(self, out: Outgoing) -> SentResult: ...

    def changes_since(self, cursor: str | None) -> tuple[list[str], str]:
        """Message ids added since the cursor, and the new cursor. With no cursor, returns
        no ids and the current position (the app doesn't backfill old mail here)."""

    def get_message(self, message_id: str) -> Incoming: ...

    def ensure_label(self, name: str) -> str: ...

    def label_thread(self, thread_id: str, label_id: str) -> None: ...

    def set_read(self, message_id: str, read: bool) -> None: ...

    def list_recent(self, query: str = "", limit: int = 50) -> list[Summary]: ...

    def revoke(self) -> None: ...
