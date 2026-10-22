"""Filling blank contact cells in All Subcontracts (the app's only Smartsheet writes)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.contacts import writeback
from app.db.models import AuditLog, Contact, Subcontract, Vendor
from app.mail import inbound
from app.main import create_app
from app.services import sync_runner
from app.services.settings_store import set_setting
from app.smartsheet.writer import ContactWriter
from tests.fixtures.sheets import FakeContactWriter
from tests.test_email_e2e import mail  # noqa: F401


@pytest.fixture
def sheet(mail):
    writer = FakeContactWriter(mail.source)
    sync_runner.set_source_factory(lambda: mail.source)
    sync_runner.set_writer_factory(lambda: writer)
    yield writer
    sync_runner.set_writer_factory(None)


def row(mail, rid):
    return mail.source.row("subcontracts", rid).values


def test_writer_can_only_touch_contact_cells(monkeypatch):
    w = ContactWriter(token="t", base_url="https://example.invalid")
    sent = []
    monkeypatch.setattr(w.http, "request", lambda *a, **k: sent.append(a))
    with pytest.raises(PermissionError):
        w.update_row(1, {"contract_amount": "1"})
    with pytest.raises(PermissionError):
        w.update_row(1, {"billing_email": "a@b.com", "payment_terms": "Net 7"})
    assert sent == []  # refused before any request


def test_blank_cells_filled_never_overwritten(mail, sheet):
    db = mail.db
    tri = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 104))
    tri.vendor.email_domains = ["tristateglass.com"]
    db.commit()
    # Tri-State: no billing in Smartsheet; their invoice sender is found in email.
    mail.box.deliver(from_addr="kim@tristateglass.com", from_name="Kim Ortiz", subject="Invoice 88",
                     body="Attached.\n\nKim Ortiz\nAccounts Payable\n(708) 555-0123")
    # Delta: Smartsheet has ap@deltafire.com; a different billing person emails us.
    mail.box.deliver(from_addr="maria@deltafire.com", from_name="Maria Lopez", subject="Invoice 1234",
                     body="Attached.\n\nMaria Lopez\nAccounts Receivable\n(847) 555-0142")
    # Ridge: one PM shows up in email, so the blank PM cells can be filled.
    mail.box.deliver(from_addr="lou@ridgeplumbing.com", from_name="Lou Park", subject="Schedule",
                     body="Q\n\nLou Park\nProject Manager\nRidge Plumbing\nC: 312-555-0177")
    inbound.poll_all(db)
    db.commit()
    sync_runner.sync_now("test")

    r104 = row(mail, 104)
    assert (r104["billing_email"], r104["billing_name"], r104["billing_phone"]) == (
        "kim@tristateglass.com", "Kim Ortiz", "(708) 555-0123")
    r101 = row(mail, 101)
    assert r101["billing_email"] == "ap@deltafire.com" and r101["billing_name"] == "Dana AP"  # untouched
    r102 = row(mail, 102)
    assert (r102["pm_email"], r102["pm_name"], r102["pm_phone"]) == (
        "lou@ridgeplumbing.com", "Lou Park", "(312) 555-0177")
    assert r102["billing_email"] == "billing@ridgeplumbing.com"
    for rid, values in sheet.writes:
        assert set(values) <= set(sheet.allowed)
    db.expire_all()
    assert db.scalar(select(Subcontract).where(Subcontract.source_row_id == 104)).billing_email == "kim@tristateglass.com"
    log = db.scalars(select(AuditLog).where(AuditLog.action == "smartsheet.cells_filled")).all()
    assert {a.details["row_id"] for a in log} == {102, 104}

    # Nothing left to fill: the next sync writes nothing.
    n = len(sheet.writes)
    sync_runner.sync_now("test")
    assert len(sheet.writes) == n


def test_someone_editing_the_row_wins(mail, sheet, monkeypatch):
    db = mail.db
    tri = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 104))
    db.add(Contact(email="kim@tristateglass.com", name="Kim", vendor_id=tri.vendor_id, role="billing",
                   status="confirmed", source="email", message_count=1))
    db.commit()
    # Typed into Smartsheet after the last sync, before the app writes.
    real_get = sheet.get_row

    def edited(rid):
        if rid == 104:
            row(mail, 104)["billing_email"] = "office@tristateglass.com"
        return real_get(rid)
    monkeypatch.setattr(sheet, "get_row", edited)
    stats = sync_runner.fill_contact_cells()
    assert row(mail, 104)["billing_email"] == "office@tristateglass.com" and stats["skipped_changed"] == 1
    assert not any(rid == 104 for rid, _ in sheet.writes)


def test_turned_off_writes_nothing(mail, sheet):
    db = mail.db
    tri = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 104))
    db.add(Contact(email="kim@tristateglass.com", vendor_id=tri.vendor_id, role="billing", status="confirmed",
                   source="email", message_count=1))
    set_setting(db, "smartsheet_contact_fill", False, "admin")
    db.commit()
    sync_runner.sync_now("test")
    assert sheet.writes == []


def test_replacing_billing_takes_a_click(mail, sheet):
    db = mail.db
    delta = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 101))
    db.add(Contact(email="maria@deltafire.com", name="Maria Lopez", phone="(847) 555-0142",
                   vendor_id=delta.vendor_id, role="billing", status="confirmed", source="email", message_count=1))
    db.get(Vendor, delta.vendor_id).waiver_email = "maria@deltafire.com"
    db.commit()
    sync_runner.sync_now("test")
    assert row(mail, 101)["billing_email"] == "ap@deltafire.com"  # not replaced automatically
    with TestClient(create_app()) as c:
        page = c.get(f"/subs/{delta.id}")
        assert "Update Smartsheet to maria@deltafire.com" in page.text
        # Stale page: Smartsheet changed since it loaded.
        r = c.post(f"/subs/{delta.id}/smartsheet-billing", data={"expected": "old@deltafire.com"})
        assert "changed in Smartsheet" in r.text and row(mail, 101)["billing_email"] == "ap@deltafire.com"
        r = c.post(f"/subs/{delta.id}/smartsheet-billing", data={"expected": "ap@deltafire.com"})
        assert "Smartsheet updated." in r.text and "Update Smartsheet to" not in r.text
    v = row(mail, 101)
    assert (v["billing_email"], v["billing_name"], v["billing_phone"]) == (
        "maria@deltafire.com", "Maria Lopez", "(847) 555-0142")
    assert db.scalar(select(AuditLog).where(AuditLog.action == "smartsheet.billing_replaced")).details["before"][
        "billing_email"] == "ap@deltafire.com"
    with TestClient(create_app()) as c:
        assert c.get("/settings").status_code == 200
