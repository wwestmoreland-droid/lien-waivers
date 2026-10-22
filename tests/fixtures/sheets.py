"""Fixture sheets built from real AR/AP, subcontract and CO rows (IMAGE Studios –
Palatine job, Oct 2026), with vendor, client and project names anonymized.
Amounts, dates, statuses and row structure are kept as they were."""
from __future__ import annotations

import copy

from app.smartsheet.client import SheetRow

PROJECT = "P--IL-102125-1"
PROJECT_NAME = "Studio Co - Palatine, IL"
OTHER_PROJECT = "P--CA-012425-1"
OTHER_NAME = "Kids Dental - Stockton, CA"


def _row(row_id, **values):
    return SheetRow(row_id=row_id, values=values, display={})


def intake():
    return [
        _row(1, project_number=PROJECT, project_name=PROJECT_NAME, project_status="8 Construction",
             project_manager={"name": "Pat Manager", "email": "pmanager@buildoutpros.com"},
             state="IL", complete_address="100 Main St, Palatine, IL, 60067",
             client_company="Studio Co Holdings", contract_amount=950000.0),
        _row(2, project_number=OTHER_PROJECT, project_name=OTHER_NAME,
             project_status="7 Pre Construction – Permitting/Procurement/Purchasing",
             state="CA", complete_address="5 Elm St, Stockton, CA, 95207", client_company="Kids Dental LLC"),
        _row(3, project_number="P--TX-010126-1", project_name="Bid Only - Austin, TX",
             project_status="3 Estimating", state="TX"),
    ]


def archived():
    return [_row(50, project_number="P--NV-010125-1", project_name="Old Job - Reno, NV",
                 project_status="10 Complete", state="NV")]


def subcontracts():
    return [
        _row(101, contractor_name="Delta Fire Systems, Inc", project=PROJECT, contract_amount=32760.7,
             payment_terms="Net 30", supplier_contractor="Labor & Material",
             billing_email="ap@deltafire.com", billing_name="Dana AP", pm_email="pm@deltafire.com",
             estimator_email="est@deltafire.com"),
        _row(102, contractor_name="Ridge Plumbing", project=PROJECT, contract_amount=226800.0,
             payment_terms="Net 30", supplier_contractor="Labor & Material",
             billing_email="billing@ridgeplumbing.com"),
        _row(103, contractor_name="Summit Electric, LLC", project=PROJECT, contract_amount=148720.0,
             payment_terms="Net 30 with 10% Retention", supplier_contractor="Labor & Material",
             billing_email="office@gmail.com"),
        _row(104, contractor_name="Tri-State Glass - Mirror, Inc.", project=PROJECT, contract_amount=39000.0,
             payment_terms="Net 15", supplier_contractor="Labor & Material"),
        _row(105, contractor_name="Iannuzzi Millwork Corporation", project=PROJECT, contract_amount=6764.0,
             payment_terms="Net 30", supplier_contractor="Labor & Material"),
        _row(106, contractor_name="Zero Dollar Signs", project=PROJECT, contract_amount=None,
             payment_terms="Net 30"),
        _row(201, contractor_name="Valley Drywall", project=OTHER_PROJECT, contract_amount=50000.0),
    ]


def change_orders():
    return [
        _row(301, project_number=PROJECT, internal_external="Internal", approval="Approved",
             contractor_and_project=f"Delta Fire Systems, Inc - {PROJECT_NAME}", total_cost=1663.0,
             cost_from_sub=1663.0, requested_total=1663.0, description="Add heads"),
        _row(302, project_number=PROJECT, internal_external="Internal", approval="Pending",
             contractor_and_project=f"Ridge Plumbing - {PROJECT_NAME}", total_cost=4500.0,
             cost_from_sub=4200.0, requested_total=4500.0, description="Reroute line"),
        _row(303, project_number=PROJECT, internal_external="External", approval="Approved",
             contractor_and_project=PROJECT_NAME, total_cost=9000.0),
    ]


def ar_ap():
    P = "Payables"
    return [
        _row(1001, receivables_payables=P, project_number=PROJECT, invoice_no="Pay App 1",
             vendor_and_project=f"Delta Fire Systems, Inc - {PROJECT_NAME}", amount=29647.8,
             date_received="2026-09-30", approval="Approved", approval_date="2026-10-07", paid_date="2026-10-16"),
        _row(1002, receivables_payables=P, project_number=PROJECT, invoice_no="COR #1",
             vendor_and_project=f"Delta Fire Systems, Inc - {PROJECT_NAME}", amount=1663.0,
             date_received="2026-09-04", approval="Rejected", approval_date="2026-10-01"),
        _row(1003, receivables_payables=P, project_number=PROJECT, invoice_no="Pay App 2",
             vendor_and_project=f"Delta Fire Systems, Inc - {PROJECT_NAME}", amount=627.3,
             date_received="2026-10-31", approval="Approved", approval_date="2026-10-19"),
        _row(1004, receivables_payables=P, project_number=PROJECT, invoice_no="Pay App 1",
             vendor_and_project=f"Ridge Plumbing - {PROJECT_NAME}", amount=82500.0,
             date_received="2026-07-31", approval="Approved", paid_date="2026-08-21"),
        _row(1005, receivables_payables=P, project_number=PROJECT, invoice_no="Pay App 2",
             vendor_and_project=f"Ridge Plumbing - {PROJECT_NAME}", amount=85000.0,
             date_received="2026-08-31", approval="Approved", paid_date="2026-09-18"),
        _row(1006, receivables_payables=P, project_number=PROJECT, invoice_no=2379.0,
             vendor_and_project=f"Summit Electric, LLC - {PROJECT_NAME}", amount=80000.0,
             date_received="2026-08-19", approval="Approved", paid_date="2026-09-04"),
        _row(1007, receivables_payables=P, project_number=PROJECT, invoice_no=2402.0,
             vendor_and_project=f"Summit Electric, LLC - {PROJECT_NAME}", amount=20000.0,
             date_received="2026-09-21", approval="Hold"),
        _row(1008, receivables_payables=P, project_number=PROJECT, invoice_no=44718.0,
             vendor_and_project=f"Tri-State Glass - Mirror, Inc. - {PROJECT_NAME}", amount=19500.0,
             date_received="2026-09-10", approval="Approved", paid_date="2026-09-11"),
        _row(1009, receivables_payables=P, project_number=PROJECT, invoice_no="2791-01",
             vendor_and_project="Ianuzzi Millwork Corporation", amount=6764.0,
             date_received="2026-09-24", approval="Approved", paid_date="2026-10-16"),
        _row(1010, receivables_payables=P, project_number=PROJECT, invoice_no="INV408272",
             vendor_and_project="BP Waste Services", amount=230.75,
             date_received="2026-10-19", approval="Approved"),
        _row(1011, receivables_payables="Receivables", project_number=PROJECT, invoice_no="AR-1",
             vendor_and_project=PROJECT_NAME, amount=100000.0),
        _row(1012, receivables_payables=P, project_number=OTHER_PROJECT, invoice_no="1",
             vendor_and_project=f"Valley Drywall - {OTHER_NAME}", amount=10000.0, approval="Approved"),
    ]


class FakeSource:
    """In-memory SheetSource. Tests mutate .sheets between syncs."""

    def __init__(self):
        self.sheets = {
            "intake": intake(), "archived": archived(), "subcontracts": subcontracts(),
            "change_orders": change_orders(), "ar_ap": ar_ap(),
        }

    def read_sheet(self, key):
        return copy.deepcopy(self.sheets[key])

    def row(self, key, row_id) -> SheetRow:
        return next(r for r in self.sheets[key] if r.row_id == row_id)


class FakeContactWriter:
    """Stands in for app.smartsheet.writer.ContactWriter, writing into a FakeSource."""

    def __init__(self, source: FakeSource):
        from app.smartsheet.writer import WRITABLE
        self.source = source
        self.allowed = WRITABLE
        self.writes: list[tuple[int, dict]] = []

    def get_row(self, row_id: int) -> dict:
        v = self.source.row("subcontracts", row_id).values
        return {k: (str(v[k]).strip() if v.get(k) not in (None, "") else None) for k in self.allowed}

    def update_row(self, row_id: int, values: dict) -> None:
        bad = [k for k in values if k not in self.allowed]
        if bad:
            raise PermissionError(bad)
        self.writes.append((row_id, dict(values)))
        self.source.row("subcontracts", row_id).values.update(values)
