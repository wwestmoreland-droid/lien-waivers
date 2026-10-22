from decimal import Decimal as D

from sqlalchemy import func, select

from app.ctc.engine import compute_for_subcontract
from app.db.models import AuditLog, ChangeOrder, Payable, PayableEvent, Project, ReviewItem, Subcontract, Vendor
from app.matching import resolver
from app.services.projects import picker_label, picker_projects, set_tracked
from app.smartsheet.sync import run_sync
from tests.fixtures.sheets import OTHER_PROJECT, PROJECT


def track(db, number, actor="tester@buildoutpros.com"):
    p = db.scalar(select(Project).where(Project.number == number))
    set_tracked(db, p, True, actor)
    db.commit()
    return p


def payable(db, row_id) -> Payable:
    return db.scalar(select(Payable).where(Payable.source_row_id == row_id))


def test_picker_shows_only_status_7_8_9_and_untracked_stores_nothing(db, source):
    run_sync(db, source, trigger="test")
    numbers = [p.number for p in picker_projects(db)]
    assert numbers == [OTHER_PROJECT, PROJECT]
    p = db.scalar(select(Project).where(Project.number == PROJECT))
    assert picker_label(p) == "New – not tracked"
    assert p.sub_count == 6 and p.sub_total == D("454044.70")
    assert db.scalar(select(func.count(Payable.id))) == 0
    assert db.scalar(select(func.count(Subcontract.id))) == 0


def test_tracking_is_audited(db, source):
    run_sync(db, source, trigger="test")
    track(db, PROJECT)
    log = db.scalars(select(AuditLog).where(AuditLog.action == "project.tracked")).all()
    assert len(log) == 1 and log[0].entity_id == PROJECT


def test_tracked_project_sync_and_matching(db, source):
    run_sync(db, source, trigger="test")
    track(db, PROJECT)
    run_sync(db, source, trigger="test")

    # Receivables and other (untracked) projects are not stored.
    assert db.scalar(select(func.count(Payable.id))) == 10
    assert payable(db, 1011) is None and payable(db, 1012) is None

    assert payable(db, 1001).match_status == "matched"
    # Hyphenated vendor name resolves.
    assert payable(db, 1008).match_status == "matched"
    assert payable(db, 1008).vendor.name == "Tri-State Glass - Mirror, Inc."
    # Known spelling variant resolves through the seed alias.
    assert payable(db, 1009).match_status == "matched"
    # A vendor with no subcontract goes to the Unmatched queue, not silently skipped.
    assert payable(db, 1010).match_status == "unmatched_vendor"
    # Invoice float cleaned.
    assert payable(db, 1006).invoice_no == "2379"

    sc = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 101))
    c = compute_for_subcontract(sc)
    assert c.revised_contract == D("34423.70")
    assert c.cost_to_complete == D("4148.60")

    summit = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 103))
    assert summit.retainage_pct == D("10")
    # Public mail domains are never recorded as vendor domains.
    assert summit.vendor.email_domains in (None, [])
    assert db.scalar(select(Vendor).where(Vendor.name == "Delta Fire Systems, Inc")).email_domains == ["deltafire.com"]


def test_review_items(db, source):
    run_sync(db, source, trigger="test")
    track(db, PROJECT)
    run_sync(db, source, trigger="test")
    kinds = {(r.kind, r.status) for r in db.scalars(select(ReviewItem))}
    assert ("no_contract", "open") in kinds  # Zero Dollar Signs
    assert ("co_amount_mismatch", "open") in kinds  # Ridge pending CO 4200 vs 4500
    item = db.scalar(select(ReviewItem).where(ReviewItem.kind == "no_contract"))
    assert item.assignee_email == "wwestmoreland@buildoutpros.com"


def test_first_sync_after_tracking_is_backfill_and_resync_is_idempotent(db, source):
    run_sync(db, source, trigger="test")
    track(db, PROJECT)
    run_sync(db, source, trigger="test")
    events = db.scalars(select(PayableEvent)).all()
    assert events and all(e.from_backfill for e in events)
    count = len(events)

    run_sync(db, source, trigger="test")
    assert db.scalar(select(func.count(PayableEvent.id))) == count

    # A real transition after backfill: Summit invoice moves Hold -> Approved, then is paid.
    source.row("ar_ap", 1007).values["approval"] = "Approved"
    run_sync(db, source, trigger="test")
    ev = db.scalar(select(PayableEvent).where(PayableEvent.payable_id == payable(db, 1007).id,
                                              PayableEvent.kind == "approved"))
    assert ev is not None and not ev.from_backfill

    source.row("ar_ap", 1007).values["paid_date"] = "2026-10-22"
    run_sync(db, source, trigger="test")
    run_sync(db, source, trigger="test")
    paid = db.scalars(select(PayableEvent).where(PayableEvent.payable_id == payable(db, 1007).id,
                                                 PayableEvent.kind == "paid")).all()
    assert len(paid) == 1 and not paid[0].from_backfill


def test_same_day_approve_and_pay_records_both_events(db, source):
    run_sync(db, source, trigger="test")
    track(db, PROJECT)
    run_sync(db, source, trigger="test")
    row = source.row("ar_ap", 1003)
    row.values["approval"] = None
    run_sync(db, source, trigger="test")
    db.execute(PayableEvent.__table__.delete().where(PayableEvent.payable_id == payable(db, 1003).id))
    db.commit()
    row.values["approval"] = "Approved"
    row.values["paid_date"] = "2026-10-22"
    run_sync(db, source, trigger="test")
    kinds = {e.kind for e in db.scalars(select(PayableEvent).where(PayableEvent.payable_id == payable(db, 1003).id))}
    assert kinds == {"approved", "paid"}


def test_alias_mapping_resolves_unmatched_row(db, source):
    run_sync(db, source, trigger="test")
    track(db, PROJECT)
    source.row("ar_ap", 1004).values["vendor_and_project"] = "Ridge Plumbing & Heating"
    run_sync(db, source, trigger="test")
    assert payable(db, 1004).match_status == "unmatched_vendor"
    assert payable(db, 1004).match_candidates[0]["name"] == "Ridge Plumbing"

    ridge = db.scalar(select(Vendor).where(Vendor.name == "Ridge Plumbing"))
    resolver.add_alias(db, ridge, "Ridge Plumbing & Heating", actor="tester")
    db.commit()
    run_sync(db, source, trigger="test")
    assert payable(db, 1004).match_status == "matched"


def test_removed_source_row_is_flagged_not_deleted(db, source):
    run_sync(db, source, trigger="test")
    track(db, PROJECT)
    run_sync(db, source, trigger="test")
    source.sheets["ar_ap"] = [r for r in source.sheets["ar_ap"] if r.row_id != 1005]
    run_sync(db, source, trigger="test")
    assert payable(db, 1005).deleted_in_source


def test_external_cos_not_resolved(db, source):
    run_sync(db, source, trigger="test")
    track(db, PROJECT)
    run_sync(db, source, trigger="test")
    ext = db.scalar(select(ChangeOrder).where(ChangeOrder.source_row_id == 303))
    assert ext.match_status == "external" and ext.subcontract_id is None
