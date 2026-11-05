"""A signed copy's own amount and through date are what's on record."""
from datetime import date
from decimal import Decimal

from app.mail import verify
from app.waivers import service as waivers
from app.waivers.types import WaiverStatus
from tests.test_email_e2e import mail  # noqa: F401
from tests.test_reply_scenarios import SIGNED, W, reply, send_request


def reads(monkeypatch, amount, through, wtype="unconditional_partial"):
    def fake(w, att, client=None):
        found = {"is_lien_waiver": True, "appears_signed": True, "notary_completed": True, "waiver_type": wtype,
                 "amount": amount, "through_date": through, "project_matches": True, "is_supplier_waiver": False,
                 "signer_company": "Delta Fire Systems", "issues": [], "summary": ""}
        problems = verify.compare(w, found)
        return {"status": "issues" if problems else "looks_good", "problems": problems, "found": found,
                "attachment_id": att.id, "filename": att.filename}
    monkeypatch.setattr(verify, "verify", fake)


def test_a_corrected_signed_copy_is_recorded_as_signed(mail, monkeypatch):
    """Daus on Cannon Creek: we asked for $12,819.27 through Dec 8, 2025; they signed the
    corrected one for $219,847.35 through Oct 1, 2026."""
    db = mail.db
    w, e = send_request(mail)
    asked = (W(db, w).amount, W(db, w).through_date)
    reads(monkeypatch, "219847.35", "2026-10-01")
    reply(mail, e, attachments=[SIGNED])
    ww = W(db, w)
    assert ww.status == WaiverStatus.RECEIVED
    assert (ww.amount, ww.through_date) == (Decimal("219847.35"), date(2026, 10, 1))
    assert "Recorded from the signed copy: $219,847.35 through Oct 1, 2026" in ww.verification["note"]
    assert f"${asked[0]:,.2f}" in ww.verification["note"]
    assert not any("should be" in p for p in ww.verification["problems"])  # the old request doesn't apply
    assert ww.verification["status"] == "looks_good"
    assert waivers.best_signed_waiver(db, ww.subcontract).id == ww.id


def test_signed_for_less_is_flagged(mail, monkeypatch):
    db = mail.db
    w, e = send_request(mail)
    reads(monkeypatch, "100.00", None)
    reply(mail, e, attachments=[SIGNED])
    ww = W(db, w)
    assert ww.amount == Decimal("100.00") and "less than we asked for" in ww.verification["note"]


def test_send_back_asks_for_what_we_asked_for(mail, monkeypatch):
    db = mail.db
    w, e = send_request(mail)
    asked = W(db, w).amount
    reads(monkeypatch, "100.00", None)
    reply(mail, e, attachments=[SIGNED])
    waivers.reject(db, W(db, w), "ap", "wrong amount")
    db.commit()
    assert W(db, w).amount == asked


def test_a_different_kind_of_waiver_isnt_recorded_over_it(mail, monkeypatch):
    db = mail.db
    w, e = send_request(mail)
    asked = W(db, w).amount
    reads(monkeypatch, "99999.00", None, wtype="unconditional_final")
    reply(mail, e, attachments=[SIGNED])
    assert W(db, w).amount == asked


def test_copies_filed_earlier_are_caught_up(mail, monkeypatch):
    db = mail.db
    w, e = send_request(mail)
    reads(monkeypatch, "219847.35", "2026-10-01")
    real = waivers.apply_signed_values
    monkeypatch.setattr(waivers, "apply_signed_values", lambda *a, **k: False)  # filed before this fix
    reply(mail, e, attachments=[SIGNED])
    monkeypatch.setattr(waivers, "apply_signed_values", real)
    assert W(db, w).amount != Decimal("219847.35")
    assert waivers.apply_signed_values_all(db) == 1
    db.commit()
    assert W(db, w).amount == Decimal("219847.35") and waivers.apply_signed_values_all(db) == 0
