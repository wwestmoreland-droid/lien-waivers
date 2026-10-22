"""Cost to complete, per subcontract, computed from the app's own data.

All figures can be computed "as of" a given payable: only payables up to and including
that one (ordered by date received, then Smartsheet row ID) count. That is what the
waiver decision uses, because Smartsheet's Total Billed includes later rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Protocol

ZERO = Decimal("0.00")


class PayableLike(Protocol):
    source_row_id: int
    amount: Decimal | None
    date_received: date | None
    approval_status: str | None
    paid_date: date | None
    is_retention_invoice: bool
    deleted_in_source: bool


class ChangeOrderLike(Protocol):
    amount: Decimal | None
    status: str | None
    is_internal: bool
    deleted_in_source: bool


@dataclass
class CTC:
    original_contract: Decimal
    approved_cos: Decimal
    pending_cos: Decimal
    revised_contract: Decimal
    billed_to_date: Decimal
    paid_to_date: Decimal
    hold_total: Decimal
    retention_billed: Decimal
    retention_owed: Decimal | None
    cost_to_complete: Decimal
    pct_complete: Decimal | None
    status: str  # ok / complete / over_billed / no_contract
    as_of_row_id: int | None = None
    payables_counted: list[int] = field(default_factory=list)


def order_key(p: PayableLike) -> tuple:
    return (p.date_received or date.max, p.source_row_id)


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def compute_ctc(
    original_amount: Decimal | None,
    retainage_pct: Decimal | None,
    change_orders: Iterable[ChangeOrderLike],
    payables: Iterable[PayableLike],
    as_of: PayableLike | None = None,
    treat_as_of_as_approved: bool = True,
) -> CTC:
    """Compute CTC for one subcontract.

    as_of: limit to payables up to and including this one. When the as-of payable is the
    one being approved right now, treat_as_of_as_approved counts it as billed even if
    the stored status hasn't caught up.
    """
    original = original_amount or ZERO
    cos = [c for c in change_orders if c.is_internal and not c.deleted_in_source]
    approved_cos = sum((c.amount or ZERO for c in cos if _norm(c.status) == "approved"), ZERO)
    pending_cos = sum((c.amount or ZERO for c in cos if _norm(c.status) == "pending"), ZERO)
    revised = original + approved_cos

    rows = sorted((p for p in payables if not p.deleted_in_source), key=order_key)
    if as_of is not None:
        cutoff = order_key(as_of)
        rows = [p for p in rows if order_key(p) <= cutoff]

    billed = paid = hold = retention_billed = ZERO
    counted: list[int] = []
    for p in rows:
        amt = p.amount or ZERO
        status = _norm(p.approval_status)
        is_as_of = as_of is not None and p.source_row_id == as_of.source_row_id
        if status == "approved" or (is_as_of and treat_as_of_as_approved and status not in ("rejected", "hold")):
            billed += amt
            counted.append(p.source_row_id)
            if p.is_retention_invoice:
                retention_billed += amt
        elif status == "hold":
            hold += amt
        if p.paid_date and status != "rejected":
            paid += amt

    retention_owed = None
    if retainage_pct and retainage_pct > 0:
        retention_owed = (retainage_pct / Decimal(100) * revised).quantize(Decimal("0.01")) - retention_billed

    ctc = revised - billed
    pct = (billed / revised).quantize(Decimal("0.0001")) if revised > 0 else None

    if revised <= 0:
        status = "no_contract"
    elif ctc < 0:
        status = "over_billed"
    elif ctc == 0:
        status = "complete"
    else:
        status = "ok"

    return CTC(
        original_contract=original,
        approved_cos=approved_cos,
        pending_cos=pending_cos,
        revised_contract=revised,
        billed_to_date=billed,
        paid_to_date=paid,
        hold_total=hold,
        retention_billed=retention_billed,
        retention_owed=retention_owed,
        cost_to_complete=ctc,
        pct_complete=pct,
        status=status,
        as_of_row_id=as_of.source_row_id if as_of is not None else None,
        payables_counted=counted,
    )


def compute_for_subcontract(sc, as_of=None) -> CTC:
    """Convenience wrapper over an ORM Subcontract."""
    return compute_ctc(sc.original_amount, sc.retainage_pct, sc.change_orders, sc.payables, as_of=as_of)
