from dataclasses import dataclass
from datetime import date
from decimal import Decimal as D

from app.ctc.engine import compute_ctc


@dataclass
class P:
    source_row_id: int
    amount: D
    date_received: date | None
    approval_status: str | None = "Approved"
    paid_date: date | None = None
    is_retention_invoice: bool = False
    deleted_in_source: bool = False


@dataclass
class CO:
    amount: D
    status: str
    is_internal: bool = True
    deleted_in_source: bool = False


def test_basic_ctc_with_approved_internal_cos_only():
    cos = [CO(D("1663"), "Approved"), CO(D("500"), "Pending"), CO(D("9000"), "Approved", is_internal=False),
           CO(D("100"), "Declined")]
    pays = [P(1, D("29647.80"), date(2026, 9, 30), paid_date=date(2026, 10, 16)),
            P(2, D("1663"), date(2026, 9, 4), approval_status="Rejected"),
            P(3, D("627.30"), date(2026, 10, 31))]
    c = compute_ctc(D("32760.70"), None, cos, pays)
    assert c.revised_contract == D("34423.70")
    assert c.pending_cos == D("500")
    assert c.billed_to_date == D("30275.10")
    assert c.paid_to_date == D("29647.80")
    assert c.cost_to_complete == D("4148.60")
    assert c.status == "ok"
    assert c.pct_complete == D("0.8795")


def test_point_in_time_ignores_later_rows():
    pays = [P(1, D("60000"), date(2026, 7, 1)), P(2, D("40000"), date(2026, 8, 1))]
    early = compute_ctc(D("100000"), None, [], pays, as_of=pays[0])
    assert early.cost_to_complete == D("40000")
    full = compute_ctc(D("100000"), None, [], pays)
    assert full.cost_to_complete == D("0")
    assert full.status == "complete"


def test_point_in_time_tiebreak_on_row_id():
    pays = [P(9, D("10"), date(2026, 7, 1)), P(3, D("20"), date(2026, 7, 1))]
    c = compute_ctc(D("100"), None, [], pays, as_of=pays[1])  # row 3 comes first
    assert c.billed_to_date == D("20")


def test_as_of_payable_counts_even_before_status_catches_up():
    target = P(2, D("40"), date(2026, 8, 1), approval_status=None)
    c = compute_ctc(D("100"), None, [], [P(1, D("60"), date(2026, 7, 1)), target], as_of=target)
    assert c.cost_to_complete == D("0")


def test_hold_and_rejected_excluded_hold_reported():
    pays = [P(1, D("80000"), date(2026, 8, 19)), P(2, D("20000"), date(2026, 9, 21), approval_status="Hold"),
            P(3, D("5000"), date(2026, 9, 22), approval_status="Rejected")]
    c = compute_ctc(D("148720"), D("10"), [], pays)
    assert c.billed_to_date == D("80000")
    assert c.hold_total == D("20000")
    assert c.cost_to_complete == D("68720")


def test_retention():
    pays = [P(1, D("90000"), date(2026, 8, 1)), P(2, D("10000"), date(2026, 9, 1), is_retention_invoice=True)]
    before = compute_ctc(D("100000"), D("10"), [], pays, as_of=pays[0])
    assert before.retention_owed == D("10000")
    assert before.cost_to_complete == D("10000")
    after = compute_ctc(D("100000"), D("10"), [], pays)
    assert after.retention_billed == D("10000")
    assert after.retention_owed == D("0")
    assert after.cost_to_complete == D("0")


def test_over_billed():
    c = compute_ctc(D("1000"), None, [], [P(1, D("1200"), date(2026, 1, 1))])
    assert c.status == "over_billed"
    assert c.cost_to_complete == D("-200")


def test_no_contract_is_not_complete():
    c = compute_ctc(None, None, [], [P(1, D("500"), date(2026, 1, 1))])
    assert c.status == "no_contract"
    assert c.pct_complete is None


def test_deleted_rows_ignored():
    c = compute_ctc(D("100"), None, [CO(D("50"), "Approved", deleted_in_source=True)],
                    [P(1, D("100"), date(2026, 1, 1), deleted_in_source=True)])
    assert c.revised_contract == D("100")
    assert c.billed_to_date == D("0")
