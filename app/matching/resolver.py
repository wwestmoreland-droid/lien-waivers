"""Resolve Smartsheet vendor strings to vendors and subcontracts.

The old automation joined on the "Vendor - Project name" text. Here every payable is
resolved to a subcontract by (vendor, project number). A payable that does not resolve
to exactly one subcontract is never skipped: it gets a match_status that puts it in the
Unmatched queue until a person maps it (and the mapping is saved as an alias).
"""
from __future__ import annotations

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DirectPurchase, Payable, Subcontract, Vendor, VendorAlias
from app.matching.normalize import normalize_vendor

# Spelling variants seen in the live data. Applied only to map an alias onto the
# canonical vendor; a person can add more from the Unmatched queue.
SEED_ALIASES: dict[str, str] = {
    "Ianuzzi Millwork Corporation": "Iannuzzi Millwork Corporation",
    "Crown Coverings": "Crowne Coverings",
}
_SEED_NORM = {normalize_vendor(a): normalize_vendor(c) for a, c in SEED_ALIASES.items()}
_SEED_CANONICAL_NAME = {normalize_vendor(c): c for c in SEED_ALIASES.values()}

FUZZY_SUGGEST_MIN = 85


def find_vendor(db: Session, name: str | None) -> Vendor | None:
    norm = normalize_vendor(name)
    if not norm:
        return None
    alias = db.scalar(select(VendorAlias).where(VendorAlias.normalized_alias == norm))
    if alias:
        return alias.vendor
    vendor = db.scalar(select(Vendor).where(Vendor.normalized_name == norm))
    if vendor:
        return vendor
    canonical = _SEED_NORM.get(norm)
    if canonical:
        return db.scalar(select(Vendor).where(Vendor.normalized_name == canonical))
    return None


def get_or_create_vendor(db: Session, name: str) -> Vendor:
    vendor = find_vendor(db, name)
    if vendor:
        return vendor
    norm = normalize_vendor(name)
    canonical = _SEED_NORM.get(norm)
    if canonical:
        vendor = Vendor(name=_SEED_CANONICAL_NAME[canonical], normalized_name=canonical)
        db.add(vendor)
        db.flush()
        add_alias(db, vendor, name, actor="system:seed")
        return vendor
    vendor = Vendor(name=name.strip(), normalized_name=norm)
    db.add(vendor)
    db.flush()
    return vendor


def add_alias(db: Session, vendor: Vendor, alias: str, actor: str) -> VendorAlias | None:
    norm = normalize_vendor(alias)
    if not norm or norm == vendor.normalized_name:
        return None
    existing = db.scalar(select(VendorAlias).where(VendorAlias.normalized_alias == norm))
    if existing:
        existing.vendor_id = vendor.id
        return existing
    va = VendorAlias(vendor_id=vendor.id, alias=alias.strip(), normalized_alias=norm, created_by=actor)
    db.add(va)
    db.flush()
    return va


def suggest_vendors(db: Session, name: str | None, limit: int = 3) -> list[dict]:
    norm = normalize_vendor(name)
    if not norm:
        return []
    vendors = db.scalars(select(Vendor)).all()
    choices = {v.id: v.normalized_name for v in vendors}
    out = []
    for _, score, vid in process.extract(norm, choices, scorer=fuzz.WRatio, limit=limit):
        if score >= FUZZY_SUGGEST_MIN:
            v = db.get(Vendor, vid)
            out.append({"vendor_id": vid, "name": v.name, "score": round(score)})
    return out


def is_direct_purchase(db: Session, vendor_id: int, project_number: str | None) -> bool:
    return db.scalar(
        select(DirectPurchase.id).where(
            DirectPurchase.vendor_id == vendor_id,
            (DirectPurchase.project_number == project_number) | (DirectPurchase.project_number.is_(None)),
        )
    ) is not None


def subcontracts_for(db: Session, vendor_id: int, project_number: str) -> list[Subcontract]:
    from app.db.models import Project

    return list(
        db.scalars(
            select(Subcontract)
            .join(Project, Subcontract.project_id == Project.id)
            .where(
                Subcontract.vendor_id == vendor_id,
                Project.number == project_number,
                Subcontract.deleted_in_source.is_(False),
            )
        )
    )


def resolve_payable(db: Session, p: Payable) -> None:
    """Set vendor_id, subcontract_id and match_status on a payable."""
    p.match_candidates = None
    if p.manual_subcontract_id:
        sc = db.get(Subcontract, p.manual_subcontract_id)
        p.subcontract_id = sc.id
        p.vendor_id = sc.vendor_id
        p.match_status = "matched"
        return

    vendor = find_vendor(db, p.vendor_parsed)
    p.vendor_id = vendor.id if vendor else None
    p.subcontract_id = None
    if not vendor:
        p.match_status = "unmatched_vendor"
        p.match_candidates = suggest_vendors(db, p.vendor_parsed)
        return

    scs = subcontracts_for(db, vendor.id, p.project_number or "")
    if len(scs) == 1:
        p.subcontract_id = scs[0].id
        p.match_status = "matched"
    elif len(scs) > 1:
        p.match_status = "ambiguous"
        p.match_candidates = [
            {"subcontract_id": s.id, "amount": str(s.original_amount or ""), "cost_codes": s.cost_codes}
            for s in scs
        ]
    elif is_direct_purchase(db, vendor.id, p.project_number):
        p.match_status = "direct_purchase"
    else:
        p.match_status = "no_subcontract"


def resolve_change_order(db: Session, co) -> None:
    vendor = find_vendor(db, co.vendor_parsed)
    co.subcontract_id = None
    if not vendor:
        co.match_status = "unmatched_vendor"
        return
    scs = subcontracts_for(db, vendor.id, co.project_number or "")
    if len(scs) == 1:
        co.subcontract_id = scs[0].id
        co.match_status = "matched"
    else:
        co.match_status = "ambiguous" if scs else "no_subcontract"
