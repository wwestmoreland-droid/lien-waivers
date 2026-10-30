"""Deleting drafts, and replies that aren't a signed copy yet (e.g. "forwarded to billing")."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import EmailMessage, ReviewItem, Subcontract, Vendor, Waiver
from app.mail import inbound
from app.mail.provider import InAttachment
from app.main import create_app
from app.services import filestore
from app.smartsheet.sync import run_sync
from app.waivers.types import WaiverStatus
from tests.test_email_e2e import PDF, mail, pay, request_email, send_request  # noqa: F401
from tests.test_suppliers import ask_lists, sub


def test_select_and_delete_drafts(mail):
    db = mail.db
    ask_lists(mail)
    w = pay(mail, 1003)
    req = request_email(db, w)
    sd = db.scalar(select(EmailMessage).where(EmailMessage.kind == "supplier_list", EmailMessage.status == "draft"))
    with TestClient(create_app()) as c:
        page = c.get("/").text
        assert "Delete selected" in page and f'value="{req.id}" form="del-form"' in page
        r = c.post("/today/delete-drafts", data={"ids": [str(req.id), str(sd.id)], "back": "/"})
        assert "Deleted 2 drafts." in r.text and "1 waiver was cancelled" in r.text
    db.expire_all()
    assert db.get(EmailMessage, req.id).status == db.get(EmailMessage, sd.id).status == "discarded"
    assert db.get(Waiver, w.id).status == WaiverStatus.VOID
    sc = db.get(Subcontract, int(sd.token.split("-")[1]))
    assert sc.supplier_list_status == "skipped"
    # Neither comes back on the next sync.
    run_sync(db, mail.source, trigger="test")
    assert not db.scalar(select(EmailMessage).where(EmailMessage.waiver_id == w.id, EmailMessage.status == "draft"))
    assert not db.scalar(select(EmailMessage).where(EmailMessage.token == sd.token, EmailMessage.status == "draft"))


def _reply(mail, e, body, attachments, cc=None):
    mail.box.deliver(from_addr="pm@deltafire.com", from_name="Pat", subject="Re: " + e.subject,
                     thread_id=e.provider_thread_id, in_reply_to=e.rfc_message_id, body=body,
                     attachments=attachments, cc=cc)
    inbound.poll_all(mail.db)
    mail.db.expire_all()


def test_our_own_unsigned_copy_coming_back_isnt_reviewed(mail):
    db = mail.db
    w, e = send_request(mail)
    ours = filestore.read(db, w.generated_pdf_path)
    _reply(mail, e, "Forwarding to our billing department, they'll get it signed.\n\nPat",
           [InAttachment("Unconditional_Partial_Lien_Waiver.pdf", "application/pdf", ours)],
           cc=["billing@deltafire.com"])
    w = db.get(Waiver, w.id)
    assert w.status == WaiverStatus.SENT and w.signed_pdf_path is None  # still waiting on the sub
    assert any(ev.action == "reply_not_signed" for ev in w.events)
    q = db.scalar(select(ReviewItem).where(ReviewItem.kind == "waiver_forwarded"))
    assert q is not None and "billing@deltafire.com" in q.message
    with TestClient(create_app()) as c:
        page = c.get("/").text
        assert "Delta Fire Systems, Inc" not in page.split("Signed copies back")[1].split("Quick questions")[0]
        assert "Last reply: They sent back our own unsigned copy." in page
        assert "sent the waiver on to billing@deltafire.com" in page
        r = c.post(f"/queues/review/{q.id}/use-forward")
        assert "waivers will go to billing@deltafire.com from now on" in r.text
    db.expire_all()
    assert db.get(Vendor, w.subcontract.vendor_id).waiver_email == "billing@deltafire.com"
    # The safety net doesn't file it later either.
    inbound.poll_all(db)
    assert db.get(Waiver, w.id).status == WaiverStatus.SENT


def test_reply_saying_not_signed_with_a_real_file(mail):
    """They say it isn't signed but attach a real file. Without the automatic check the app can't
    tell, so it's filed for review with their words shown next to it (never hidden)."""
    db = mail.db
    w, e = send_request(mail)
    _reply(mail, e, "I sent this over to our accounting department. Will get it signed and send it back.",
           [InAttachment("invoice.pdf", "application/pdf", PDF)])
    w = db.get(Waiver, w.id)
    assert w.status == WaiverStatus.RECEIVED
    assert "accounting department" in w.verification["note"]


def test_reply_saying_not_signed_check_agrees(mail, monkeypatch):
    from app.mail import verify
    db = mail.db
    w, e = send_request(mail)
    monkeypatch.setattr(verify, "verify", lambda w_, att, client=None: {
        "status": "issues", "problems": ["it isn't signed"],
        "found": {"is_lien_waiver": True, "appears_signed": False, "notary_completed": False}})
    _reply(mail, e, "Forwarded to our billing department, they will sign it.",
           [InAttachment("w.pdf", "application/pdf", PDF)])
    w = db.get(Waiver, w.id)
    assert w.status == WaiverStatus.SENT
    assert "billing department" in next(ev.note for ev in w.events if ev.action == "reply_not_signed")


def test_a_real_signed_copy_is_still_filed(mail):
    db = mail.db
    w, e = send_request(mail)
    _reply(mail, e, "Signed copy attached.", [InAttachment("signed.pdf", "application/pdf", PDF)])
    assert db.get(Waiver, w.id).status == WaiverStatus.RECEIVED


def test_already_filed_forward_goes_back_to_waiting(mail):
    """Filed before this was recognised: the next mail check puts it back to waiting."""
    from app.waivers import service as waivers
    db = mail.db
    w, e = send_request(mail)
    ours = filestore.read(db, w.generated_pdf_path)
    mail.box.deliver(from_addr="pm@deltafire.com", subject="Re: " + e.subject, thread_id=e.provider_thread_id,
                     body="forwarded to billing", attachments=[InAttachment("w.pdf", "application/pdf", ours)])
    msg = mail.box.messages[mail.box.order[-1]]
    stored = inbound._store(db, mail.sender, msg, direction="in", status="received")
    stored.waiver_id = w.id
    waivers.mark_received(db, w, "system:mail", "w.pdf", ours)  # the old behaviour
    w.verification = {"status": "not_checked", "email_id": stored.id}
    db.commit()
    inbound.poll_all(db)
    db.expire_all()
    w = db.get(Waiver, w.id)
    assert w.status == WaiverStatus.SENT and w.signed_pdf_path is None
    assert any(ev.action == "not_signed" for ev in w.events)


def test_not_signed_yet_button(mail):
    db = mail.db
    w, e = send_request(mail)
    _reply(mail, e, "Here it is.", [InAttachment("signed.pdf", "application/pdf", PDF)])
    assert db.get(Waiver, w.id).status == WaiverStatus.RECEIVED
    sent_before = len(mail.box.sent)
    with TestClient(create_app()) as c:
        assert "Not signed yet" in c.get("/").text
        r = c.post(f"/waivers/{w.id}/not-signed", data={"back": "/"})
        assert "Back to waiting on Delta Fire Systems" in r.text
    db.expire_all()
    assert db.get(Waiver, w.id).status == WaiverStatus.SENT and len(mail.box.sent) == sent_before
    assert not db.scalar(select(EmailMessage).where(EmailMessage.kind == "correction"))  # nothing drafted to the sub


def test_reminders_reply_all_including_people_looped_in(mail):
    """A sub replies on the chain and copies someone in: our follow-ups go to all of them."""
    from datetime import date

    from app.mail import reminders
    db = mail.db
    w, e = send_request(mail)
    first_to, first_cc = list(e.to_addrs), list(e.cc_addrs or [])
    _reply(mail, e, "Looping in our office manager, she handles waivers.",
           [], cc=["office@deltafire.com", "contracts@buildoutpros.com"])
    w = db.get(Waiver, w.id)
    w.sent_at = w.sent_at.replace(year=2026, month=10, day=1)
    db.commit()
    reminders.run(db, date(2026, 10, 8))
    r = db.scalar(select(EmailMessage).where(EmailMessage.kind == "reminder_1"))
    everyone = set(r.to_addrs) | set(r.cc_addrs or [])
    assert set(first_to) <= everyone and set(first_cc) <= everyone
    assert "pm@deltafire.com" in r.to_addrs                 # the person who replied
    assert "office@deltafire.com" in everyone               # the person they looped in
    assert "contracts@buildoutpros.com" not in everyone     # never ourselves
    reply = db.scalar(select(EmailMessage).where(EmailMessage.direction == "in"))
    assert r.in_reply_to == reply.rfc_message_id and r.provider_thread_id == e.provider_thread_id
