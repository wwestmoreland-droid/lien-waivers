"""Log vendor contacts from email headers and signatures (section 6B).

Reads From, Reply-To, To and Cc, and the sender's signature. Skips internal senders,
no-reply and notification addresses, bulk mail, calendar invites, auto-replies and bounces.
The email body is never stored; only contact fields and a link to the message.
"""
from __future__ import annotations

import email.utils
import re
from collections.abc import Callable
from datetime import datetime, timezone

import structlog
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.contacts import ai, signature
from app.db.models import Contact, ContactMessage, EmailMessage, Mailbox, Subcontract, Vendor
from app.mail.provider import Incoming
from app.matching import resolver
from app.smartsheet.sync import PUBLIC_MAIL_DOMAINS

log = structlog.get_logger()

INTERNAL_DOMAIN = "buildoutpros.com"
SKIP_LOCAL = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|noreply|notifications?|notify|mailer-daemon|postmaster|bounces?|"
    r"alerts?|news|newsletters?|marketing|promotions?|updates|automated|system|daemon|calendar-notification|"
    r"invitations?|digest|mailer)([+._-]|$)", re.I)
SKIP_DOMAINS = re.compile(
    r"(^|\.)(docusign\.net|docusign\.com|smartsheet\.com|google\.com|googlemail\.com|intuit\.com|"
    r"quickbooks\.com|zoom\.us|calendly\.com|mailchimp\.com|constantcontact\.com|hubspot\.com|"
    r"salesforce\.com|linkedin\.com|facebookmail\.com|amazonses\.com|sendgrid\.net|procore\.com|"
    r"microsoft\.com|office365\.com)$", re.I)
CALENDAR_SUBJECT = re.compile(r"^(invitation|updated invitation|accepted|declined|tentatively accepted|"
                              r"canceled event|cancelled event|new event)\b", re.I)
BULK_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS", "SPAM"}
NOT_A_NAME = re.compile(r"@|^(ap|ar|accounts?|accounting|billing|office|admin|info|sales|contracts?)$", re.I)
FIELDS = ("phone", "mobile", "title", "company")
MAX_SIGNATURE_CHECKS = 4


def now() -> datetime:
    return datetime.now(timezone.utc)


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def is_bulk(msg: Incoming) -> bool:
    h = msg.headers
    return bool(h.get("list-unsubscribe") or h.get("list-id")) or \
        h.get("precedence", "").lower() in ("bulk", "list", "junk") or bool(BULK_LABELS & set(msg.label_ids))


def is_calendar(msg: Incoming) -> bool:
    return "text/calendar" in msg.headers.get("content-type", "").lower() or bool(CALENDAR_SUBJECT.match(msg.subject or ""))


def skip_message(msg: Incoming) -> str | None:
    from app.mail.inbound import is_auto_reply, is_bounce

    if is_bulk(msg):
        return "bulk"
    if is_calendar(msg):
        return "calendar"
    if is_bounce(msg):
        return "bounce"
    if is_auto_reply(msg):
        return "auto-reply"
    return None


def skip_address(addr: str, mb: Mailbox) -> bool:
    addr = addr.lower()
    dom = _domain(addr)
    local = addr.split("@", 1)[0]
    return (not dom or dom == INTERNAL_DOMAIN or dom.endswith("." + INTERNAL_DOMAIN) or addr in (mb.email, mb.only_to)
            or bool(SKIP_LOCAL.match(local)) or bool(SKIP_DOMAINS.search(dom)))


def wanted(mb: Mailbox, msg: Incoming) -> bool:
    """A read-only source that receives forwarded ap@ mail only looks at mail sent to ap@."""
    return not (mb.role == "readonly" and mb.only_to and mb.only_to.lower() not in msg.recipients_all)


def _people(msg: Incoming) -> list[tuple[str, str | None, str]]:
    """(address, display name, header) for everyone on the message, From first."""
    out: list[tuple[str, str | None, str]] = [(msg.from_addr, msg.from_name, "from")]
    h = msg.headers
    for field, fallback in (("reply-to", []), ("to", msg.to), ("cc", msg.cc)):
        if h.get(field):
            pairs = email.utils.getaddresses([h[field]])
        else:
            pairs = [("", a) for a in fallback]
        out += [(a.lower(), n or None, field) for n, a in pairs if a]
    seen, unique = set(), []
    for addr, name, field in out:
        if addr and addr not in seen:
            seen.add(addr)
            unique.append((addr, name, field))
    return unique


def clean_name(name: str | None, addr: str) -> str | None:
    if not name:
        return None
    name = name.strip().strip("'\"").strip()
    if "," in name and len(name.split(",")) == 2:  # "Lopez, Maria"
        last, first = (p.strip() for p in name.split(","))
        if first and last and " " not in first:
            name = f"{first} {last}"
    if not name or NOT_A_NAME.search(name) or name.lower() == addr.split("@")[0]:
        return None
    return name[:255]


# ---------------------------------------------------------------- vendor matching

def known_email_vendor(db: Session, addr: str) -> Vendor | None:
    sc = db.scalar(select(Subcontract).where(or_(
        Subcontract.billing_email == addr, Subcontract.pm_email == addr,
        Subcontract.estimator_email == addr, Subcontract.foreman_email == addr)).limit(1))
    if sc:
        return sc.vendor
    v = db.scalar(select(Vendor).where(or_(Vendor.billing_email == addr, Vendor.waiver_email == addr)).limit(1))
    if v:
        return v
    c = db.scalar(select(Contact).where(Contact.email == addr, Contact.status == "confirmed",
                                        Contact.vendor_id.is_not(None)))
    return c.vendor if c else None


def thread_vendor(db: Session, mb: Mailbox, thread_id: str | None) -> Vendor | None:
    if not thread_id:
        return None
    e = db.scalar(select(EmailMessage).where(EmailMessage.mailbox_id == mb.id,
                                             EmailMessage.provider_thread_id == thread_id,
                                             EmailMessage.vendor_id.is_not(None)).limit(1))
    return e.vendor if e else None


def domain_vendor(db: Session, addr: str) -> Vendor | None:
    dom = _domain(addr)
    if not dom or dom in PUBLIC_MAIL_DOMAINS or dom == INTERNAL_DOMAIN:
        return None  # never domain-match gmail.com and the like
    hits = [v for v in db.scalars(select(Vendor).where(Vendor.email_domains.is_not(None)))
            if dom in (v.email_domains or [])]
    return hits[0] if len(hits) == 1 else None


def name_vendor(db: Session, *names: str | None) -> tuple[Vendor | None, int]:
    for n in names:
        if not n:
            continue
        v = resolver.find_vendor(db, n)
        if v:
            return v, 95
        hits = resolver.suggest_vendors(db, n, limit=2)
        if hits and hits[0]["score"] >= 90 and (len(hits) == 1 or hits[1]["score"] < hits[0]["score"] - 5):
            return db.get(Vendor, hits[0]["vendor_id"]), hits[0]["score"]
    return None, 0


def match_vendor(db: Session, mb: Mailbox | None, addr: str, thread_id: str | None,
                 company: str | None, display_name: str | None) -> tuple[Vendor | None, str | None, int]:
    """Section 6B order: exact email, linked thread, company domain, company name, else unassigned."""
    v = known_email_vendor(db, addr)
    if v:
        return v, "exact", 100
    v = thread_vendor(db, mb, thread_id) if mb else None
    if v:
        return v, "thread", 90
    v = domain_vendor(db, addr)
    if v:
        return v, "domain", 80
    v, score = name_vendor(db, company, display_name)
    if v:
        return v, "name", score
    return None, None, 0


def role_for(db: Session, addr: str, title: str | None) -> str | None:
    role = signature.guess_role(title)
    if role:
        return role
    for col, r in ((Subcontract.billing_email, "billing"), (Subcontract.pm_email, "pm"),
                   (Subcontract.estimator_email, "estimator"), (Subcontract.foreman_email, "foreman")):
        if db.scalar(select(Subcontract.id).where(col == addr).limit(1)):
            return r
    return None


# ---------------------------------------------------------------- harvesting

class Stats:
    def __init__(self):
        self.new = 0
        self.bodies = 0
        self.skipped = 0


def harvest(db: Session, mb: Mailbox, msg: Incoming, *, fetch_body: Callable[[], Incoming] | None = None,
            budget: ai.AiBudget | None = None, stats: Stats | None = None) -> list[Contact]:
    """Log everyone on one message. `msg` may be headers only; then `fetch_body` is called
    for the sender's signature only when that sender is new or hasn't been parsed yet."""
    stats = stats or Stats()
    if not wanted(mb, msg):
        return []
    reason = skip_message(msg)
    if reason:
        stats.skipped += 1
        return []
    touched = []
    for addr, name, field in _people(msg):
        if skip_address(addr, mb):
            continue
        c, created = _upsert(db, mb, msg, addr, clean_name(name, addr), field)
        if created:
            stats.new += 1
        if field == "from":
            body_msg = msg
            if not msg.body_text and fetch_body is not None and (created or needs_signature(c)):
                body_msg = fetch_body()
                stats.bodies += 1
            if body_msg.body_text:
                _apply_signature(db, c, body_msg, created, budget)
        if c.vendor_id is None or c.status == "new":
            _assign(db, mb, c, msg.thread_id, name)
        touched.append(c)
    db.flush()
    return touched


def _upsert(db: Session, mb: Mailbox, msg: Incoming, addr: str, name: str | None, field: str) -> tuple[Contact, bool]:
    c = db.scalar(select(Contact).where(Contact.email == addr))
    created = c is None
    if created:
        c = Contact(email=addr, name=name, source="email", status="new", first_seen_at=msg.date or now(),
                    source_message_id=msg.message_id, source_mailbox_id=mb.id, message_count=0)
        db.add(c)
        db.flush()
    elif name and not c.name:
        c.name = name
    link = db.scalar(select(ContactMessage.id).where(ContactMessage.contact_id == c.id,
                                                     ContactMessage.mailbox_id == mb.id,
                                                     ContactMessage.provider_message_id == msg.message_id))
    if link is None:
        db.add(ContactMessage(contact_id=c.id, mailbox_id=mb.id, provider_message_id=msg.message_id,
                              provider_thread_id=msg.thread_id, field=field, subject=(msg.subject or "")[:1000],
                              sent_at=msg.date))
        c.message_count = (c.message_count or 0) + 1
        seen = msg.date or now()
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        last = c.last_seen_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is None or seen > last:
            c.last_seen_at = seen
        first = c.first_seen_at if c.first_seen_at.tzinfo else c.first_seen_at.replace(tzinfo=timezone.utc)
        if seen < first:
            c.first_seen_at = seen
    return c, created


def needs_signature(c: Contact) -> bool:
    return c.signature_hash is None and (c.signature_checks or 0) < MAX_SIGNATURE_CHECKS


def _apply_signature(db: Session, c: Contact, msg: Incoming, created: bool, budget: ai.AiBudget | None) -> None:
    sig = signature.parse(msg.body_text, msg.from_name)
    if not sig.found_anything and len(sig.block) < 2:
        # A one-line reply has no signature; an older message may, so try again (a few times).
        c.signature_checks = (c.signature_checks or 0) + 1
        return
    h = sig.hash
    if h is None or h == c.signature_hash:
        return
    changed_sig = c.signature_hash is not None
    found = {k: getattr(sig, k) for k in FIELDS}
    c.parsed_by = "rules"
    if not sig.found_anything and (created or changed_sig) and budget is not None and budget.take():
        got = ai.parse_signature(sig.text, msg.from_name or msg.from_addr) or {}
        found = {k: (got.get(k) or None) for k in FIELDS}
        if got.get("name") and not c.name:
            c.name = got["name"][:255]
        c.parsed_by = "ai"
    c.signature_hash = h
    # A value that replaces a different one counts as a change and goes back to the queue.
    previous = {k: getattr(c, k) for k in FIELDS if found.get(k) and getattr(c, k) and found[k] != getattr(c, k)}
    for k in FIELDS:
        if found.get(k):
            setattr(c, k, found[k][:255])
    if previous and c.status == "confirmed":
        c.previous = previous
        c.needs_review = True
    if not c.role:
        c.role = role_for(db, c.email, c.title)


def _assign(db: Session, mb: Mailbox | None, c: Contact, thread_id: str | None, display_name: str | None) -> None:
    v, method, confidence = match_vendor(db, mb, c.email, thread_id, c.company, display_name)
    if not c.role:
        c.role = role_for(db, c.email, c.title)
    if v is None:
        return
    if c.vendor_id is None:
        c.vendor_id, c.match_method, c.confidence = v.id, method, confidence
    if method == "exact" and c.status == "new":
        # Rule 1: an address already on file for this vendor is confirmed automatically.
        c.vendor_id, c.match_method, c.confidence = v.id, method, confidence
        c.status, c.confirmed_by, c.confirmed_at = "confirmed", "system:known-email", now()


def rematch_unassigned(db: Session) -> int:
    """After a contact is confirmed or vendors change, try again for contacts still unassigned."""
    n = 0
    for c in db.scalars(select(Contact).where(Contact.vendor_id.is_(None), Contact.status == "new")):
        _assign(db, None, c, None, c.name)
        n += c.vendor_id is not None
    return n
