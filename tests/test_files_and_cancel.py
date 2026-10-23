"""PDFs and attachments kept in the database, recovery of lost files, and cancelling waivers."""
import shutil

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db.models import EmailMessage, StoredFile, Waiver
from app.mail import inbound, outbound, reminders
from app.mail.provider import InAttachment
from app.main import create_app
from app.services.recover_files import recover
from app.waivers import service
from app.waivers.types import WaiverStatus
from tests.test_email_e2e import PDF, mail, pay, request_email, send_request  # noqa: F401


def wipe_disk():
    """What a redeploy without a volume does."""
    shutil.rmtree(get_settings().data_dir, ignore_errors=True)


def test_pdfs_survive_losing_the_disk(mail):
    db = mail.db
    w = pay(mail)
    wipe_disk()
    with TestClient(create_app()) as c:
        r = c.get(f"/waivers/{w.id}/pdf")
        assert r.status_code == 200 and r.content[:4] == b"%PDF"
    wipe_disk()
    e = request_email(db, w)
    assert outbound.send(db, e, "ap")
    assert mail.box.sent[-1].attachments[0].data[:4] == b"%PDF"


def test_never_sends_a_request_without_its_pdf(mail):
    db = mail.db
    w = pay(mail)
    db.query(StoredFile).delete()
    db.commit()
    wipe_disk()
    e = request_email(db, w)
    assert outbound.send(db, e, "ap") is False
    assert e.status == "failed" and "missing" in e.error and mail.box.sent == []


def test_recover_lost_files(mail):
    db = mail.db
    w, e = send_request(mail)
    mail.box.deliver(from_addr="ap@deltafire.com", from_name="Dana AP", subject="Re: " + e.subject,
                     thread_id=e.provider_thread_id, in_reply_to=e.rfc_message_id,
                     attachments=[InAttachment("signed waiver.pdf", "application/pdf", PDF)])
    inbound.poll_all(db)
    db.expire_all()
    w = db.get(Waiver, w.id)
    assert w.status == WaiverStatus.RECEIVED
    # Files from before they were kept in the database: gone from disk and from the table.
    db.query(StoredFile).delete()
    db.commit()
    wipe_disk()
    stats = recover(db)
    db.commit()
    assert stats["waiver_pdfs"] >= 1 and stats["attachments"] == 1 and stats["signed"] == 1
    with TestClient(create_app()) as c:
        assert c.get(f"/waivers/{w.id}/pdf").content[:4] == b"%PDF"
        assert c.get(f"/waivers/{w.id}/signed").content == PDF
        att = db.scalar(select(EmailMessage).where(EmailMessage.direction == "in")).attachments[0]
        assert c.get(f"/email/attachment/{att.id}").content == PDF
    assert recover(db) == {"waiver_pdfs": 0, "attachments": 0, "signed": 0, "unrecoverable": 0}


def test_cancel_sent_waiver_drafts_disregard_note_and_stops_reminders(mail):
    db = mail.db
    w, e = send_request(mail)
    note = service.void(db, w, "ap", "invoice was rejected", notify_sub=True)
    db.commit()
    assert w.status == WaiverStatus.VOID
    assert note.kind == "cancelled" and note.status == "draft" and note.provider_thread_id == e.provider_thread_id
    assert "Please disregard the unconditional partial lien waiver" in note.body_text
    assert sum(reminders.run(db, today=__import__("datetime").date(2026, 11, 30)).values()) == 0

    # Brought back: a draft again, with a new request email.
    service.restore(db, w, "ap")
    db.commit()
    assert w.status == WaiverStatus.DRAFT and w.sent_at is None
    drafts = db.scalars(select(EmailMessage).where(EmailMessage.waiver_id == w.id,
                                                   EmailMessage.kind == "waiver_request",
                                                   EmailMessage.status == "draft")).all()
    assert len(drafts) == 1


def test_bulk_cancel_from_waivers_list(mail):
    db = mail.db
    w1 = pay(mail, 1003)
    [w2] = service.prepare_past(db, [service.past_paid_invoices(db)[0].payable.id], "ap")
    db.commit()
    with TestClient(create_app()) as c:
        page = c.get("/waivers")
        assert "Cancel selected" in page.text
        r = c.post("/waivers/cancel", data={"waiver_id": [str(w1.id), str(w2.id)], "reason": "duplicate",
                                            "back": "/waivers?status=open&msg=old"})
        assert r.status_code == 200 and "Cancelled 2 waivers." in r.text and "old" not in str(r.url)
        assert c.get(f"/waivers/{w1.id}").text.count("Restore it") == 1
    db.expire_all()
    assert {db.get(Waiver, w1.id).status, db.get(Waiver, w2.id).status} == {"void"}
    assert all(m.status == "discarded" for m in db.scalars(select(EmailMessage).where(
        EmailMessage.waiver_id.in_([w1.id, w2.id]))))
