"""Look up one vendor in the connected mailboxes right away, instead of waiting for the backfill.

Searches the last 12 months for mail from the vendor's known email domains or mentioning its
name, and logs the contacts on those messages the usual way (headers first, a body only for a
new sender's signature).
"""
from __future__ import annotations

import re

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contacts import ai
from app.contacts.harvest import Stats, harvest
from app.db.models import ContactMessage, Mailbox, Vendor
from app.mail import mailboxes
from app.mail.provider import TokenError
from app.matching.normalize import _SUFFIXES

log = structlog.get_logger()

MAX_MESSAGES_PER_MAILBOX = 60


def search_name(name: str) -> str | None:
    words = [w for w in re.sub(r"[^\w\s]", " ", name).split() if w.lower() not in _SUFFIXES]
    words = words[:3]
    return " ".join(words) if words and len(" ".join(words)) >= 4 else None


def query_for(v: Vendor) -> str | None:
    parts = [f"from:{d}" for d in (v.email_domains or [])]
    name = search_name(v.name)
    if name:
        parts.append(f'"{name}"')
    if not parts:
        return None
    return "{" + " ".join(parts) + "} newer_than:1y -category:promotions -category:social"


def lookup_vendor(db: Session, v: Vendor) -> tuple[list[int], list[str]]:
    """Returns (ids of contacts found on matching messages, problems to show)."""
    q = query_for(v)
    if not q:
        return [], ["There's no email domain or usable name to search for."]
    found: set[int] = set()
    problems = []
    boxes = db.scalars(select(Mailbox).where(Mailbox.status == "connected",
                                             Mailbox.role.in_(("sender", "listening", "readonly")))).all()
    if not boxes:
        return [], ["No mailbox is connected."]
    for mb in boxes:
        provider = mailboxes.provider_for(mb)
        q_mb = q
        if mb.role == "readonly" and mb.only_to:
            a = mb.only_to
            q_mb += f" {{to:{a} cc:{a} deliveredto:{a}}}"
        stats, budget = Stats(), ai.AiBudget(5)
        try:
            ids, _, _ = provider.list_page(q_mb, None, MAX_MESSAGES_PER_MAILBOX)
            for mid in ids:
                meta = provider.get_metadata(mid)
                for c in harvest(db, mb, meta, fetch_body=lambda mid=mid: provider.get_message(mid),
                                 budget=budget, stats=stats):
                    found.add(c.id)
            # Also count contacts already logged from these messages earlier.
            if ids:
                found |= set(db.scalars(select(ContactMessage.contact_id).where(
                    ContactMessage.mailbox_id == mb.id, ContactMessage.provider_message_id.in_(ids))))
        except TokenError as exc:
            mailboxes.mark_broken(db, mb, str(exc))
            problems.append(f"{mb.email} needs reconnecting.")
        except Exception as exc:
            log.exception("contacts.lookup_failed", mailbox=mb.email, vendor=v.name)
            problems.append(f"Couldn't search {mb.email}: {type(exc).__name__}")
        db.flush()
    return sorted(found), problems
