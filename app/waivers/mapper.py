"""One field mapper for every waiver type (the old automation had three copies)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.ctc.engine import order_key
from app.db.models import Payable, Subcontract, Waiver

CONTRACTED_BY = "Buildout Pros LLC"


def fmt_date(d: date | None) -> str:
    return d.strftime("%b %-d, %Y") if d else ""


def exceptions_for(sc: Subcontract, as_of: Payable | None = None) -> str:
    """Pending Internal COs and invoices on hold for this sub, for a conditional final's EXCEPTIONS."""
    items = []
    for co in sorted(sc.change_orders, key=lambda c: c.source_row_id):
        if co.deleted_in_source or not co.is_internal or (co.status or "").lower() != "pending":
            continue
        desc = (co.description or "change order").strip()
        items.append(f"Pending CO: {desc} (${co.amount or Decimal(0):,.2f})")
    for p in sorted(sc.payables, key=order_key):
        if p.deleted_in_source or (p.approval_status or "").lower() != "hold":
            continue
        if as_of is not None and p.id == as_of.id:
            continue
        items.append(f"Invoice {p.invoice_no or ''} on hold (${p.amount or Decimal(0):,.2f})".replace("  ", " "))
    return "; ".join(items)


def field_values(waiver: Waiver, today: date | None = None) -> dict[str, object]:
    p, sc = waiver.payable, waiver.subcontract
    project = sc.project
    return {
        "contracted_by": CONTRACTED_BY,
        "project_name": project.name or project.number,
        "project_address": project.address or "",
        "owner": project.client_company or "",
        "through_date": fmt_date(waiver.through_date),
        "amount": waiver.amount,
        "exceptions": waiver.exceptions_text or "",
        "date": fmt_date(today or date.today()),
        "sub_name": sc.vendor.name,
        "sub_address": sc.vendor.address or "",
        "invoice_no": p.invoice_no or "",
    }
