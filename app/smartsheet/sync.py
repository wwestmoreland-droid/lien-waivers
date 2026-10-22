"""Smartsheet -> local database sync.

Reads INTAKE and Archived Projects in full (so the project picker stays current) and
stores subcontracts, change orders and payables only for tracked projects. Status
changes on payables are recorded as payable_events; the waiver engine reads those, so
a missed or late sync never loses an event.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ctc.engine import compute_for_subcontract
from app.db.models import (
    ChangeOrder,
    CtcSnapshot,
    Payable,
    PayableEvent,
    Project,
    ReviewItem,
    Subcontract,
    SyncRun,
)
from app.matching import resolver
from app.matching.normalize import (
    clean_invoice_no,
    looks_like_retention_invoice,
    retainage_pct_from_terms,
    split_vendor_project,
    state_from_project_number,
    text,
    to_date,
    to_datetime,
    to_decimal,
)
from app.services.settings_store import get_setting
from app.smartsheet.client import SheetRow, SheetSource

log = structlog.get_logger()

PUBLIC_MAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "icloud.com", "aol.com", "hotmail.com",
    "live.com", "msn.com", "comcast.net", "att.net", "me.com", "ymail.com", "protonmail.com",
    "sbcglobal.net", "verizon.net", "mail.com",
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def _email(value) -> str | None:
    if isinstance(value, dict):
        return (value.get("email") or "").lower() or None
    if isinstance(value, list) and value:
        return _email(value[0])
    s = text(value)
    return s.lower() if s and "@" in s else None


def _domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    d = email.rsplit("@", 1)[1].strip().lower()
    return None if d in PUBLIC_MAIL_DOMAINS else d


def run_sync(db: Session, source: SheetSource, trigger: str = "schedule",
             backfill_projects: set[str] | None = None) -> SyncRun:
    run = SyncRun(trigger=trigger, rows_read={}, stats=defaultdict(int), errors=[])
    db.add(run)
    db.flush()
    stats: dict[str, int] = defaultdict(int)
    try:
        sheets = {k: source.read_sheet(k) for k in ("intake", "archived", "subcontracts", "change_orders", "ar_ap")}
        run.rows_read = {k: len(v) for k, v in sheets.items()}

        _sync_projects(db, sheets["intake"], sheets["archived"], stats)
        tracked = {p.number: p for p in db.scalars(select(Project).where(Project.is_tracked.is_(True)))}
        backfill = set(backfill_projects or ()) | {n for n, p in tracked.items() if p.backfilled_at is None}

        _sync_subcontracts(db, sheets["subcontracts"], tracked, stats)
        _sync_change_orders(db, sheets["change_orders"], tracked, stats)
        _sync_payables(db, sheets["ar_ap"], tracked, backfill, run, stats)
        _compute_ctc_and_reviews(db, tracked, stats)

        for number in backfill & tracked.keys():
            tracked[number].backfilled_at = now()

        run.status = "ok"
    except Exception as exc:  # recorded on the run, then re-raised for the caller's logs
        log.exception("sync.failed")
        run.status = "error"
        run.errors = [repr(exc)]
        run.stats = dict(stats)
        run.finished_at = now()
        db.commit()
        raise
    run.stats = dict(stats)
    run.finished_at = now()
    db.commit()
    log.info("sync.done", run_id=run.id, **run.stats)
    return run


# ---------------------------------------------------------------- projects

def _project_values(row: SheetRow, source_sheet: str) -> dict | None:
    v = row.values
    number = text(v.get("project_number"))
    if not number:
        return None
    number = number[:14]
    pm = v.get("project_manager")
    return {
        "number": number,
        "name": text(v.get("project_name")),
        "status": text(v.get("project_status")),
        "state": (text(v.get("state")) or state_from_project_number(number) or "")[:2] or None,
        "address": text(v.get("complete_address")),
        "client_company": text(v.get("client_company")),
        "owner": text(v.get("owner")),
        "pm_name": text(pm),
        "pm_email": _email(pm),
        "contract_amount": to_decimal(v.get("contract_amount")),
        "source_sheet": source_sheet,
        "source_row_id": row.row_id,
    }


def _sync_projects(db: Session, intake: list[SheetRow], archived: list[SheetRow], stats) -> None:
    seen: dict[str, dict] = {}
    for row in archived:
        vals = _project_values(row, "Archived")
        if vals:
            seen[vals["number"]] = vals
    for row in intake:  # INTAKE wins over Archived
        vals = _project_values(row, "INTAKE")
        if vals:
            seen[vals["number"]] = vals

    existing = {p.number: p for p in db.scalars(select(Project))}
    ts = now()
    for number, vals in seen.items():
        p = existing.get(number)
        if p is None:
            p = Project(number=number, first_seen_at=ts)
            db.add(p)
            stats["projects_new"] += 1
        for k, val in vals.items():
            setattr(p, k, val)
        p.last_seen_at = ts
        p.deleted_in_source = False
    for number, p in existing.items():
        if number not in seen and not p.deleted_in_source:
            p.deleted_in_source = True
            stats["projects_missing"] += 1
    db.flush()


# ---------------------------------------------------------------- subcontracts

def _sync_subcontracts(db: Session, rows: list[SheetRow], tracked: dict[str, Project], stats) -> None:
    counts: dict[str, list] = defaultdict(lambda: [0, Decimal("0")])
    projects = {p.number: p for p in db.scalars(select(Project))}
    ts = now()
    seen_ids: set[int] = set()

    for row in rows:
        v = row.values
        name = text(v.get("contractor_name"))
        number = (text(v.get("project")) or "")[:14]
        if not name or not number:
            continue
        amount = to_decimal(v.get("contract_amount")) or Decimal("0")
        counts[number][0] += 1
        counts[number][1] += amount

        project = tracked.get(number)
        if project is None:
            continue
        vendor = resolver.get_or_create_vendor(db, name)
        sc = db.scalar(select(Subcontract).where(Subcontract.source_row_id == row.row_id))
        if sc is None:
            sc = Subcontract(source_row_id=row.row_id, vendor_id=vendor.id, project_id=project.id,
                             contractor_name_raw=name)
            db.add(sc)
            stats["subcontracts_new"] += 1
        terms = text(v.get("payment_terms"))
        sc.vendor_id = vendor.id
        sc.project_id = project.id
        sc.contractor_name_raw = name
        sc.supplier_contractor = text(v.get("supplier_contractor"))
        sc.cost_codes = text(v.get("cost_code"))
        sc.original_amount = to_decimal(v.get("contract_amount"))
        sc.payment_terms = terms
        sc.retainage_pct = retainage_pct_from_terms(terms)
        sc.pm_name = text(v.get("pm_name"))
        sc.pm_email = _email(v.get("pm_email"))
        sc.estimator_name = text(v.get("estimator_name"))
        sc.estimator_email = _email(v.get("estimator_email"))
        sc.billing_name = text(v.get("billing_name"))
        sc.billing_email = _email(v.get("billing_email"))
        sc.billing_phone = text(v.get("billing_phone"))
        sc.foreman_name = text(v.get("foreman_name"))
        sc.foreman_email = _email(v.get("foreman_email"))
        sc.foreman_phone = text(v.get("foreman_phone"))
        sc.bp_pm_responsible = text(v.get("bp_pm_responsible"))
        sc.superintendent = text(v.get("superintendent_responsible"))
        sc.final_waiver_status_source = text(v.get("final_waiver"))
        sc.partial_waiver_amount_source = text(v.get("partial_waiver_amount"))
        sc.insurance_exp = to_date(v.get("insurance_exp"))
        sc.last_seen_at = ts
        sc.deleted_in_source = False
        seen_ids.add(row.row_id)

        # Fill vendor-level contact defaults and known domains from the subcontract.
        if not vendor.billing_email and sc.billing_email:
            vendor.billing_email = sc.billing_email
            vendor.billing_contact_name = sc.billing_name
            vendor.billing_phone = sc.billing_phone
        domains = set(vendor.email_domains or [])
        for e in (sc.billing_email, sc.pm_email, sc.estimator_email, sc.foreman_email):
            d = _domain(e)
            if d and d != "buildoutpros.com":
                domains.add(d)
        if domains != set(vendor.email_domains or []):
            vendor.email_domains = sorted(domains)
        if sc.is_material_vendor and vendor.type == "sub":
            vendor.type = "supplier"

    for number, p in projects.items():
        p.sub_count, p.sub_total = counts.get(number, [0, Decimal("0")])

    tracked_ids = [p.id for p in tracked.values()]
    if tracked_ids:
        for sc in db.scalars(select(Subcontract).where(Subcontract.project_id.in_(tracked_ids))):
            if sc.source_row_id not in seen_ids and not sc.deleted_in_source:
                sc.deleted_in_source = True
                stats["subcontracts_missing"] += 1
    db.flush()


# ---------------------------------------------------------------- change orders

def _sync_change_orders(db: Session, rows: list[SheetRow], tracked: dict[str, Project], stats) -> None:
    ts = now()
    seen_ids: set[int] = set()
    for row in rows:
        v = row.values
        number = (text(v.get("project_number")) or "")[:14]
        project = tracked.get(number)
        if project is None:
            continue
        co = db.scalar(select(ChangeOrder).where(ChangeOrder.source_row_id == row.row_id))
        if co is None:
            co = ChangeOrder(source_row_id=row.row_id)
            db.add(co)
            stats["change_orders_new"] += 1
        raw = text(v.get("contractor_and_project"))
        old_status = co.status
        co.project_number = number
        co.vendor_raw = raw
        co.vendor_parsed = split_vendor_project(raw, project.name)
        co.internal_external = text(v.get("internal_external"))
        co.status = text(v.get("approval"))
        total = to_decimal(v.get("total_cost"))
        co.amount = total if total is not None else to_decimal(v.get("requested_total"))
        co.cost_from_sub = to_decimal(v.get("cost_from_sub"))
        co.description = text(v.get("description"))
        co.created_in_source = to_datetime(v.get("created"))
        if (co.status or "").lower() == "approved" and (old_status or "").lower() != "approved":
            co.approved_seen_at = ts
        co.last_seen_at = ts
        co.deleted_in_source = False
        seen_ids.add(row.row_id)
        if co.is_internal:
            resolver.resolve_change_order(db, co)
        else:
            co.match_status = "external"
            co.subcontract_id = None

    for co in db.scalars(select(ChangeOrder).where(ChangeOrder.project_number.in_(list(tracked)))):
        if co.source_row_id not in seen_ids and not co.deleted_in_source:
            co.deleted_in_source = True
            stats["change_orders_missing"] += 1
    db.flush()


# ---------------------------------------------------------------- payables

def _record_event(db: Session, payable: Payable, kind: str, value, backfill: bool, run: SyncRun, stats) -> None:
    exists = db.scalar(select(PayableEvent.id).where(PayableEvent.payable_id == payable.id, PayableEvent.kind == kind))
    if exists:
        return
    try:
        with db.begin_nested():
            db.add(PayableEvent(payable_id=payable.id, kind=kind, value=str(value) if value is not None else None,
                                from_backfill=backfill, sync_run_id=run.id))
        stats[f"events_{kind}{'_backfill' if backfill else ''}"] += 1
    except IntegrityError:
        pass


def _sync_payables(db: Session, rows: list[SheetRow], tracked: dict[str, Project], backfill: set[str],
                   run: SyncRun, stats) -> None:
    ts = now()
    seen_ids: set[int] = set()
    other_names = [p.name for p in tracked.values() if p.name]
    for row in rows:
        v = row.values
        if (text(v.get("receivables_payables")) or "").lower() != "payables":
            continue
        number = (text(v.get("project_number")) or "")[:14]
        project = tracked.get(number)
        if project is None:
            continue
        stats["payables_read"] += 1
        p = db.scalar(select(Payable).where(Payable.source_row_id == row.row_id))
        is_new = p is None
        if is_new:
            p = Payable(source_row_id=row.row_id, first_seen_at=ts)
            db.add(p)
            stats["payables_new"] += 1
        old_approval = (p.approval_status or "").lower()
        old_paid = p.paid_date

        raw = text(v.get("vendor_and_project"))
        p.project_number = number
        p.vendor_raw = raw
        p.vendor_parsed = split_vendor_project(raw, project.name, other_names)
        p.invoice_no = clean_invoice_no(v.get("invoice_no"))
        p.amount = to_decimal(v.get("amount"))
        p.date_received = to_date(v.get("date_received"))
        p.approval_status = text(v.get("approval"))
        p.approval_date = to_date(v.get("approval_date"))
        p.approved_by = text(v.get("approved_by"))
        p.paid_date = to_date(v.get("paid_date"))
        p.entered_in_qb = bool(v.get("entered_in_qb"))
        p.cost_code = text(v.get("cost_code"))
        p.material_sub_other = text(v.get("material_sub_other"))
        p.waiver_created_source = text(v.get("waiver_created"))
        p.is_retention_auto = looks_like_retention_invoice(p.invoice_no, p.cost_code)
        p.last_seen_at = ts
        p.deleted_in_source = False
        seen_ids.add(row.row_id)
        db.flush()

        resolver.resolve_payable(db, p)

        is_backfill = number in backfill
        new_approval = (p.approval_status or "").lower()
        if new_approval != old_approval or is_new:
            if new_approval == "approved":
                _record_event(db, p, "approved", p.approval_date, is_backfill, run, stats)
            elif new_approval == "hold":
                _record_event(db, p, "held", p.approval_date, is_backfill, run, stats)
            elif new_approval == "rejected":
                _record_event(db, p, "rejected", p.approval_date, is_backfill, run, stats)
        if p.paid_date and not old_paid:
            _record_event(db, p, "paid", p.paid_date, is_backfill, run, stats)

    tracked_numbers = list(tracked)
    if tracked_numbers:
        for p in db.scalars(select(Payable).where(Payable.project_number.in_(tracked_numbers))):
            if p.source_row_id not in seen_ids and not p.deleted_in_source:
                p.deleted_in_source = True
                stats["payables_missing"] += 1
        # Re-resolve rows whose match may have changed (new alias, new subcontract).
        for p in db.scalars(select(Payable).where(Payable.project_number.in_(tracked_numbers),
                                                  Payable.match_status != "matched")):
            resolver.resolve_payable(db, p)
    db.flush()


# ---------------------------------------------------------------- CTC + review items

def upsert_review(db: Session, kind: str, ref_type: str, ref_id: int, project_number: str | None,
                  message: str, active: bool, stats) -> None:
    item = db.scalar(select(ReviewItem).where(ReviewItem.kind == kind, ReviewItem.ref_type == ref_type,
                                              ReviewItem.ref_id == ref_id))
    if active:
        if item is None:
            db.add(ReviewItem(kind=kind, ref_type=ref_type, ref_id=ref_id, project_number=project_number,
                              message=message, assignee_email=get_setting(db, "default_reviewer_email")))
            stats[f"review_{kind}_new"] += 1
        elif item.status == "resolved" and item.resolved_by == "system:sync":
            item.status = "open"
            item.resolved_by = None
            item.resolved_at = None
            item.message = message
        elif item.status == "open":
            item.message = message
    elif item is not None and item.status == "open":
        item.status = "resolved"
        item.resolved_by = "system:sync"
        item.resolved_at = now()


def _compute_ctc_and_reviews(db: Session, tracked: dict[str, Project], stats) -> None:
    db.expire_all()  # reload relationships after the inserts above
    today = date.today()
    for project in tracked.values():
        for sc in project.subcontracts:
            if sc.deleted_in_source:
                continue
            c = compute_for_subcontract(sc)
            snap = db.scalar(select(CtcSnapshot).where(CtcSnapshot.subcontract_id == sc.id,
                                                       CtcSnapshot.snapshot_date == today))
            if snap is None:
                snap = CtcSnapshot(subcontract_id=sc.id, snapshot_date=today)
                db.add(snap)
            for f in ("original_contract", "approved_cos", "pending_cos", "revised_contract", "billed_to_date",
                      "paid_to_date", "hold_total", "retention_billed", "retention_owed", "cost_to_complete",
                      "pct_complete", "status"):
                setattr(snap, f, getattr(c, f))
            stats["ctc_computed"] += 1

            upsert_review(db, "over_billed", "subcontract", sc.id, project.number,
                          f"{sc.vendor.name} is over-billed by ${-c.cost_to_complete:,.2f} on {project.name}.",
                          c.status == "over_billed", stats)
            upsert_review(db, "no_contract", "subcontract", sc.id, project.number,
                          f"{sc.vendor.name} on {project.name} has a contract amount of 0 or blank.",
                          c.status == "no_contract", stats)

        for co in db.scalars(select(ChangeOrder).where(ChangeOrder.project_number == project.number,
                                                       ChangeOrder.deleted_in_source.is_(False))):
            upsert_review(db, "co_amount_mismatch", "change_order", co.id, project.number,
                          f"Internal CO for {co.vendor_parsed}: Cost From Subcontractor "
                          f"${co.cost_from_sub or 0:,.2f} differs from Total Cost ${co.amount or 0:,.2f}.",
                          co.amount_mismatch, stats)
    db.flush()
