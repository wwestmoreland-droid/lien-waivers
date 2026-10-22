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
        self.revoked = False

    def deliver(self, *, from_addr: str, subject: str, body: str = "", thread_id: str | None = None,
                in_reply_to: str | None = None, to: list[str] | None = None, from_name: str | None = None,
                attachments: list[InAttachment] | None = None, headers: dict | None = None,
                recipients_all: list[str] | None = None, label_ids: list[str] | None = None) -> Incoming:
        mid = f"m{next(_ids)}"
        msg = Incoming(
            message_id=mid, thread_id=thread_id or f"t{mid}", rfc_message_id=f"<{mid}@fake>",
            in_reply_to=in_reply_to, references=in_reply_to, from_addr=from_addr.lower(), from_name=from_name,
            to=to or [self.email], cc=[], recipients_all=recipients_all or (to or [self.email]),
            subject=subject, body_text=body, date=datetime.now(timezone.utc),
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

    def get_message(self, message_id: str) -> Incoming:
        self._check()
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

    def recent_ids(self, days: int, limit: int = 500) -> list[str]:
        self._check()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return [mid for mid in self.box.order
                if not self.box.messages[mid].date or self.box.messages[mid].date >= cutoff][-limit:]

    def revoke(self) -> None:
        self.box.revoked = True
