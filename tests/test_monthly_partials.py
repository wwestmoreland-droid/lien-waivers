"""Partial waivers once a month: one per sub, covering everything paid so far."""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import EmailMessage, Payable, Waiver
from app.main import create_app
from app.services.settings_store import get_setting, set_setting
from app.smartsheet.sync import run_sync
from app.waivers import service as waivers
from app.waivers.types import WaiverStatus
from tests.fixtures.sheets import PROJECT, PROJECT_NAME, _row
from tests.test_email_e2e import mail, request_email  # noqa: F401

pytestmark = pytest.mark.monthly_partials


def small_invoices(mail, *amounts_and_days):
    """New paid Delta invoices in Smartsheet (row ids from 2001), then a sync."""
    start = len([r for r in mail.source.sheets["ar_ap"] if r.row_id >= 2001])
    for i, (amount, day) in enumerate(amounts_and_days, start=start):
        mail.source.sheets["ar_ap"].append(_row(
            2001 + i, receivables_payables="Payables", project_number=PROJECT, invoice_no=f"INV-{i + 1}",
            vendor_and_project=f"Delta Fire Systems, Inc - {PROJECT_NAME}", amount=amount,
            date_received=day, approval="Approved", approval_date=day, paid_date=day))
    run_sync(mail.db, mail.source, trigger="test")
    mail.db.commit()
    mail.db.expire_all()


def delta_partials(db):
    return db.scalars(select(Waiver).join(Payable).where(Payable.source_row_id >= 2001).order_by(Waiver.id)).all()


def outbox(db):
    return db.scalars(select(EmailMessage).where(EmailMessage.kind == "waiver_request",
                                                 EmailMessage.status == "draft")).all()


def test_paid_invoices_wait_then_one_waiver_per_sub_goes_out(mail, monkeypatch):
    db = mail.db
    monkeypatch.setattr("app.services.today.local_today", lambda: date(2026, 11, 20))
    small_invoices(mail, (400.0, "2026-11-03"), (250.0, "2026-11-10"), (125.5, "2026-11-18"))
    ws = delta_partials(db)
    assert len(ws) == 3 and all(w.status == WaiverStatus.DRAFT for w in ws)
    assert all(request_email(db, w) is None for w in ws)  # nothing to send yet
    assert {w.id for w in waivers.held_partials(db)} == {w.id for w in ws}
    assert "Dec 1" in next(ev.note for ev in ws[0].events if ev.action == "held_monthly")

    # The send day: the newest one covers all three; the others are cancelled.
    monkeypatch.setattr("app.services.today.local_today", lambda: date(2026, 12, 1))
    run_sync(db, mail.source, trigger="test")
    db.commit()
    db.expire_all()
    ws = delta_partials(db)
    keep = ws[-1]
    assert [w.status for w in ws] == [WaiverStatus.VOID, WaiverStatus.VOID, WaiverStatus.DRAFT]
    e = request_email(db, keep)
    assert e is not None and e.status == "draft"
    paid_before = Decimal("29647.80")  # Pay App 1, paid in October
    assert keep.amount == paid_before + Decimal("775.50") and "$30,423.30" in e.subject
    assert waivers.held_partials(db) == []
    assert get_setting(db, "partials_released_month") == "2026-12"

    # Later in December: a new payment waits for January.
    small_invoices(mail, (90.0, "2026-12-05"))
    newest = delta_partials(db)[-1]
    assert waivers.is_held(db, newest) and request_email(db, newest) is None


def test_send_now_from_today(mail, monkeypatch):
    db = mail.db
    monkeypatch.setattr("app.services.today.local_today", lambda: date(2026, 11, 20))
    small_invoices(mail, (400.0, "2026-11-03"), (250.0, "2026-11-10"))
    month_before = get_setting(db, "partials_released_month")
    with TestClient(create_app()) as c:
        page = c.get("/").text
        assert "held for the monthly send on Dec 1" in page and "Delta Fire Systems" in page
        r = c.post("/today/release-partials", follow_redirects=False)
        assert "1%20partial%20waiver%20is%20in%20Ready%20to%20send" in r.headers["location"]
    db.expire_all()
    ws = delta_partials(db)
    assert [w.status for w in ws] == [WaiverStatus.VOID, WaiverStatus.DRAFT] and request_email(db, ws[-1])
    # Sending early doesn't use up this month's send day.
    assert get_setting(db, "partials_released_month") == month_before


def test_switching_to_every_invoice_releases_whats_held(mail, monkeypatch):
    db = mail.db
    monkeypatch.setattr("app.services.today.local_today", lambda: date(2026, 11, 20))
    small_invoices(mail, (400.0, "2026-11-03"))
    with TestClient(create_app()) as c:
        assert "Once a month, on day" in c.get("/settings/email").text
        r = c.post("/settings/email/partials", data={"schedule": "each_payment", "day": "1"}, follow_redirects=False)
        assert "went%20to%20Ready%20to%20send" in r.headers["location"]
    db.expire_all()
    assert request_email(db, delta_partials(db)[0]) is not None
    small_invoices(mail, (250.0, "2026-11-04"))
    assert request_email(db, delta_partials(db)[-1]) is not None  # straight out, one per invoice


def test_by_hand_and_finals_are_never_held(mail, monkeypatch):
    from app.db.models import Subcontract
    from app.waivers.types import WaiverType
    db = mail.db
    monkeypatch.setattr("app.services.today.local_today", lambda: date(2026, 11, 20))
    sc = next(s for s in db.scalars(select(Subcontract)) if s.vendor.name.startswith("Delta"))
    w = waivers.create_manual_waiver(db, sc, WaiverType.UNCONDITIONAL_PARTIAL, Decimal("500"), date(2026, 11, 1),
                                     "ap", invoice_no="X1", paid_date=date(2026, 11, 2))
    db.commit()
    assert not waivers.is_held(db, w) and request_email(db, w) is not None


def test_send_day_setting(mail):
    db = mail.db
    set_setting(db, "partial_send_day", 15, "ap")
    set_setting(db, "partials_released_month", None, "ap")
    db.commit()
    assert waivers.next_partial_send(db, date(2026, 11, 20)) == date(2026, 11, 20)  # due, goes out on the next sync
    assert waivers.next_partial_send(db, date(2026, 11, 3)) == date(2026, 11, 15)
    set_setting(db, "partials_released_month", "2026-11", "ap")
    assert waivers.next_partial_send(db, date(2026, 11, 20)) == date(2026, 12, 15)
