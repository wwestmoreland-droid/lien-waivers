"""Contact actions: Smartsheet seeding, confirm/ignore, billing-email suggestions."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contacts.harvest import INTERNAL_DOMAIN, rematch_unassigned
from app.db.models import Contact, Subcontract, Vendor
from app.services.audit import audit
from app.smartsheet.sync import PUBLIC_MAIL_DOMAINS

ROLES = ("billing", "pm", "estimator", "foreman", "owner", "other")


def now() -> datetime:
    return datetime.now(timezone.utc)


def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def sync_from_subcontracts(db: Session) -> int:
    """Contacts listed in All Subcontracts are known for their vendor, so they start confirmed."""
    created = 0
    for sc in db.scalars(select(Subcontract).where(Subcontract.deleted_in_source.is_(False))):
        for addr, name, phone, role in ((sc.billing_email, sc.billing_name, sc.billing_phone, "billing"),
                                        (sc.pm_email, sc.pm_name, None, "pm"),
                                        (sc.estimator_email, sc.estimator_name, None, "estimator"),
                                        (sc.foreman_email, sc.foreman_name, sc.foreman_phone, "foreman")):
            if not addr or _domain(addr) == INTERNAL_DOMAIN:
                continue
            addr = addr.lower()
            c = db.scalar(select(Contact).where(Contact.email == addr))
            if c is None:
                c = Contact(email=addr, source="smartsheet", message_count=0, first_seen_at=now())
                db.add(c)
                created += 1
            if c.status != "ignored" and (c.status == "new" or c.vendor_id is None):
                c.vendor_id, c.match_method, c.confidence = sc.vendor_id, "exact", 100
                c.status, c.confirmed_by, c.confirmed_at = "confirmed", "system:smartsheet", now()
            c.name = c.name or name
            c.phone = c.phone or phone
            c.role = c.role or role
        db.flush()
    return created


def confirm(db: Session, c: Contact, actor: str, vendor_id: int | None, role: str | None) -> None:
    before = {"vendor_id": c.vendor_id, "role": c.role, "status": c.status}
    c.vendor_id = vendor_id
    if role in ROLES:
        c.role = role
    if vendor_id and c.match_method in (None, "name", "domain", "thread") and vendor_id != before["vendor_id"]:
        c.match_method = "manual"
    c.status, c.needs_review, c.previous = "confirmed", False, None
    c.confirmed_by, c.confirmed_at, c.is_active = actor, now(), True
    if vendor_id:
        _learn_domain(db, db.get(Vendor, vendor_id), c.email)
    audit(db, actor, "contact.confirmed", "contact", c.email, before=before,
          after={"vendor_id": c.vendor_id, "role": c.role})
    rematch_unassigned(db)


def ignore(db: Session, c: Contact, actor: str) -> None:
    c.status, c.needs_review, c.is_active = "ignored", False, False
    audit(db, actor, "contact.ignored", "contact", c.email)


def _learn_domain(db: Session, v: Vendor | None, addr: str) -> None:
    """A confirmed company address teaches the vendor its domain (never public mail domains)."""
    dom = _domain(addr)
    if v is None or not dom or dom in PUBLIC_MAIL_DOMAINS or dom == INTERNAL_DOMAIN:
        return
    taken = [o for o in db.scalars(select(Vendor).where(Vendor.id != v.id, Vendor.email_domains.is_not(None)))
             if dom in (o.email_domains or [])]
    if not taken and dom not in (v.email_domains or []):
        v.email_domains = sorted(set(v.email_domains or []) | {dom})


def waiver_email(sc: Subcontract) -> str | None:
    """Where waiver requests go: the address picked on the sub page, else Smartsheet's billing email."""
    return sc.vendor.waiver_email or sc.billing_email or sc.vendor.billing_email


def vendor_contacts(db: Session, vendor_id: int) -> list[Contact]:
    return list(db.scalars(select(Contact).where(Contact.vendor_id == vendor_id, Contact.status == "confirmed",
                                                 Contact.is_active.is_(True))
                           .order_by(Contact.role != "billing", Contact.last_seen_at.desc().nulls_last())))


def billing_suggestion(db: Session, sc: Subcontract) -> Contact | None:
    """A confirmed billing contact, seen in email, whose address differs from where waivers go now."""
    current = (waiver_email(sc) or "").lower()
    for c in vendor_contacts(db, sc.vendor_id):
        if c.role == "billing" and c.source == "email" and c.email != current and c.last_seen_at:
            return c
    return None


def set_waiver_email(db: Session, v: Vendor, addr: str | None, actor: str) -> None:
    before = v.waiver_email
    v.waiver_email = (addr or "").strip().lower() or None
    v.waiver_email_set_by = actor if v.waiver_email else None
    audit(db, actor, "vendor.waiver_email", "vendor", str(v.id), before=before, after=v.waiver_email)
