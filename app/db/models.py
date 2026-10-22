"""Local data model. The app's database is the source of record; Smartsheet is input only.

Rows that come from Smartsheet are never deleted here. When a source row disappears it is
marked `deleted_in_source` so history and audit trails stay intact.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


Money = Numeric(14, 2)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="AP")  # Admin / AP / PM
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True)  # e.g. P--CA-012425-1
    name: Mapped[str | None] = mapped_column(String(500))
    state: Mapped[str | None] = mapped_column(String(2))
    address: Mapped[str | None] = mapped_column(String(500))
    client_company: Mapped[str | None] = mapped_column(String(255))
    owner: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str | None] = mapped_column(String(100))
    pm_name: Mapped[str | None] = mapped_column(String(255))
    pm_email: Mapped[str | None] = mapped_column(String(255))
    contract_amount: Mapped[Decimal | None] = mapped_column(Money)
    source_sheet: Mapped[str] = mapped_column(String(20), default="INTAKE")  # INTAKE / Archived
    source_row_id: Mapped[int | None] = mapped_column(BigInteger)
    # Counts from All Subcontracts, kept for every project so the picker can show them
    # before a project is tracked (subcontract rows themselves are stored only when tracked).
    sub_count: Mapped[int] = mapped_column(Integer, default=0)
    sub_total: Mapped[Decimal] = mapped_column(Money, default=0)

    is_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    tracked_by: Mapped[str | None] = mapped_column(String(255))
    tracked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ever_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    backfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_in_source: Mapped[bool] = mapped_column(Boolean, default=False)

    subcontracts: Mapped[list[Subcontract]] = relationship(back_populates="project")

    @property
    def status_code(self) -> int | None:
        if not self.status:
            return None
        head = self.status.strip().split(" ", 1)[0]
        return int(head) if head.isdigit() else None


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255), unique=True)
    type: Mapped[str] = mapped_column(String(20), default="sub")  # sub / supplier / both
    email_domains: Mapped[list | None] = mapped_column(JSON)
    billing_contact_name: Mapped[str | None] = mapped_column(String(255))
    billing_email: Mapped[str | None] = mapped_column(String(255))
    billing_phone: Mapped[str | None] = mapped_column(String(100))
    address: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    aliases: Mapped[list[VendorAlias]] = relationship(back_populates="vendor")


class VendorAlias(Base):
    __tablename__ = "vendor_aliases"
    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    alias: Mapped[str] = mapped_column(String(255))
    normalized_alias: Mapped[str] = mapped_column(String(255), unique=True)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    vendor: Mapped[Vendor] = relationship(back_populates="aliases")


class DirectPurchase(Base):
    """A vendor billed on a job with no subcontract on purpose (e.g. a material house
    bought from directly). Payables matching it leave the Unmatched queue and never
    trigger waivers. project_number NULL means every job."""

    __tablename__ = "direct_purchases"
    __table_args__ = (UniqueConstraint("vendor_id", "project_number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    project_number: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Subcontract(Base):
    __tablename__ = "subcontracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_row_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    contractor_name_raw: Mapped[str] = mapped_column(String(255))
    supplier_contractor: Mapped[str | None] = mapped_column(String(50))  # Labor / Material / Labor & Material
    cost_codes: Mapped[str | None] = mapped_column(Text)
    original_amount: Mapped[Decimal | None] = mapped_column(Money)
    payment_terms: Mapped[str | None] = mapped_column(String(100))
    retainage_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    pm_name: Mapped[str | None] = mapped_column(String(255))
    pm_email: Mapped[str | None] = mapped_column(String(255))
    estimator_name: Mapped[str | None] = mapped_column(String(255))
    estimator_email: Mapped[str | None] = mapped_column(String(255))
    billing_name: Mapped[str | None] = mapped_column(String(255))
    billing_email: Mapped[str | None] = mapped_column(String(255))
    billing_phone: Mapped[str | None] = mapped_column(String(100))
    foreman_name: Mapped[str | None] = mapped_column(String(255))
    foreman_email: Mapped[str | None] = mapped_column(String(255))
    foreman_phone: Mapped[str | None] = mapped_column(String(100))
    bp_pm_responsible: Mapped[str | None] = mapped_column(String(255))
    superintendent: Mapped[str | None] = mapped_column(String(255))
    final_waiver_status_source: Mapped[str | None] = mapped_column(String(50))
    partial_waiver_amount_source: Mapped[str | None] = mapped_column(String(50))
    insurance_exp: Mapped[date | None] = mapped_column(Date)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_in_source: Mapped[bool] = mapped_column(Boolean, default=False)

    vendor: Mapped[Vendor] = relationship()
    project: Mapped[Project] = relationship(back_populates="subcontracts")
    change_orders: Mapped[list[ChangeOrder]] = relationship(back_populates="subcontract")
    payables: Mapped[list[Payable]] = relationship(back_populates="subcontract", foreign_keys="Payable.subcontract_id")

    @property
    def has_retention(self) -> bool:
        return bool(self.retainage_pct and self.retainage_pct > 0)

    @property
    def is_material_vendor(self) -> bool:
        return (self.supplier_contractor or "").strip().lower() == "material"


class ChangeOrder(Base):
    __tablename__ = "change_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_row_id: Mapped[int] = mapped_column(BigInteger, unique=True)  # internal CO key
    project_number: Mapped[str | None] = mapped_column(String(20))
    subcontract_id: Mapped[int | None] = mapped_column(ForeignKey("subcontracts.id"))
    vendor_raw: Mapped[str | None] = mapped_column(String(500))
    vendor_parsed: Mapped[str | None] = mapped_column(String(500))
    internal_external: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str | None] = mapped_column(String(20))  # Pending / Approved / Declined
    amount: Mapped[Decimal | None] = mapped_column(Money)  # Total Cost
    cost_from_sub: Mapped[Decimal | None] = mapped_column(Money)
    description: Mapped[str | None] = mapped_column(Text)
    created_in_source: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    match_status: Mapped[str] = mapped_column(String(30), default="unresolved")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_in_source: Mapped[bool] = mapped_column(Boolean, default=False)

    subcontract: Mapped[Subcontract | None] = relationship(back_populates="change_orders")

    @property
    def is_internal(self) -> bool:
        return (self.internal_external or "").strip().lower() == "internal"

    @property
    def amount_mismatch(self) -> bool:
        if not self.is_internal or self.cost_from_sub is None or self.amount is None:
            return False
        return abs(self.cost_from_sub - self.amount) >= Decimal("0.01")


class Payable(Base):
    __tablename__ = "payables"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_row_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    project_number: Mapped[str | None] = mapped_column(String(20))
    vendor_raw: Mapped[str | None] = mapped_column(String(500))
    vendor_parsed: Mapped[str | None] = mapped_column(String(500))
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    subcontract_id: Mapped[int | None] = mapped_column(ForeignKey("subcontracts.id"))
    manual_subcontract_id: Mapped[int | None] = mapped_column(ForeignKey("subcontracts.id"))
    # matched / unmatched_vendor / no_subcontract / ambiguous / direct_purchase
    match_status: Mapped[str] = mapped_column(String(30), default="unresolved")
    match_candidates: Mapped[list | None] = mapped_column(JSON)

    invoice_no: Mapped[str | None] = mapped_column(String(255))
    amount: Mapped[Decimal | None] = mapped_column(Money)
    date_received: Mapped[date | None] = mapped_column(Date)
    billed_through_date: Mapped[date | None] = mapped_column(Date)  # local, editable
    approval_status: Mapped[str | None] = mapped_column(String(20))  # Approved / Rejected / Hold
    approval_date: Mapped[date | None] = mapped_column(Date)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    paid_date: Mapped[date | None] = mapped_column(Date)
    entered_in_qb: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_code: Mapped[str | None] = mapped_column(String(255))
    material_sub_other: Mapped[str | None] = mapped_column(String(20))
    waiver_created_source: Mapped[str | None] = mapped_column(String(255))
    is_retention_auto: Mapped[bool] = mapped_column(Boolean, default=False)
    is_retention_override: Mapped[bool | None] = mapped_column(Boolean)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_in_source: Mapped[bool] = mapped_column(Boolean, default=False)

    subcontract: Mapped[Subcontract | None] = relationship(
        back_populates="payables", foreign_keys=[subcontract_id]
    )
    vendor: Mapped[Vendor | None] = relationship()

    @property
    def is_retention_invoice(self) -> bool:
        if self.is_retention_override is not None:
            return self.is_retention_override
        return self.is_retention_auto

    @property
    def payment_amount(self) -> Decimal | None:
        # AR/AP has no separate paid-amount column; a paid row is paid in full.
        return self.amount if self.paid_date else None

    @property
    def through_date(self) -> date | None:
        return self.billed_through_date or self.date_received

    @property
    def through_date_is_fallback(self) -> bool:
        return self.billed_through_date is None


class PayableEvent(Base):
    """State transitions found during sync. Waiver triggers (Phase 2) read these.
    Unique on (payable_id, kind) so a re-sync never records the same event twice."""

    __tablename__ = "payable_events"
    __table_args__ = (UniqueConstraint("payable_id", "kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    payable_id: Mapped[int] = mapped_column(ForeignKey("payables.id"))
    kind: Mapped[str] = mapped_column(String(30))  # approved / paid / held / rejected
    value: Mapped[str | None] = mapped_column(String(100))
    # True when the event was already in place when the row was first loaded (backfill).
    # Backfilled events never create emails.
    from_backfill: Mapped[bool] = mapped_column(Boolean, default=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sync_run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id"))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(255))


class CtcSnapshot(Base):
    __tablename__ = "ctc_snapshots"
    __table_args__ = (UniqueConstraint("subcontract_id", "snapshot_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    subcontract_id: Mapped[int] = mapped_column(ForeignKey("subcontracts.id"))
    snapshot_date: Mapped[date] = mapped_column(Date)
    original_contract: Mapped[Decimal] = mapped_column(Money)
    approved_cos: Mapped[Decimal] = mapped_column(Money)
    pending_cos: Mapped[Decimal] = mapped_column(Money)
    revised_contract: Mapped[Decimal] = mapped_column(Money)
    billed_to_date: Mapped[Decimal] = mapped_column(Money)
    paid_to_date: Mapped[Decimal] = mapped_column(Money)
    hold_total: Mapped[Decimal] = mapped_column(Money)
    retention_billed: Mapped[Decimal] = mapped_column(Money)
    retention_owed: Mapped[Decimal | None] = mapped_column(Money)
    cost_to_complete: Mapped[Decimal] = mapped_column(Money)
    pct_complete: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    status: Mapped[str] = mapped_column(String(20))


class ReviewItem(Base):
    """Things a person has to look at: over-billed, no contract, CO amount mismatch, etc.
    Waiver-specific review reasons are added in Phase 2."""

    __tablename__ = "review_items"
    __table_args__ = (UniqueConstraint("kind", "ref_type", "ref_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(50))
    ref_type: Mapped[str] = mapped_column(String(30))  # payable / subcontract / change_order
    ref_id: Mapped[int] = mapped_column(Integer)
    project_number: Mapped[str | None] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open / resolved / dismissed
    assignee_email: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_by: Mapped[str | None] = mapped_column(String(255))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(255))  # user email or "system:sync"
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str | None] = mapped_column(String(50))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict | None] = mapped_column(JSON)


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(30))  # schedule / manual / backfill / cli
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running / ok / error
    rows_read: Mapped[dict | None] = mapped_column(JSON)
    stats: Mapped[dict | None] = mapped_column(JSON)
    errors: Mapped[list | None] = mapped_column(JSON)


class JobLock(Base):
    """Cross-process lock so a scheduled sync and a Sync-now click never overlap."""

    __tablename__ = "job_locks"
    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    holder: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Waiver(Base):
    """One waiver owed for one payable. Unique on (payable_id, type) so re-processing an event
    never creates a second one. Every status change is appended to waiver_events."""

    __tablename__ = "waivers"
    __table_args__ = (UniqueConstraint("payable_id", "type"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    payable_id: Mapped[int] = mapped_column(ForeignKey("payables.id"))
    subcontract_id: Mapped[int] = mapped_column(ForeignKey("subcontracts.id"))
    type: Mapped[str] = mapped_column(String(30))  # WaiverType
    state: Mapped[str | None] = mapped_column(String(2))
    template_key: Mapped[str | None] = mapped_column(String(100))
    amount: Mapped[Decimal | None] = mapped_column(Money)
    through_date: Mapped[date | None] = mapped_column(Date)
    through_date_is_fallback: Mapped[bool] = mapped_column(Boolean, default=True)
    exceptions_text: Mapped[str | None] = mapped_column(Text)
    # needs_review / draft / sent / received / accepted / rejected / void
    status: Mapped[str] = mapped_column(String(20), default="draft")
    review_reason: Mapped[str | None] = mapped_column(Text)
    pdf_version: Mapped[int] = mapped_column(Integer, default=0)
    generated_pdf_path: Mapped[str | None] = mapped_column(String(500))
    signed_pdf_path: Mapped[str | None] = mapped_column(String(500))
    trigger_event_id: Mapped[int | None] = mapped_column(ForeignKey("payable_events.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer_email: Mapped[str | None] = mapped_column(String(255))

    payable: Mapped[Payable] = relationship()
    subcontract: Mapped[Subcontract] = relationship()
    events: Mapped[list[WaiverEvent]] = relationship(back_populates="waiver", order_by="WaiverEvent.id")

    @property
    def is_open(self) -> bool:
        return self.status in ("needs_review", "draft", "sent", "received", "rejected")


class WaiverEvent(Base):
    """Append-only history for a waiver."""

    __tablename__ = "waiver_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    waiver_id: Mapped[int] = mapped_column(ForeignKey("waivers.id"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(50))
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSON)

    waiver: Mapped[Waiver] = relationship(back_populates="events")
