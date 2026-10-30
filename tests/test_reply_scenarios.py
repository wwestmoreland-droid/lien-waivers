"""Reply handling: every hole found by the reply stress runs, as a regression test."""
import io
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models import Contact, EmailMessage, ReviewItem, Subcontract, Waiver
from app.mail import inbound, mailboxes, outbound, reminders
from app.mail.fake import FakeMailProvider
from app.mail.provider import InAttachment
from app.services import filestore
from app.waivers import service as waivers
from app.waivers.types import WaiverStatus
from tests.test_email_e2e import PDF, mail, pay, request_email, send_request  # noqa: F401

SIGNED = InAttachment("signed.pdf", "application/pdf", PDF)


def photo(size=(900, 1200), fmt="JPEG") -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.effect_noise(size, 60).convert("RGB").save(buf, fmt)
    return buf.getvalue()


def go(mail, **kw):
    m = mail.box.deliver(**kw)
    inbound.poll_all(mail.db)
    mail.db.expire_all()
    return m


def reply(mail, e, **kw):
    kw.setdefault("from_addr", "ap@deltafire.com")
    kw.setdefault("subject", "Re: " + e.subject)
    return go(mail, thread_id=e.provider_thread_id, in_reply_to=e.rfc_message_id, **kw)


def W(db, w):
    db.expire_all()
    return db.get(Waiver, w.id)


def asked(db, kind):
    return db.scalar(select(ReviewItem).where(ReviewItem.kind == kind, ReviewItem.status == "open"))


def filed_bytes(db, w):
    return filestore.read(db, W(db, w).signed_pdf_path)


def second_delta_waiver(db):
    [w2] = waivers.prepare_past(db, [r.payable.id for r in waivers.past_paid_invoices(db)
                                     if r.sc and r.sc.vendor.name.startswith("Delta")][:1], "ap")
    db.commit()
    return w2


# ---------------------------------------------------------------- choosing the right file

def test_our_unsigned_copy_plus_signed_files_the_signed_one(mail):
    db = mail.db
    w, e = send_request(mail)
    ours = filestore.read(db, w.generated_pdf_path)
    reply(mail, e, attachments=[InAttachment("original.pdf", "application/pdf", ours), SIGNED])
    assert W(db, w).status == WaiverStatus.RECEIVED and filed_bytes(db, w) == PDF


def test_signature_logo_is_never_the_signed_copy(mail):
    db = mail.db
    w, e = send_request(mail)
    big = photo()
    reply(mail, e, attachments=[InAttachment("image001.png", "image/png", photo((120, 40), "PNG")),
                                InAttachment("IMG_2231.jpg", "image/jpeg", big)])
    assert filed_bytes(db, w) == big


def test_empty_file_isnt_filed(mail):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, attachments=[InAttachment("signed.pdf", "application/pdf", b"")])
    assert W(db, w).status == WaiverStatus.SENT


def test_pdf_without_extension_is_recognised(mail):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, attachments=[InAttachment("signedwaiver", "application/octet-stream", PDF)])
    assert W(db, w).status == WaiverStatus.RECEIVED


def test_iphone_heic_becomes_a_jpeg(mail):
    import pillow_heif
    from PIL import Image
    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.effect_noise((900, 1200), 60).convert("RGB").save(buf, "HEIF")
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, attachments=[InAttachment("IMG_0001.HEIC", "image/heic", buf.getvalue())])
    ww = W(db, w)
    assert ww.status == WaiverStatus.RECEIVED and ww.signed_pdf_path.endswith(".jpg")
    assert filed_bytes(db, w)[:2] == b"\xff\xd8"


def test_two_signed_files_one_filed_and_a_question(mail):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, attachments=[InAttachment("job1.pdf", "application/pdf", PDF + b"1"),
                                InAttachment("job2.pdf", "application/pdf", PDF + b"2")])
    assert W(db, w).status == WaiverStatus.RECEIVED
    q = asked(db, "extra_files")
    assert q is not None and "job2.pdf" in q.message


# ---------------------------------------------------------------- who it's from

def test_confirmed_bookkeeper_on_gmail(mail):
    from app.contacts import service as contacts
    db = mail.db
    w, e = send_request(mail)
    c = Contact(email="deltabooks@gmail.com", source="email", message_count=0)
    db.add(c)
    db.flush()
    contacts.confirm(db, c, "ap", w.subcontract.vendor_id, "billing")
    db.commit()
    go(mail, from_addr="deltabooks@gmail.com", subject="waiver", attachments=[SIGNED])
    assert W(db, w).status == WaiverStatus.RECEIVED


def test_no_reply_service_with_reply_to(mail):
    db = mail.db
    w, e = send_request(mail)
    go(mail, from_addr="quickbooks@notification.intuit.com", subject="Lien waiver from Delta Fire",
       headers={"Reply-To": "ap@deltafire.com"}, attachments=[SIGNED])
    assert W(db, w).status == WaiverStatus.RECEIVED


def test_coworker_forwards_without_tag(mail):
    db = mail.db
    w, e = send_request(mail)
    go(mail, from_addr="npigott@buildoutpros.com", subject="Fwd: Delta Fire lien waiver",
       body="---------- Forwarded message ---------\nFrom: Dana <ap@deltafire.com>\nSigned.", attachments=[SIGNED])
    assert W(db, w).status == WaiverStatus.RECEIVED


def test_another_sub_on_this_thread_is_asked_about(mail):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, from_addr="billing@ridgeplumbing.com", attachments=[SIGNED])
    assert W(db, w).status == WaiverStatus.SENT
    assert asked(db, "other_company") is not None


def test_another_subs_tag_is_asked_about(mail):
    db = mail.db
    w, e = send_request(mail)
    ridge = [r for r in waivers.past_paid_invoices(db) if r.sc and r.sc.vendor.name == "Ridge Plumbing"][:1]
    [w2] = waivers.prepare_past(db, [r.payable.id for r in ridge], "ap")
    waivers.mark_sent(db, w2, "ap")
    db.commit()
    go(mail, from_addr="ap@deltafire.com", subject=f"Signed [LW-{w2.id}]", attachments=[SIGNED])
    assert W(db, w2).status == WaiverStatus.SENT and asked(db, "other_company") is not None


# ---------------------------------------------------------------- threads

def test_broken_thread_found_by_reply_headers(mail):
    db = mail.db
    w, e = send_request(mail)
    go(mail, from_addr="someone@contractor-mail.net", subject="Signed", in_reply_to=e.rfc_message_id,
       attachments=[SIGNED])
    assert W(db, w).status == WaiverStatus.RECEIVED


def test_reply_on_old_accepted_thread_goes_to_the_open_waiver(mail):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, attachments=[SIGNED])
    waivers.accept(db, W(db, w), "ap")
    w2 = second_delta_waiver(db)
    outbound.send(db, request_email(db, w2), "ap")
    db.commit()
    reply(mail, e, attachments=[InAttachment("new signed.pdf", "application/pdf", PDF + b"new")])
    assert W(db, w2).status == WaiverStatus.RECEIVED


def test_final_on_the_partials_thread_goes_to_the_final(mail):
    db = mail.db
    w, e = send_request(mail)
    uf = waivers.prepare_unconditional_final(db, w.subcontract, "ap")
    outbound.send(db, request_email(db, uf), "ap")
    db.commit()
    reply(mail, e, body="Final waiver signed, attached.", attachments=[SIGNED])
    assert W(db, uf).status == WaiverStatus.RECEIVED and W(db, w).status == WaiverStatus.SENT


def test_signed_waiver_on_the_supplier_list_thread(mail):
    from tests.test_suppliers import ask_lists
    db = mail.db
    ask_lists(mail)
    for old in db.scalars(select(Waiver).where(Waiver.status == WaiverStatus.SENT)).all():
        waivers.mark_received(db, old, "ap", "x.pdf", PDF + b"old")
        waivers.accept(db, old, "ap")  # only the new request is open
    db.commit()
    w, e = send_request(mail)
    sd = db.scalar(select(EmailMessage).where(EmailMessage.token == f"SD-{w.subcontract_id}"))
    outbound.send(db, sd, "ap")
    db.commit()
    go(mail, from_addr="ap@deltafire.com", subject="Re: " + sd.subject, thread_id=sd.provider_thread_id,
       body="No suppliers. Signed lien waiver attached too.", attachments=[SIGNED])
    assert W(db, w).status == WaiverStatus.RECEIVED


def test_supplier_list_as_a_new_email(mail):
    from tests.test_suppliers import ask_lists
    db = mail.db
    ask_lists(mail)
    m = go(mail, from_addr="ap@deltafire.com", subject="Our suppliers for Palatine", body="ABC Supply - pipe - $12k")
    s = db.scalar(select(EmailMessage).where(EmailMessage.provider_message_id == m.message_id))
    assert s is not None and s.kind == "supplier_list_reply"


# ---------------------------------------------------------------- the app asks instead of staying quiet

@pytest.mark.parametrize("body,attachments,kind", [
    ("Signed waiver attached, thanks!", [], "nothing_attached"),
    ("Signed through DocuSign: https://www.docusign.net/abc", [], "signed_by_link"),
    ("Here it is", [InAttachment("waiver signed.docx", "application/msword", b"PK..word")], "unreadable_file"),
    ("Here", [InAttachment("waivers.zip", "application/zip", b"PK\x03\x04zip")], "unreadable_file"),
    ("Is this amount right? We were paid $31,000.", [], "sub_replied"),
    ("Can you resend to our new AP person?", [], "sub_replied"),
])
def test_replies_that_need_a_person(mail, body, attachments, kind):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, body=body, attachments=attachments)
    assert W(db, w).status == WaiverStatus.SENT and asked(db, kind) is not None


def test_copy_for_a_cancelled_waiver(mail):
    db = mail.db
    w, e = send_request(mail)
    waivers.void(db, W(db, w), "ap", "wrong")
    db.commit()
    reply(mail, e, attachments=[SIGNED])
    assert asked(db, "copy_for_cancelled") is not None


def test_copy_after_accepted(mail):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, attachments=[SIGNED])
    waivers.accept(db, W(db, w), "ap")
    db.commit()
    reply(mail, e, attachments=[InAttachment("again.pdf", "application/pdf", PDF + b"again")])
    assert asked(db, "copy_after_accepted") is not None


def test_same_file_we_sent_back(mail):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, attachments=[SIGNED])
    waivers.reject(db, W(db, w), "ap", "not notarized")
    db.commit()
    reply(mail, e, body="here", attachments=[SIGNED])
    ww = W(db, w)
    assert ww.status == WaiverStatus.REJECTED
    assert any("same copy we sent back" in (ev.note or "") for ev in ww.events)


def test_real_copy_with_notarized_wording_is_filed(mail):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, body="Signed and notarized, attached. Sorry it took a while to get it notarized.",
          attachments=[SIGNED])
    assert W(db, w).status == WaiverStatus.RECEIVED


# ---------------------------------------------------------------- ap@

def _ro(mail):
    ro = mailboxes.connect(mail.db, "wwestmoreland@buildoutpros.com", "readonly", "r", "gmail.readonly", "a")
    ro.only_to = "ap@buildoutpros.com"
    mail.db.commit()
    return FakeMailProvider.boxes["wwestmoreland@buildoutpros.com"]


@pytest.mark.parametrize("subject", ["Signed [LW-{id}]", "Lien waiver", "Palatine docs"])
def test_ap_copies_reach_a_person(mail, subject):
    db = mail.db
    box = _ro(mail)
    w, e = send_request(mail)
    box.deliver(from_addr="ap@deltafire.com", subject=subject.format(id=w.id), to=["ap@buildoutpros.com"],
                recipients_all=["ap@buildoutpros.com"], attachments=[InAttachment("scan0001.pdf", "application/pdf", PDF)])
    inbound.poll_all(db)
    assert W(db, w).status == WaiverStatus.SENT  # read-only: a person files it
    assert asked(db, "found_in_ap") is not None


# ---------------------------------------------------------------- reminders

def test_reminders_after_send_back(mail):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, attachments=[SIGNED])
    waivers.reject(db, W(db, w), "ap", "not signed")
    db.commit()
    s = reminders.run(db, date.today() if date.today().weekday() < 5 else date(2026, 11, 4))
    assert s["reminder_1"] == 0  # not yet: the clock restarts at the send-back
    from app.db.models import WaiverEvent
    ev = db.scalar(select(WaiverEvent).where(WaiverEvent.waiver_id == w.id, WaiverEvent.action == "rejected"))
    ev.at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    db.commit()
    assert reminders.run(db, date(2026, 10, 8))["reminder_1"] == 1


def test_reminder_for_a_waiver_sent_outside_the_app(mail):
    db = mail.db
    w = pay(mail)
    waivers.mark_sent(db, W(db, w), "ap")
    db.commit()
    ww = W(db, w)
    ww.sent_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    db.commit()
    assert reminders.run(db, date(2026, 10, 8))["reminder_1"] == 1
    r = db.scalar(select(EmailMessage).where(EmailMessage.kind == "reminder_1"))
    assert r.subject.startswith("Reminder: ") and r.to_addrs and r.attachments


def test_reminder_includes_the_new_billing_contact(mail):
    from app.contacts import service as contacts
    db = mail.db
    w, e = send_request(mail)
    contacts.set_waiver_email(db, w.subcontract.vendor, "newap@deltafire.com", "ap")
    db.commit()
    ww = W(db, w)
    ww.sent_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    db.commit()
    reminders.run(db, date(2026, 10, 8))
    r = db.scalar(select(EmailMessage).where(EmailMessage.kind == "reminder_1"))
    assert "newap@deltafire.com" in r.to_addrs


# ---------------------------------------------------------------- suppliers

def test_supplier_named_file_without_the_check(mail):
    from app.suppliers import service as suppliers
    db = mail.db
    delta = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 101))
    abc = suppliers.add(db, delta, "ap", "ABC Supply")
    db.commit()
    w, e = send_request(mail)
    reply(mail, e, body="ABC's waiver attached, ours to follow.",
          attachments=[InAttachment("ABC Supply lien waiver.pdf", "application/pdf", PDF)])
    assert W(db, w).status == WaiverStatus.SENT  # not mistaken for the sub's own
    assert db.scalar(select(Waiver).where(Waiver.supplier_id == abc.id)).status == WaiverStatus.RECEIVED


def test_sub_cant_be_its_own_supplier(mail):
    from app.suppliers import service as suppliers
    db = mail.db
    delta = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 101))
    with pytest.raises(ValueError):
        suppliers.add(db, delta, "ap", "Delta Fire Systems, Inc")
