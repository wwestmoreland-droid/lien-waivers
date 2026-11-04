"""Waivers entered by hand, for things Smartsheet doesn't have."""
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ctc.engine import compute_for_subcontract
from app.db.models import EmailMessage, Subcontract, Waiver
from app.main import create_app
from app.smartsheet.sync import run_sync
from app.waivers import service as waivers
from app.waivers.types import WaiverStatus, WaiverType
from tests.test_email_e2e import PDF, mail  # noqa: F401
from tests.fixtures.sheets import PROJECT


def today_signed_back(db):
    from app.services import today
    return today.signed_back(db)


def delta(db):
    return next(sc for sc in db.scalars(select(Subcontract)) if sc.vendor.name.startswith("Delta")
                and sc.project.number == PROJECT)


def request_for(db, w):
    return db.scalar(select(EmailMessage).where(EmailMessage.waiver_id == w.id, EmailMessage.kind == "waiver_request"))


def test_manual_partial_keeps_its_amount_and_leaves_totals_alone(mail):
    db = mail.db
    sc = delta(db)
    before = compute_for_subcontract(sc)
    w = waivers.create_manual_waiver(db, sc, WaiverType.UNCONDITIONAL_PARTIAL, Decimal("17935.00"),
                                     date(2026, 10, 13), "ap", invoice_no="377841", paid_date=date(2026, 11, 2),
                                     payment_amount=Decimal("5380.50"), note="July card payment missing")
    db.commit()
    assert w.status == WaiverStatus.DRAFT and w.amount == Decimal("17935.00") and w.generated_pdf_path
    e = request_for(db, w)
    assert e is not None and "$17,935.00" in e.subject and "377841" in e.body_text and "$5,380.50" in e.body_text
    # A sync neither removes it nor changes its amount, and cost to complete is untouched.
    run_sync(db, mail.source, trigger="test")
    db.commit()
    db.expire_all()
    w = db.get(Waiver, w.id)
    assert w.amount == Decimal("17935.00") and w.status == WaiverStatus.DRAFT and w.payable.is_manual
    after = compute_for_subcontract(db.get(Subcontract, sc.id))
    assert (after.paid_to_date, after.cost_to_complete) == (before.paid_to_date, before.cost_to_complete)
    assert any("entered by hand: July card payment missing" in (ev.note or "") for ev in w.events)


def test_rules_that_keep_the_email_right(mail):
    db = mail.db
    sc = delta(db)
    for kw, wtype in ((dict(invoice_no="1"), WaiverType.UNCONDITIONAL_PARTIAL),  # no paid date
                      (dict(paid_date=date(2026, 11, 2)), WaiverType.UNCONDITIONAL_PARTIAL),  # no invoice
                      (dict(paid_date=date(2026, 11, 2)), WaiverType.UNCONDITIONAL_FINAL)):
        if wtype == WaiverType.UNCONDITIONAL_FINAL:
            w = waivers.create_manual_waiver(db, sc, wtype, Decimal("100"), date(2026, 11, 1), "ap", **kw)
            assert w.id  # a final needs no invoice number
            continue
        try:
            waivers.create_manual_waiver(db, sc, wtype, Decimal("100"), date(2026, 11, 1), "ap", **kw)
            raise AssertionError("should have been refused")
        except ValueError:
            db.rollback()


def test_form_on_the_job_page(mail):
    db = mail.db
    sc = delta(db)
    with TestClient(create_app()) as c:
        page = c.get(f"/jobs/{PROJECT}").text
        assert "Add a waiver by hand" in page and f"/subs/{sc.id}/manual-waiver" in page
        r = c.post(f"/subs/{sc.id}/manual-waiver", data={
            "wtype": "UNCONDITIONAL_PARTIAL", "amount": "$17,935.00", "through_date": "2026-10-13",
            "invoice_no": "377841", "payment_amount": "5,380.50", "paid_date": "2026-11-02", "note": ""},
            follow_redirects=False)
        assert r.status_code == 303 and "Ready%20to%20send" in r.headers["location"]
        bad = c.post(f"/subs/{sc.id}/manual-waiver", data={
            "wtype": "UNCONDITIONAL_PARTIAL", "amount": "abc", "through_date": "2026-10-13"}, follow_redirects=False)
        assert "isn%27t%20an%20amount" in bad.headers["location"] or "isn't an amount" in bad.headers["location"]
        # Already signed: straight to Signed copies back, no request email waiting.
        r = c.post(f"/subs/{sc.id}/manual-waiver", data={
            "wtype": "UNCONDITIONAL_FINAL", "amount": "17935", "through_date": "2026-11-02", "paid_date": "2026-11-02"},
            files={"file": ("signed.pdf", PDF, "application/pdf")}, follow_redirects=False)
        assert "is%20accepted%20with%20its%20signed%20copy" in r.headers["location"]
    db.expire_all()
    manual = db.scalars(select(Waiver).where(Waiver.subcontract_id == sc.id).order_by(Waiver.id.desc())).all()
    final = next(w for w in manual if w.type == WaiverType.UNCONDITIONAL_FINAL.value)
    assert final.status == WaiverStatus.ACCEPTED and final.signed_pdf_path  # accepted on the spot
    assert final.id not in [w.id for w in today_signed_back(db)]  # nothing to review
    assert request_for(db, final).status == "discarded"
    partial = next(w for w in manual if w.type == WaiverType.UNCONDITIONAL_PARTIAL.value and w.payable.is_manual)
    assert partial.amount == Decimal("17935.00")


def test_unconditional_final_needs_no_amount_and_shows_on_the_card(mail):
    db = mail.db
    sc = delta(db)
    with TestClient(create_app()) as c:
        r = c.post(f"/subs/{sc.id}/manual-waiver", data={
            "wtype": "UNCONDITIONAL_FINAL", "amount": "", "through_date": "2026-11-02", "paid_date": "2026-11-02"},
            follow_redirects=False)
        loc = r.headers["location"]
        assert loc.startswith(f"/jobs/{PROJECT}?open={sc.id}") and loc.endswith(f"#sub-{sc.id}")
        page = c.get(loc.split("#")[0]).text
    card = page.split(f'id="sub-{sc.id}"')[1].split('class="subcard"')[0]
    assert card.lstrip().startswith("open") and "Unconditional final for Delta" in card  # opened, with the result
    assert "Waivers in progress" in card and "added by hand" in card and "in Ready to send" in card
    db.expire_all()
    w = db.scalars(select(Waiver).where(Waiver.subcontract_id == sc.id).order_by(Waiver.id.desc())).first()
    assert w.type == WaiverType.UNCONDITIONAL_FINAL.value and w.amount is None and w.generated_pdf_path
    assert request_for(db, w) is not None


def test_a_mistake_is_shown_on_the_card_too(mail):
    db = mail.db
    sc = delta(db)
    with TestClient(create_app()) as c:
        r = c.post(f"/subs/{sc.id}/manual-waiver", data={
            "wtype": "UNCONDITIONAL_PARTIAL", "amount": "500", "through_date": "2026-11-02", "paid_date": "2026-11-02"},
            follow_redirects=False)
        page = c.get(r.headers["location"].split("#")[0]).text
    card = page.split(f'id="sub-{sc.id}"')[1].split('class="subcard"')[0]
    assert "Enter the invoice number" in card


def test_signed_by_hand_is_the_one_on_file_until_a_newer_one_comes(mail):
    """Accepted on the spot, even when the automatic check would object, and replaced as the
    waiver on file only by a newer signed one."""
    from app.mail import verify
    db = mail.db
    sc = delta(db)
    called = []
    real = verify.verify
    verify.verify = lambda *a, **k: called.append(1) or real(*a, **k)
    try:
        w = waivers.create_manual_waiver(db, sc, WaiverType.UNCONDITIONAL_PARTIAL, Decimal("10000"),
                                         date(2026, 10, 1), "ap", invoice_no="A1", paid_date=date(2026, 10, 2))
        waivers.accept_by_hand(db, w, "ap", "signed.pdf", PDF)
        db.commit()
    finally:
        verify.verify = real
    assert w.status == WaiverStatus.ACCEPTED and not called
    assert waivers.best_signed_waiver(db, sc).id == w.id
    newer = waivers.create_manual_waiver(db, sc, WaiverType.UNCONDITIONAL_PARTIAL, Decimal("12000"),
                                         date(2026, 11, 1), "ap", invoice_no="A2", paid_date=date(2026, 11, 2))
    waivers.mark_received(db, newer, "system:mail", "newer.pdf", PDF + b"newer")
    db.commit()
    assert waivers.best_signed_waiver(db, sc).id == newer.id  # a more up to date signed waiver takes over
