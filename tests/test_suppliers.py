"""Suppliers under each sub: list requests, list replies, supplier waivers, finals, download."""
import io

from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import select

from app.db.models import (
    Contact,
    EmailMessage,
    ReviewItem,
    Subcontract,
    Supplier,
    SupplierCandidate,
    Waiver,
)
from app.mail import inbound, outbound
from app.mail.provider import InAttachment
from app.main import create_app
from app.smartsheet.sync import run_sync
from app.suppliers import service as suppliers
from app.waivers import service as waivers
from app.waivers.types import WaiverStatus, WaiverType
from tests.fixtures.sheets import PROJECT
from tests.test_email_e2e import mail, pay, request_email, send_request  # noqa: F401
from tests.test_job_waivers import signed_pdf


def sub(db, row_id) -> Subcontract:
    return db.scalar(select(Subcontract).where(Subcontract.source_row_id == row_id))


def test_major_trades_are_asked_for_their_list_once(mail):
    db = mail.db
    asked = db.scalars(select(EmailMessage).where(EmailMessage.kind == "supplier_list")).all()
    names = {db.get(Subcontract, int(e.token.split("-")[1])).vendor.name for e in asked}
    assert "Delta Fire Systems, Inc" in names and "Ridge Plumbing" in names
    assert "Iannuzzi Millwork Corporation" not in names  # under $20,000
    assert all(e.status == "draft" for e in asked)  # waits in the Outbox like everything else
    assert "[SD-" in asked[0].subject and "suppliers" in asked[0].body_text
    run_sync(db, mail.source, trigger="test")
    assert len(db.scalars(select(EmailMessage).where(EmailMessage.kind == "supplier_list")).all()) == len(asked)


def test_labor_only_subs_are_not_asked(mail):
    db = mail.db
    delta = sub(db, 101)
    delta.supplier_contractor = "Labor"
    assert not suppliers.is_major_trade(db, delta)


def test_add_edit_remove_supplier(mail):
    db = mail.db
    delta = sub(db, 101)
    with TestClient(create_app()) as c:
        r = c.post(f"/subs/{delta.id}/suppliers", data={"name": "ABC Supply", "supplies": "sprinkler pipe",
                                                         "amount": "12,000", "email": "Orders@abcsupply.com",
                                                         "back": "/jobs"})
        assert r.status_code == 200 and "Added ABC Supply" in r.text
        s = db.scalar(select(Supplier))
        assert s.approx_amount == 12000 and s.vendor.name == "ABC Supply" and s.status == "active"
        assert db.scalar(select(Contact).where(Contact.email == "orders@abcsupply.com")).vendor_id == s.vendor_id
        page = c.get("/jobs").text
        assert "Suppliers (1)" in page and "ABC Supply" in page and "No waiver yet" in page
        c.post(f"/suppliers/{s.id}", data={"supplies": "pipe and fittings", "back": "/jobs"})
        db.expire_all()
        assert db.get(Supplier, s.id).supplies == "pipe and fittings"
        c.post(f"/suppliers/{s.id}/remove", data={"back": "/jobs"})
        db.expire_all()
        assert db.get(Supplier, s.id).status == "removed" and suppliers.active(db, delta) == []
        assert "Suppliers (0)" in c.get(f"/jobs/{PROJECT}").text


def _reply_to_list(mail, sc, body, attachments=None):
    e = db_e = mail.db.scalar(select(EmailMessage).where(EmailMessage.token == f"SD-{sc.id}"))
    outbound.send(mail.db, db_e, "ap")
    mail.db.commit()
    mail.box.deliver(from_addr="ap@deltafire.com", from_name="Dana AP", subject="Re: " + e.subject,
                     thread_id=e.provider_thread_id, in_reply_to=e.rfc_message_id, body=body,
                     attachments=attachments or [])
    inbound.poll_all(mail.db)
    mail.db.expire_all()


def test_supplier_list_reply_becomes_quick_questions(mail, monkeypatch):
    db = mail.db
    delta = sub(db, 101)
    monkeypatch.setattr(suppliers, "read_list", lambda db_, e, client=None: {
        "no_suppliers_over_minimum": False,
        "suppliers": [{"name": "ABC Supply", "supplies": "sprinkler pipe", "approx_amount": "12000",
                       "contact_name": "Al", "email": "al@abcsupply.com", "phone": None},
                      {"name": "Pipe Co", "supplies": "fittings", "approx_amount": None, "contact_name": None,
                       "email": None, "phone": None}]})
    _reply_to_list(mail, delta, "Here you go: ABC Supply, Pipe Co")
    reply = db.scalar(select(EmailMessage).where(EmailMessage.kind == "supplier_list_reply"))
    assert reply is not None and reply.project_number == PROJECT
    cands = db.scalars(select(SupplierCandidate).order_by(SupplierCandidate.id)).all()
    assert [c.name for c in cands] == ["ABC Supply", "Pipe Co"] and all(c.status == "pending" for c in cands)
    assert db.get(Subcontract, delta.id).supplier_list_status == "received"
    with TestClient(create_app()) as c:
        page = c.get("/").text
        assert "lists <strong>ABC Supply</strong> (sprinkler pipe, ~$12,000). Add as their supplier?" in page
        r = c.post(f"/supplier-candidates/{cands[0].id}/yes", data={"back": "/"})
        assert "Added ABC Supply." in r.text
        c.post(f"/supplier-candidates/{cands[1].id}/no", data={"back": "/"})
    db.expire_all()
    assert [s.name for s in suppliers.active(db, db.get(Subcontract, delta.id))] == ["ABC Supply"]
    assert db.get(SupplierCandidate, cands[1].id).status == "rejected"


def test_no_suppliers_reply_marks_the_sub(mail, monkeypatch):
    db = mail.db
    delta = sub(db, 101)
    monkeypatch.setattr(suppliers, "read_list",
                        lambda db_, e, client=None: {"no_suppliers_over_minimum": True, "suppliers": []})
    _reply_to_list(mail, delta, "We don't have any suppliers over $20k.")
    assert db.get(Subcontract, delta.id).supplier_list_status == "none"
    with TestClient(create_app()) as c:
        assert "none over the minimum" in c.get("/jobs").text


def test_unread_list_reply_asks_a_person(mail):
    db = mail.db
    delta = sub(db, 101)
    _reply_to_list(mail, delta, "see attached")  # no API key in tests: Claude can't read it
    assert db.scalar(select(ReviewItem).where(ReviewItem.kind == "supplier_list_unread"))


def test_request_asks_for_supplier_waivers_and_files_them(mail, monkeypatch):
    from app.mail import verify
    db = mail.db
    delta = sub(db, 101)
    abc = suppliers.add(db, delta, "ap", "ABC Supply", "pipe")
    db.commit()
    w, e = send_request(mail)
    assert "Please also send signed lien waivers from your suppliers on this job" in e.body_text and "ABC Supply" in e.body_text

    def fake_verify(w_, att, client=None):
        supplier = att.filename.startswith("abc")
        found = {"is_lien_waiver": True, "appears_signed": True, "notary_completed": True,
                 "waiver_type": "unconditional_partial", "amount": "5000" if supplier else str(w_.amount),
                 "through_date": w_.through_date.isoformat(), "project_name_on_form": "Studio", "project_matches": True,
                 "signer_company": "ABC Supply Co." if supplier else "Delta Fire", "is_supplier_waiver": supplier,
                 "issues": [], "summary": ""}
        return {"status": "looks_good", "problems": [], "found": found, "attachment_id": att.id}
    monkeypatch.setattr(verify, "verify", fake_verify)
    mail.box.deliver(from_addr="ap@deltafire.com", subject="Re: " + e.subject, thread_id=e.provider_thread_id,
                     in_reply_to=e.rfc_message_id,
                     attachments=[InAttachment("abc-waiver.pdf", "application/pdf", signed_pdf("ABC")),
                                  InAttachment("delta-waiver.pdf", "application/pdf", signed_pdf("Delta"))])
    inbound.poll_all(db)
    db.expire_all()
    own = db.get(Waiver, w.id)
    assert own.status == WaiverStatus.RECEIVED and own.signed_pdf_path.endswith("delta-waiver.pdf")
    sw = db.scalar(select(Waiver).where(Waiver.supplier_id == abc.id))
    assert sw.status == WaiverStatus.RECEIVED and sw.subcontract_id == delta.id and sw.payable_id == own.payable_id
    assert str(sw.amount) == "5000.00" and sw.signed_pdf_path.endswith("abc-waiver.pdf")
    with TestClient(create_app()) as c:
        page = c.get("/").text
        assert "Supplier waiver: ABC Supply" in page and "(for Delta Fire Systems, Inc)" in page
        r = c.post(f"/waivers/{sw.id}/accept", data={"back": "/"})
        assert "Accepted" in r.text
        jobs = c.get("/jobs").text
        assert "Unconditional partial through" in jobs and ", signed" in jobs
        # Download with suppliers: the cover lists ABC under Delta, and its waiver follows Delta's.
        r = c.get(f"/jobs/{PROJECT}/signed-waivers.pdf?suppliers=1")
        pdf = PdfReader(io.BytesIO(r.content))
        cover = pdf.pages[0].extract_text()
        assert "ABC Supply" in cover and "supplier to Delta Fire Systems" in cover
        texts = [p.extract_text() for p in pdf.pages[1:]]
        assert texts.index(next(t for t in texts if "Delta" in t)) < texts.index(next(t for t in texts if "ABC" in t))
        without = PdfReader(io.BytesIO(c.get(f"/jobs/{PROJECT}/signed-waivers.pdf").content))
        assert "ABC Supply" not in without.pages[0].extract_text()


def test_unknown_supplier_waiver_becomes_a_question(mail, monkeypatch):
    from app.mail import verify
    db = mail.db
    delta = sub(db, 101)
    suppliers.add(db, delta, "ap", "ABC Supply")
    db.commit()
    w, e = send_request(mail)
    monkeypatch.setattr(verify, "verify", lambda w_, att, client=None: {
        "status": "looks_good", "problems": [], "found": {"is_supplier_waiver": True, "signer_company": "Zed Lumber",
                                                          "waiver_type": "unconditional_partial", "appears_signed": True}})
    mail.box.deliver(from_addr="ap@deltafire.com", subject="Re: " + e.subject, thread_id=e.provider_thread_id,
                     attachments=[InAttachment("zed.pdf", "application/pdf", signed_pdf("Zed"))])
    inbound.poll_all(db)
    assert db.scalar(select(ReviewItem).where(ReviewItem.kind == "supplier_waiver_unknown"))
    assert db.get(Waiver, w.id).status == WaiverStatus.SENT  # not mistaken for Delta's own


def test_final_lists_missing_supplier_waivers(mail):
    db = mail.db
    delta = sub(db, 101)
    abc = suppliers.add(db, delta, "ap", "ABC Supply")
    w = pay(mail, 1003)
    cf = waivers.create_waiver(db, w.payable, delta, WaiverType.CONDITIONAL_FINAL, None)
    db.commit()
    item = db.scalar(select(ReviewItem).where(ReviewItem.kind == "missing_supplier_waivers", ReviewItem.ref_id == cf.id))
    assert item.status == "open" and "ABC Supply" in item.message
    assert any("ABC Supply" in (ev.note or "") for ev in cf.events)
    values = outbound.merge_values(db, cf, mail.sender)
    assert values["missing_supplier_waivers"] and values["missing_supplier_names"] == "ABC Supply"
    with TestClient(create_app()) as c:
        assert "supplier waivers are still missing from ABC Supply" in c.get("/").text
    suppliers.remove(db, abc, "ap")
    db.commit()
    assert db.get(ReviewItem, item.id).status == "resolved"
