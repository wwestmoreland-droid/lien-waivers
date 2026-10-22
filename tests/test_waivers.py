from datetime import date
from pathlib import Path

import pytest
import yaml
from pypdf import PdfReader
from sqlalchemy import func, select

from app.db.models import Payable, Project, ReviewItem, Waiver, WaiverEvent
from app.services.projects import set_tracked
from app.smartsheet.client import SheetRow
from app.smartsheet.sync import run_sync
from app.waivers import service
from app.waivers.types import WaiverStatus, WaiverType as W
from tests.fixtures.sheets import OTHER_NAME, OTHER_PROJECT, PROJECT, PROJECT_NAME


@pytest.fixture
def tracked(db, source):
    run_sync(db, source, trigger="test")
    for n in (PROJECT, OTHER_PROJECT):
        set_tracked(db, db.scalar(select(Project).where(Project.number == n)), True, "t")
    db.commit()
    run_sync(db, source, trigger="test")  # backfill
    return source


def add_ap(source, row_id, vendor, amount, received, approval="Approved", paid=None, invoice=None,
           project=PROJECT, project_name=PROJECT_NAME):
    source.sheets["ar_ap"].append(SheetRow(row_id, {
        "receivables_payables": "Payables", "project_number": project, "invoice_no": invoice or f"INV-{row_id}",
        "vendor_and_project": f"{vendor} - {project_name}", "amount": amount, "date_received": received,
        "approval": approval, "paid_date": paid}, {}))


def waivers(db, row_id=None):
    q = select(Waiver)
    if row_id:
        q = q.join(Payable).where(Payable.source_row_id == row_id)
    return db.scalars(q.order_by(Waiver.id)).all()


def sync(db, source):
    run_sync(db, source, trigger="test")
    db.expire_all()


def test_backfill_creates_no_waivers(db, tracked):
    assert waivers(db) == []


def test_paid_partial_creates_unconditional_partial_with_pdf(db, tracked):
    tracked.row("ar_ap", 1003).values["paid_date"] = "2026-10-22"  # Delta Pay App 2, CTC after 4,148.60
    sync(db, tracked)
    [w] = waivers(db, 1003)
    assert w.type == W.UNCONDITIONAL_PARTIAL and w.status == WaiverStatus.DRAFT
    assert str(w.amount) == "627.30" and w.through_date == date(2026, 10, 31) and w.through_date_is_fallback
    assert Path(w.generated_pdf_path).exists()
    assert len(PdfReader(w.generated_pdf_path).pages) == 1
    sync(db, tracked)
    assert len(waivers(db)) == 1  # idempotent


def test_final_cycle_with_pending_co_exceptions(db, tracked):
    add_ap(tracked, 2001, "Ridge Plumbing", 59300.0, "2026-10-20")  # Ridge to $0 CTC; has a pending CO
    sync(db, tracked)
    [cf] = waivers(db, 2001)
    assert cf.type == W.CONDITIONAL_FINAL and str(cf.amount) == "59300.00"
    assert "Pending CO: Reroute line ($4,500.00)" in cf.exceptions_text
    assert db.scalar(select(ReviewItem).where(ReviewItem.kind == "final_exceptions", ReviewItem.ref_id == cf.id))

    tracked.row("ar_ap", 2001).values["paid_date"] = "2026-10-25"
    sync(db, tracked)
    types = [w.type for w in waivers(db, 2001)]
    assert types == [W.CONDITIONAL_FINAL, W.UNCONDITIONAL_FINAL]
    uf = waivers(db, 2001)[1]
    assert uf.amount is None


def test_same_day_approve_and_pay(db, tracked):
    add_ap(tracked, 2002, "Ridge Plumbing", 59300.0, "2026-10-20", paid="2026-10-21")
    sync(db, tracked)
    assert [w.type for w in waivers(db, 2002)] == [W.UNCONDITIONAL_FINAL]


def test_retention_check_then_marked_as_retention_invoice(db, tracked):
    # Summit: 10% retention sub, 148,720 contract, 80,000 billed. Bill the rest on a normal invoice.
    add_ap(tracked, 2003, "Summit Electric, LLC", 68720.0, "2026-10-20")
    sync(db, tracked)
    assert waivers(db, 2003) == []
    p = db.scalar(select(Payable).where(Payable.source_row_id == 2003))
    item = db.scalar(select(ReviewItem).where(ReviewItem.kind == "retention_check", ReviewItem.ref_id == p.id))
    assert item.status == "open"

    p.is_retention_override = True
    service.reprocess_payable(db, p, "tester")
    db.commit()
    db.expire_all()
    assert [w.type for w in waivers(db, 2003)] == [W.CONDITIONAL_FINAL]
    assert db.get(ReviewItem, item.id).status == "resolved"


def test_over_billed_payable_goes_to_review(db, tracked):
    add_ap(tracked, 2004, "Delta Fire Systems, Inc", 9000.0, "2026-11-01", paid="2026-11-02")
    sync(db, tracked)
    assert waivers(db, 2004) == []
    p = db.scalar(select(Payable).where(Payable.source_row_id == 2004))
    assert db.scalar(select(ReviewItem).where(ReviewItem.kind == "over_billed", ReviewItem.ref_type == "payable",
                                              ReviewItem.ref_id == p.id))


def test_no_contract_payable_goes_to_review(db, tracked):
    add_ap(tracked, 2005, "Zero Dollar Signs", 500.0, "2026-10-20", paid="2026-10-21")
    sync(db, tracked)
    assert waivers(db, 2005) == []
    p = db.scalar(select(Payable).where(Payable.source_row_id == 2005))
    assert db.scalar(select(ReviewItem).where(ReviewItem.kind == "no_contract", ReviewItem.ref_id == p.id))


def test_hold_payable_goes_to_review_and_closes_when_approved(db, tracked):
    add_ap(tracked, 2006, "Ridge Plumbing", 1000.0, "2026-10-20", approval="Hold")
    sync(db, tracked)
    p = db.scalar(select(Payable).where(Payable.source_row_id == 2006))
    item = db.scalar(select(ReviewItem).where(ReviewItem.kind == "hold", ReviewItem.ref_id == p.id))
    assert item.status == "open"
    tracked.row("ar_ap", 2006).values["approval"] = "Approved"
    sync(db, tracked)
    assert db.get(ReviewItem, item.id).status == "resolved"


def test_unmatched_event_waits_then_processes_after_mapping(db, tracked):
    from app.matching import resolver
    from app.db.models import Vendor
    add_ap(tracked, 2007, "Ridge Plumbing & Heating", 1000.0, "2026-10-20", paid="2026-10-21")
    sync(db, tracked)
    assert waivers(db, 2007) == []
    resolver.add_alias(db, db.scalar(select(Vendor).where(Vendor.name == "Ridge Plumbing")),
                       "Ridge Plumbing & Heating", "tester")
    db.commit()
    sync(db, tracked)
    assert [w.type for w in waivers(db, 2007)] == [W.UNCONDITIONAL_PARTIAL]


def test_statutory_state_blocks_until_template_added(db, tracked, tmp_path, monkeypatch):
    add_ap(tracked, 2008, "Valley Drywall", 5000.0, "2026-10-20", paid="2026-10-21",
           project=OTHER_PROJECT, project_name=OTHER_NAME)
    sync(db, tracked)
    [w] = waivers(db, 2008)
    assert w.status == WaiverStatus.NEEDS_REVIEW and w.state == "CA"
    assert w.review_reason == "Statutory form required for CA; add template."
    assert w.generated_pdf_path is None
    assert db.scalar(select(ReviewItem).where(ReviewItem.kind == "statutory_form_missing", ReviewItem.ref_id == w.id))

    # Register a CA form (test stand-in) and recheck.
    reg_dir = tmp_path / "pdf"
    (reg_dir / "default").mkdir(parents=True)
    src = service.registry().templates["default/unconditional_partial"].file
    for f in src.parent.glob("*.pdf"):
        (reg_dir / "default" / f.name).write_bytes(f.read_bytes())
    data = yaml.safe_load((src.parent.parent / "registry.yaml").read_text())
    data["templates"]["CA/unconditional_partial"] = dict(data["templates"]["default/unconditional_partial"], state="CA")
    (reg_dir / "registry.yaml").write_text(yaml.safe_dump(data))
    from app.waivers import registry as reg_mod
    monkeypatch.setattr(service, "registry", lambda: reg_mod.load_registry(reg_dir / "registry.yaml"))
    assert service.recheck_blocked(db) == 1
    db.commit()
    assert w.status == WaiverStatus.DRAFT and w.template_key == "CA/unconditional_partial"


def test_through_date_edit_regenerates_and_locks_after_send(db, tracked):
    tracked.row("ar_ap", 1003).values["paid_date"] = "2026-10-22"
    sync(db, tracked)
    [w] = waivers(db, 1003)
    service.set_through_date(db, w, date(2026, 10, 15), "tester")
    db.commit()
    assert w.pdf_version == 2 and not w.through_date_is_fallback
    assert w.payable.billed_through_date == date(2026, 10, 15)
    service.mark_sent(db, w, "tester")
    with pytest.raises(ValueError):
        service.set_through_date(db, w, date(2026, 10, 1), "tester")


def test_received_accept_and_history_is_append_only(db, tracked):
    tracked.row("ar_ap", 1003).values["paid_date"] = "2026-10-22"
    sync(db, tracked)
    [w] = waivers(db, 1003)
    service.mark_sent(db, w, "t")
    service.mark_received(db, w, "t", "signed.pdf", b"%PDF-1.4 test")
    service.reject(db, w, "t", "it isn't signed")
    service.mark_received(db, w, "t", "signed2.pdf", b"%PDF-1.4 test")
    service.accept(db, w, "t")
    db.commit()
    actions = [e.action for e in db.scalars(select(WaiverEvent).where(WaiverEvent.waiver_id == w.id)
                                             .order_by(WaiverEvent.id))]
    assert actions == ["created", "pdf_generated", "sent", "received", "rejected", "received", "accepted"]
    assert w.status == WaiverStatus.ACCEPTED


def test_expected_waivers_for_backfill_report(db, tracked):
    p = db.scalar(select(Payable).where(Payable.source_row_id == 1001))  # Delta Pay App 1, paid, CTC>0
    assert service.expected_waiver_types(db, p) == [W.UNCONDITIONAL_PARTIAL]
    assert db.scalar(select(func.count(Waiver.id))) == 0
