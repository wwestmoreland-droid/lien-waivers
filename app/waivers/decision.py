"""Which waiver is owed for a payable event (section 5.3). Pure functions, no database.

Triggers are status transitions found during sync (approved, paid), never "Paid Date = today".
CTC is always the point-in-time value as of the payable in question.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.ctc.engine import CTC
from app.waivers.types import WaiverType


@dataclass
class PayableFacts:
    match_status: str
    approval_status: str | None
    is_paid: bool
    is_retention_invoice: bool
    invoice_no: str | None = None


@dataclass
class SubFacts:
    has_retention: bool


@dataclass
class Flags:
    conditional_partial_enabled: bool = False


@dataclass
class Review:
    kind: str
    message: str


@dataclass
class Decision:
    waiver: WaiverType | None = None
    reviews: list[Review] = field(default_factory=list)
    defer: bool = False   # leave the event unprocessed (e.g. row still unmatched)
    note: str = ""


def classify(ctc: CTC, sub: SubFacts, p: PayableFacts) -> str | Review:
    """'partial', 'final', or a Review when no waiver should go out automatically."""
    inv = f"invoice {p.invoice_no}" if p.invoice_no else "this invoice"
    if ctc.status == "no_contract":
        return Review("no_contract", f"No contract value for this sub, so {inv} can't be classed as partial or final.")
    if ctc.cost_to_complete < 0:
        return Review("over_billed", f"Over-billed by ${-ctc.cost_to_complete:,.2f} after {inv}. "
                                     "PM review needed; no final waiver is sent automatically.")
    if ctc.cost_to_complete == 0:
        if sub.has_retention and not p.is_retention_invoice:
            return Review("retention_check",
                          f"Cost to complete hit $0.00 on {inv}, which isn't marked as the retention invoice, "
                          "but this sub's terms include retention. Either they billed gross (retention never "
                          "withheld) or the invoice is misclassified. Mark it as the retention invoice or resolve.")
        return "final"
    return "partial"


def decide(event_kind: str, p: PayableFacts, ctc: CTC, sub: SubFacts, flags: Flags) -> Decision:
    if p.match_status == "direct_purchase":
        return Decision(note="direct purchase, no waiver")
    if p.match_status != "matched":
        return Decision(defer=True, note="waiting for the row to be matched")

    status = (p.approval_status or "").strip().lower()
    if status == "rejected":
        return Decision(note="rejected, no waiver")
    if status == "hold":
        return Decision(reviews=[Review("hold", "Invoice is on Hold. No waiver until it's approved.")],
                        note="on hold")

    if event_kind == "approved":
        if p.is_paid:
            # Approved and paid in the same sync: the paid event issues the unconditional waiver.
            return Decision(note="already paid; unconditional waiver comes from the paid event")
        kind = classify(ctc, sub, p)
        if isinstance(kind, Review):
            return Decision(reviews=[kind], note=kind.kind)
        if kind == "final":
            return Decision(WaiverType.CONDITIONAL_FINAL)
        if flags.conditional_partial_enabled:
            return Decision(WaiverType.CONDITIONAL_PARTIAL)
        return Decision(note="partial; conditional partials are off, unconditional goes out when paid")

    if event_kind == "paid":
        kind = classify(ctc, sub, p)
        if isinstance(kind, Review):
            return Decision(reviews=[kind], note=kind.kind)
        return Decision(WaiverType.UNCONDITIONAL_FINAL if kind == "final" else WaiverType.UNCONDITIONAL_PARTIAL)

    return Decision(note=f"no waiver for {event_kind} events")


def waiver_amount(waiver_type: WaiverType, payable_amount: Decimal | None) -> Decimal | None:
    """Conditional final: the final payment. Unconditional final: no amount. Partials use the
    total paid to date instead (service.paid_through); this payment is the fallback."""
    if waiver_type == WaiverType.UNCONDITIONAL_FINAL:
        return None
    return payable_amount
