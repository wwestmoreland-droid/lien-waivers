"""Keep All Subcontracts' contact cells in step with confirmed contacts.

Automatic, after each sync: only BLANK cells are filled, and each row is read fresh from
Smartsheet right before writing, so anything a person typed is never overwritten. Replacing
a value that's already there only happens when someone clicks for it (replace_billing).

What gets filled, per row:
- Billing: the vendor's billing contact (the one picked for waivers, else the most recent
  confirmed billing contact). Billing is usually the same for every job with a vendor.
- PM, estimator, foreman: these differ by job, so an empty email is filled only when the
  vendor has exactly one confirmed contact in that role.
- A name or phone is filled next to an email that's already there when we know that person.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Contact, Project, Subcontract, Vendor
from app.services.audit import audit
from app.services.settings_store import get_setting

log = structlog.get_logger()

GROUPS = {
    "billing": ("billing_email", "billing_name", "billing_phone"),
    "pm": ("pm_email", "pm_name", "pm_phone"),
    "estimator": ("estimator_email", "estimator_name", "estimator_phone"),
    "foreman": ("foreman_email", "foreman_name", "foreman_phone"),
}
MAX_ROWS_PER_RUN = 50


@dataclass
class RowPlan:
    sc: Subcontract
    # role -> (email the row has now, or None) and the cells to fill
    groups: dict[str, tuple[str | None, dict[str, str]]] = field(default_factory=dict)


def _confirmed(db: Session, vendor_id: int, role: str) -> list[Contact]:
    return list(db.scalars(select(Contact).where(
        Contact.vendor_id == vendor_id, Contact.role == role, Contact.status == "confirmed",
        Contact.is_active.is_(True)).order_by(Contact.last_seen_at.desc().nulls_last(), Contact.id)))


def _pick(db: Session, v: Vendor, role: str) -> Contact | None:
    found = _confirmed(db, v.id, role)
    if role == "billing":
        if v.waiver_email:
            chosen = db.scalar(select(Contact).where(Contact.email == v.waiver_email))
            if chosen is not None and chosen.status == "confirmed":
                return chosen
        return found[0] if found else None
    return found[0] if len(found) == 1 else None


def plan_row(db: Session, sc: Subcontract) -> RowPlan:
    p = RowPlan(sc=sc)
    for role, (ef, nf, pf) in GROUPS.items():
        current = getattr(sc, ef)
        if current:
            c = db.scalar(select(Contact).where(Contact.email == current.lower(), Contact.status != "ignored"))
            cells = {}
        else:
            c = _pick(db, sc.vendor, role)
            cells = {ef: c.email} if c else {}
        if c is None:
            continue
        if not getattr(sc, nf) and c.name:
            cells[nf] = c.name
        phone = c.phone or c.mobile
        if not getattr(sc, pf) and phone:
            cells[pf] = phone
        if cells:
            p.groups[role] = (current, cells)
    return p


def fill_blanks(db: Session, writer, actor: str = "system:contacts") -> dict:
    stats = {"rows": 0, "cells": 0, "skipped_changed": 0, "errors": []}
    if not get_setting(db, "smartsheet_contact_fill"):
        return stats
    subs = db.scalars(select(Subcontract).join(Project).where(
        Project.is_tracked.is_(True), Subcontract.deleted_in_source.is_(False))
        .order_by(Subcontract.id)).all()
    for sc in subs:
        if stats["rows"] >= MAX_ROWS_PER_RUN:
            break
        plan = plan_row(db, sc)
        if not plan.groups:
            continue
        try:
            fresh = writer.get_row(sc.source_row_id)
            values: dict[str, str] = {}
            for role, (expected_email, cells) in plan.groups.items():
                ef = GROUPS[role][0]
                if (fresh.get(ef) or "").lower() != (expected_email or "").lower():
                    stats["skipped_changed"] += 1  # someone edited it since the last sync
                    continue
                values.update({k: v for k, v in cells.items() if not fresh.get(k)})
            if not values:
                continue
            writer.update_row(sc.source_row_id, values)
        except PermissionError:
            raise
        except Exception as exc:
            log.warning("smartsheet.fill_failed", row=sc.source_row_id, error=str(exc)[:200])
            stats["errors"].append(f"{sc.vendor.name} on {sc.project.number}: {exc}"[:300])
            if "403" in str(exc) or "401" in str(exc):
                break  # the token can't edit the sheet; no point trying every row
            continue
        for k, v in values.items():
            setattr(sc, k, v)
        audit(db, actor, "smartsheet.cells_filled", "subcontract", sc.id, row_id=sc.source_row_id,
              project=sc.project.number, vendor=sc.vendor.name, cells=values)
        stats["rows"] += 1
        stats["cells"] += len(values)
    db.flush()
    return stats


def billing_mismatch(db: Session, sc: Subcontract) -> Contact | None:
    """Smartsheet's billing email differs from the confirmed contact waivers go to."""
    v = sc.vendor
    if not (v.waiver_email and sc.billing_email) or v.waiver_email.lower() == sc.billing_email.lower():
        return None
    c = db.scalar(select(Contact).where(Contact.email == v.waiver_email))
    return c if c is not None and c.status == "confirmed" else None


def replace_billing(db: Session, writer, sc: Subcontract, expected_old: str, actor: str) -> dict:
    """Clicked by a person: put the waiver contact in this row's billing cells."""
    c = billing_mismatch(db, sc)
    if c is None:
        raise ValueError("Nothing to update.")
    fresh = writer.get_row(sc.source_row_id)
    if (fresh.get("billing_email") or "").lower() != expected_old.lower():
        raise ValueError("Billing Email was changed in Smartsheet since this page loaded. Sync and try again.")
    values = {"billing_email": c.email, "billing_name": c.name or "", "billing_phone": c.phone or c.mobile or ""}
    before = {k: fresh.get(k) for k in values}
    writer.update_row(sc.source_row_id, values)
    for k, v in values.items():
        setattr(sc, k, v or None)
    audit(db, actor, "smartsheet.billing_replaced", "subcontract", sc.id, row_id=sc.source_row_id,
          project=sc.project.number, vendor=sc.vendor.name, before=before, after=values)
    return values
