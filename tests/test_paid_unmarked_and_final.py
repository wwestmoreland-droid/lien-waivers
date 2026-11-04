"""Paid rows nobody marked Approved still count; an unconditional final on file stops new waivers."""
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.ctc.engine import compute_for_subcontract
from app.db.models import Payable, Subcontract, Waiver
from app.smartsheet.sync import run_sync
from app.waivers import service as waivers
from app.waivers.types import WaiverStatus, WaiverType
from tests.fixtures.sheets import PROJECT, PROJECT_NAME, _row
from tests.test_email_e2e import mail, pay  # noqa: F401


def delta(db):
    return next(s for s in db.scalars(select(Subcontract)) if s.vendor.name.startswith("Delta")
                and s.project.number == PROJECT)


def add_rows(mail, *rows):
    for i, (amount, paid, approval) in enumerate(rows):
        v = dict(receivables_payables="Payables", project_number=PROJECT, invoice_no=f"Check #{4427 + i}",
                 vendor_and_project=f"Delta Fire Systems, Inc - {PROJECT_NAME}", amount=amount,
                 date_received=paid, paid_date=paid)
        if approval:
            v["approval"] = approval
        mail.source.sheets["ar_ap"].append(_row(3001 + i, **v))
    run_sync(mail.db, mail.source, trigger="test")
    mail.db.commit()
    mail.db.expire_all()


def test_paid_rows_with_a_blank_approval_count_as_billed(mail):
    """Like rows 1745-1783: entered after the fact with a paid date and no Approved/Rejected."""
    db = mail.db
    before = compute_for_subcontract(delta(db))
    add_rows(mail, (1885.0, "2026-06-08", None), (1875.0, "2026-06-19", None), (999.0, "2026-06-20", "Rejected"))
    after = compute_for_subcontract(delta(db))
    assert after.billed_to_date == before.billed_to_date + Decimal("3760.00")  # rejected still doesn't count
    assert after.cost_to_complete == before.cost_to_complete - Decimal("3760.00")
    assert after.paid_to_date == before.paid_to_date + Decimal("3760.00")


def test_no_waivers_after_an_unconditional_final_by_hand(mail):
    db = mail.db
    sc = delta(db)
    uf = waivers.create_manual_waiver(db, sc, WaiverType.UNCONDITIONAL_FINAL, None, date(2026, 11, 2), "ap",
                                      paid_date=date(2026, 11, 2))
    db.commit()
    add_rows(mail, (500.0, "2026-11-03", "Approved"))
    w = pay(mail)  # Pay App 2 paid too
    assert w is None
    new = db.scalars(select(Waiver).join(Payable).where(Payable.source_row_id == 3001)).all()
    assert new == []
    assert db.get(Waiver, uf.id).status != WaiverStatus.VOID


def test_a_final_by_hand_cancels_unsent_drafts(mail):
    db = mail.db
    sc = delta(db)
    draft = pay(mail)  # the app's unconditional partial for Pay App 2, not sent
    assert draft.status == WaiverStatus.DRAFT
    uf = waivers.create_manual_waiver(db, sc, WaiverType.UNCONDITIONAL_FINAL, Decimal("1"), date(2026, 11, 2), "ap",
                                      paid_date=date(2026, 11, 2))
    db.commit()
    db.expire_all()
    assert db.get(Waiver, draft.id).status == WaiverStatus.VOID
    assert db.get(Waiver, uf.id).status == WaiverStatus.DRAFT and db.get(Waiver, uf.id).amount is None
