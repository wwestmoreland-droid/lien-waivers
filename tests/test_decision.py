"""Every row of the section 5.3 decision table."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal as D

import pytest

from app.ctc.engine import compute_ctc
from app.waivers.decision import Flags, PayableFacts, SubFacts, decide
from app.waivers.types import WaiverType as W


@dataclass
class P:
    source_row_id: int
    amount: D
    date_received: date
    approval_status: str = "Approved"
    paid_date: date | None = None
    is_retention_invoice: bool = False
    deleted_in_source: bool = False


def ctc_after(contract, *amounts, retention_last=False, retainage=None):
    rows = [P(i, D(a), date(2026, 1, i + 1)) for i, a in enumerate(amounts)]
    if retention_last:
        rows[-1].is_retention_invoice = True
    return compute_ctc(D(contract) if contract is not None else None, retainage, [], rows, as_of=rows[-1]), rows[-1]


def facts(row, paid=False, status="Approved", match="matched"):
    return PayableFacts(match_status=match, approval_status=status, is_paid=paid,
                        is_retention_invoice=row.is_retention_invoice, invoice_no="7")


NO_RET, RET = SubFacts(False), SubFacts(True)


def test_approved_partial_flag_off_waits_for_payment():
    c, r = ctc_after(1000, 400)
    d = decide("approved", facts(r), c, NO_RET, Flags())
    assert d.waiver is None and not d.reviews


def test_approved_partial_flag_on_conditional_partial():
    c, r = ctc_after(1000, 400)
    assert decide("approved", facts(r), c, NO_RET, Flags(conditional_partial_enabled=True)).waiver == W.CONDITIONAL_PARTIAL


def test_approved_final_conditional_final():
    c, r = ctc_after(1000, 600, 400)
    assert decide("approved", facts(r), c, NO_RET, Flags()).waiver == W.CONDITIONAL_FINAL


def test_retention_sub_final_on_retention_invoice():
    c, r = ctc_after(1000, 900, 100, retention_last=True, retainage=D(10))
    assert decide("approved", facts(r), c, RET, Flags()).waiver == W.CONDITIONAL_FINAL


def test_retention_sub_hits_zero_on_non_retention_invoice_goes_to_review():
    c, r = ctc_after(1000, 600, 400, retainage=D(10))
    d = decide("approved", facts(r), c, RET, Flags())
    assert d.waiver is None and d.reviews[0].kind == "retention_check"
    d = decide("paid", facts(r, paid=True), c, RET, Flags())
    assert d.waiver is None and d.reviews[0].kind == "retention_check"


def test_paid_partial_unconditional_partial():
    c, r = ctc_after(1000, 400)
    assert decide("paid", facts(r, paid=True), c, NO_RET, Flags()).waiver == W.UNCONDITIONAL_PARTIAL


def test_paid_final_unconditional_final():
    c, r = ctc_after(1000, 600, 400)
    assert decide("paid", facts(r, paid=True), c, NO_RET, Flags()).waiver == W.UNCONDITIONAL_FINAL


@pytest.mark.parametrize("event", ["approved", "paid"])
def test_over_billed_goes_to_review(event):
    c, r = ctc_after(1000, 600, 500)
    d = decide(event, facts(r, paid=event == "paid"), c, NO_RET, Flags())
    assert d.waiver is None and d.reviews[0].kind == "over_billed"


def test_no_contract_is_review_not_final():
    c, r = ctc_after(None, 500)
    d = decide("paid", facts(r, paid=True), c, NO_RET, Flags())
    assert d.waiver is None and d.reviews[0].kind == "no_contract"


def test_hold_goes_to_review():
    c, r = ctc_after(1000, 400)
    d = decide("approved", facts(r, status="Hold"), c, NO_RET, Flags())
    assert d.waiver is None and d.reviews[0].kind == "hold"


def test_rejected_gets_nothing():
    c, r = ctc_after(1000, 400)
    d = decide("approved", facts(r, status="Rejected"), c, NO_RET, Flags())
    assert d.waiver is None and not d.reviews


def test_unmatched_is_deferred_not_dropped():
    c, r = ctc_after(1000, 400)
    assert decide("paid", facts(r, paid=True, match="unmatched_vendor"), c, NO_RET, Flags()).defer


def test_direct_purchase_needs_no_waiver():
    c, r = ctc_after(1000, 400)
    d = decide("paid", facts(r, paid=True, match="direct_purchase"), c, NO_RET, Flags())
    assert d.waiver is None and not d.defer


def test_same_day_approve_and_pay_only_unconditional():
    c, r = ctc_after(1000, 600, 400)
    assert decide("approved", facts(r, paid=True), c, NO_RET, Flags()).waiver is None
    assert decide("paid", facts(r, paid=True), c, NO_RET, Flags()).waiver == W.UNCONDITIONAL_FINAL


def test_old_partial_stays_partial_after_later_invoices():
    """Point-in-time: an early invoice isn't 'final' just because later invoices finished the job."""
    rows = [P(1, D(600), date(2026, 1, 1), paid_date=date(2026, 2, 1)), P(2, D(400), date(2026, 1, 15))]
    c = compute_ctc(D(1000), None, [], rows, as_of=rows[0])
    assert decide("paid", facts(rows[0], paid=True), c, NO_RET, Flags()).waiver == W.UNCONDITIONAL_PARTIAL
