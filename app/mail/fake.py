"""In-memory mail provider for tests and local development (MAIL_PROVIDER=fake)."""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from app.mail.provider import Incoming, InAttachment, Outgoing, SentResult, Summary, TokenError

_ids = itertools.count(1)


class FakeMailbox:
    """Shared state for one fake mailbox (survives provider re-creation)."""

    def __init__(self, email: str):
        self.email = email
        self.messages: dict[str, Incoming] = {}
        self.order: list[str] = []
        self.sent: list[Outgoing] = []
        self.labels: dict[str, str] = {}
        self.thread_labels: dict[str, set[str]] = {}
        self.read: dict[str, bool] = {}
        self.broken = False
        self.metadata_gets = 0
        self.failing: set[str] = set()
        self.full_gets = 0
        self.revoked = False

    def deliver(self, *, from_addr: str, subject: str, body: str = "", thread_id: str | None = None,
                in_reply_to: str | None = None, to: list[str] | None = None, from_name: str | None = None,
                attachments: list[InAttachment] | None = None, headers: dict | None = None,
                recipients_all: list[str] | None = None, label_ids: list[str] | None = None,
                cc: list[str] | None = None, date: datetime | None = None) -> Incoming:
        mid = f"m{next(_ids)}"
        msg = Incoming(
            message_id=mid, thread_id=thread_id or f"t{mid}", rfc_message_id=f"<{mid}@fake>",
            in_reply_to=in_reply_to, references=in_reply_to, from_addr=from_addr.lower(), from_name=from_name,
            to=to or [self.email], cc=cc or [], recipients_all=recipients_all or (to or [self.email]) + (cc or []),
            subject=subject, body_text=body, date=date or datetime.now(timezone.utc),
            label_ids=label_ids or ["INBOX", "UNREAD"], headers={k.lower(): v for k, v in (headers or {}).items()},
            attachments=attachments or [],
        )
        self.messages[mid] = msg
        self.order.append(mid)
        return msg


class FakeMailProvider:
    boxes: dict[str, FakeMailbox] = {}

    def __init__(self, email: str):
        self.email = email
        self.box = FakeMailProvider.boxes.setdefault(email, FakeMailbox(email))

    @classmethod
    def reset(cls) -> None:
        cls.boxes = {}

    def _check(self) -> None:
        if self.box.broken:
            raise TokenError("token revoked (fake)")

    def send(self, out: Outgoing) -> SentResult:
        self._check()
        mid = f"m{next(_ids)}"
        thread = out.thread_id or f"t{mid}"
        self.box.sent.append(out)
        msg = Incoming(message_id=mid, thread_id=thread, rfc_message_id=f"<{mid}@fake>",
                       in_reply_to=out.in_reply_to, references=out.references, from_addr=out.from_addr,
                       from_name=out.from_name, to=out.to, cc=out.cc, recipients_all=out.to + out.cc,
                       subject=out.subject, body_text=out.body, date=datetime.now(timezone.utc),
                       label_ids=["SENT"], headers={}, attachments=[])
        self.box.messages[mid] = msg
        self.box.order.append(mid)
        return SentResult(message_id=mid, thread_id=thread, rfc_message_id=msg.rfc_message_id)

    def changes_since(self, cursor: str | None) -> tuple[list[str], str]:
        self._check()
        pos = int(cursor) if cursor else len(self.box.order)
        return list(self.box.order[pos:]), str(len(self.box.order))

    def get_message(self, message_id: str, attachments: bool = True) -> Incoming:
        from app.mail.provider import MessageGone
        self._check()
        self.box.full_gets += 1
        if message_id not in self.box.messages:
            raise MessageGone(message_id)
        if message_id in self.box.failing:
            raise RuntimeError(f"Gmail had a problem with {message_id}")
        return self.box.messages[message_id]

    def ensure_label(self, name: str) -> str:
        return self.box.labels.setdefault(name, f"Label_{len(self.box.labels) + 1}")

    def label_thread(self, thread_id: str, label_id: str) -> None:
        self.box.thread_labels.setdefault(thread_id, set()).add(label_id)

    def set_read(self, message_id: str, read: bool) -> None:
        self.box.read[message_id] = read

    def list_recent(self, query: str = "", limit: int = 50) -> list[Summary]:
        out = []
        for mid in reversed(self.box.order[-limit:]):
            m = self.box.messages[mid]
            out.append(Summary(message_id=mid, thread_id=m.thread_id, from_addr=m.from_addr, from_name=m.from_name,
                               subject=m.subject, date=m.date, snippet=m.body_text[:120],
                               unread="UNREAD" in m.label_ids))
        return out

    def get_metadata(self, message_id: str) -> Incoming:
        from dataclasses import replace
        self._check()
        self.box.metadata_gets += 1
        if message_id not in self.box.messages:
            from app.mail.provider import MessageGone
            raise MessageGone(message_id)
        return replace(self.box.messages[message_id], body_text="", attachments=[])

    def list_page(self, query: str, page_token: str | None, size: int = 100) -> tuple[list[str], str | None, int]:
        self._check()
        newest_first = list(reversed(self.box.order))
        start = int(page_token or 0)
        ids = newest_first[start:start + size]
        nxt = str(start + size) if start + size < len(newest_first) else None
        return ids, nxt, len(newest_first)

    def recent_ids(self, days: int, limit: int = 500) -> list[str]:
        self._check()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return [mid for mid in self.box.order if mid not in self.box.messages
                or not self.box.messages[mid].date or self.box.messages[mid].date >= cutoff][-limit:]

    def revoke(self) -> None:
        self.box.revoked = True
