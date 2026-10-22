"""End to end with the fake mail provider: request -> reply -> verification -> accept/reject,
reminders, bounces, read-only ap@ source, broken token, Sender switch."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.models import EmailMessage, Mailbox, Payable, Project, ReviewItem, Waiver
from app.mail import inbound, mailboxes, outbound, reminders, verify
from app.mail.fake import FakeMailProvider
from app.mail.provider import InAttachment
from app.services.projects import set_tracked
from app.services.settings_store import set_setting
from app.smartsheet.sync import run_sync
from app.waivers import service as waivers
from app.waivers.types import WaiverStatus
from tests.fixtures.sheets import PROJECT

PDF = b"%PDF-1.4 signed waiver"
CONTRACTS = "contracts@buildoutpros.com"


@pytest.fixture
def mail(db, source):
    FakeMailProvider.reset()
    mailboxes.set_provider_factory(lambda mb: FakeMailProvider(mb.email))
    run_sync(db, source, trigger="test")
    p = db.scalar(select(Project).where(Project.number == PROJECT))
    p.pm_email, p.pm_name = "pmanager@buildoutpros.com", "Pat Manager"
    set_tracked(db, p, True, "t")
    db.commit()
    run_sync(db, source, trigger="test")
    sender = mailboxes.connect(db, CONTRACTS, "sender", "refresh-1", "gmail.modify gmail.send", "admin")
    sender.signature = "Will Westmoreland\nBuildout Pros"
    db.commit()
    yield SimpleNamespace(db=db, source=source, sender=sender, box=FakeMailProvider.boxes[CONTRACTS])
    mailboxes.set_provider_factory(None)


def pay(mail, row_id=1003, day="2026-10-22"):
    mail.source.row("ar_ap", row_id).values["paid_date"] = day
    run_sync(mail.db, mail.source, trigger="test")
    mail.db.expire_all()
    return mail.db.scalar(select(Waiver).join(Payable).where(Payable.source_row_id == row_id))


def request_email(db, w) -> EmailMessage:
    return db.scalar(select(EmailMessage).where(EmailMessage.waiver_id == w.id, EmailMessage.kind == "waiver_request"))


def test_waiver_request_drafted_and_sent(mail):
    db = mail.db
    w = pay(mail)
    e = request_email(db, w)
    assert e.status == "draft" and e.mailbox_id == mail.sender.id
    assert e.to_addrs == ["ap@deltafire.com"] and e.cc_addrs == ["pm@deltafire.com", "est@deltafire.com"]
    assert e.subject.endswith(f"[LW-{w.id}]") and e.body_text.startswith("Hi Dana,")
    assert len(e.attachments) == 1 and e.attachments[0].filename.endswith(".pdf")
    assert mail.box.sent == []  # review before send

    assert outbound.send(db, e, "ap@buildoutpros.com")
    db.commit()
    assert e.status == "sent" and e.provider_thread_id
    assert w.status == WaiverStatus.SENT and w.sent_at
    out = mail.box.sent[0]
    assert out.from_addr == CONTRACTS and out.attachments[0].data.startswith(b"%PDF")
    assert mail.box.thread_labels[e.provider_thread_id]  # "Lien Waivers" label applied

    # Re-sync never creates a second request.
    run_sync(db, mail.source, trigger="test")
    assert db.scalars(select(EmailMessage).where(EmailMessage.kind == "waiver_request")).all() == [e]


def send_request(mail, row_id=1003):
    w = pay(mail, row_id)
    e = request_email(mail.db, w)
    outbound.send(mail.db, e, "ap")
    mail.db.commit()
    return w, e


def test_reply_in_thread_is_received_and_reject_drafts_correction(mail):
    db = mail.db
    w, e = send_request(mail)
    mail.box.deliver(from_addr="ap@deltafire.com", from_name="Dana AP", subject="Re: " + e.subject,
                     thread_id=e.provider_thread_id, in_reply_to=e.rfc_message_id,
                     attachments=[InAttachment("signed.pdf", "application/pdf", PDF)])
    inbound.poll_all(db)
    db.expire_all()
    w = db.get(Waiver, w.id)
    reply = db.scalar(select(EmailMessage).where(EmailMessage.direction == "in"))
    assert reply.waiver_id == w.id and reply.match_method == "thread"
    assert w.status == WaiverStatus.RECEIVED and w.signed_pdf_path
    assert w.verification["status"] == "not_checked"  # no API key in tests

    correction = waivers.reject_and_reply(db, w, "ap", "the notary section is blank")
    db.commit()
    assert w.status == WaiverStatus.REJECTED
    assert correction.status == "draft" and correction.provider_thread_id == e.provider_thread_id
    assert correction.in_reply_to == reply.rfc_message_id and correction.to_addrs == ["ap@deltafire.com"]
    assert "because the notary section is blank" in correction.body_text

    outbound.send(db, correction, "ap")
    # Corrected copy arrives; accept it.
    mail.box.deliver(from_addr="ap@deltafire.com", subject="Re: fixed", thread_id=e.provider_thread_id,
                     attachments=[InAttachment("signed2.pdf", "application/pdf", PDF)])
    inbound.poll_all(db)
    db.expire_all()
    w = db.get(Waiver, w.id)
    assert w.status == WaiverStatus.RECEIVED
    waivers.accept_and_ack(db, w, "ap")
    db.commit()
    assert w.status == WaiverStatus.ACCEPTED


def test_token_and_sender_matching_and_auto_reply(mail):
    db = mail.db
    w, e = send_request(mail)
    # Out-of-office in thread: logged, no status change.
    mail.box.deliver(from_addr="ap@deltafire.com", subject="Automatic reply: " + e.subject,
                     thread_id=e.provider_thread_id, headers={"Auto-Submitted": "auto-replied"},
                     attachments=[InAttachment("logo.png", "image/png", b"png")])
    # New thread with the token in the subject, no attachment: logged only.
    mail.box.deliver(from_addr="someone@deltafire.com", subject=f"question [LW-{w.id}]", body="Which notary?")
    inbound.poll_all(db)
    db.expire_all()
    assert db.get(Waiver, w.id).status == WaiverStatus.SENT
    msgs = db.scalars(select(EmailMessage).where(EmailMessage.direction == "in").order_by(EmailMessage.id)).all()
    assert msgs[0].is_auto_reply and msgs[0].waiver_id == w.id
    assert msgs[1].match_method == "token"

    # Fresh thread from a known vendor domain with the PDF: matched by sender (one open waiver).
    mail.box.deliver(from_addr="office@deltafire.com", subject="signed waiver",
                     attachments=[InAttachment("waiver.pdf", "application/pdf", PDF)])
    inbound.poll_all(db)
    db.expire_all()
    last = db.scalar(select(EmailMessage).where(EmailMessage.direction == "in").order_by(EmailMessage.id.desc()))
    assert last.match_method == "sender" and db.get(Waiver, w.id).status == WaiverStatus.RECEIVED

    # Unrelated mail isn't stored.
    mail.box.deliver(from_addr="news@vendor-marketing.com", subject="Fall sale")
    inbound.poll_all(db)
    assert db.scalar(select(EmailMessage).where(EmailMessage.subject == "Fall sale")) is None


def test_bounce_flags_original_and_vendor(mail):
    db = mail.db
    w, e = send_request(mail)
    mail.box.deliver(from_addr="mailer-daemon@googlemail.com", subject="Delivery Status Notification (Failure)",
                     body="Address not found: ap@deltafire.com", thread_id=e.provider_thread_id)
    inbound.poll_all(db)
    db.expire_all()
    e = db.get(EmailMessage, e.id)
    assert e.status == "bounced" and e.vendor.last_bounce_at
    item = db.scalar(select(ReviewItem).where(ReviewItem.kind == "email_bounced"))
    assert "pm@deltafire.com" in item.message  # other addresses on file are suggested


def test_readonly_source_found_in_ap(mail):
    db = mail.db
    w, e = send_request(mail)
    ro = mailboxes.connect(db, "wwestmoreland@buildoutpros.com", "readonly", "refresh-2", "gmail.readonly", "admin")
    ro.only_to = "ap@buildoutpros.com"
    db.commit()
    box = FakeMailProvider.boxes["wwestmoreland@buildoutpros.com"]
    # Personal mail to Will: ignored.
    box.deliver(from_addr="ap@deltafire.com", subject=f"waiver [LW-{w.id}]", to=["wwestmoreland@buildoutpros.com"],
                attachments=[InAttachment("w.pdf", "application/pdf", PDF)])
    # Mail sent to ap@ (forwarded to Will): kept and tagged, but not auto-filed.
    box.deliver(from_addr="ap@deltafire.com", subject=f"waiver [LW-{w.id}]", to=["ap@buildoutpros.com"],
                recipients_all=["ap@buildoutpros.com", "wwestmoreland@buildoutpros.com"],
                attachments=[InAttachment("w.pdf", "application/pdf", PDF)])
    inbound.poll_all(db)
    db.expire_all()
    found = db.scalars(select(EmailMessage).where(EmailMessage.mailbox_id == ro.id)).all()
    assert len(found) == 1 and found[0].found_in == "ap@" and found[0].waiver_id == w.id
    assert db.get(Waiver, w.id).status == WaiverStatus.SENT  # a person files it
    assert box.thread_labels == {} and box.read == {}         # read-only mailbox untouched
    assert inbound.receive_waiver_copy(db, db.get(Waiver, w.id), found[0], "ap")
    assert db.get(Waiver, w.id).status == WaiverStatus.RECEIVED


def test_reminders_and_escalation(mail):
    db = mail.db
    w, e = send_request(mail)
    sent = datetime(2026, 10, 1, 15, tzinfo=timezone.utc)
    w.sent_at = sent
    db.commit()
    assert reminders.run(db, date(2026, 10, 3)) == {"reminder_1": 0, "reminder_2": 0, "escalation": 0}
    reminders.run(db, date(2026, 10, 4))
    r1 = db.scalar(select(EmailMessage).where(EmailMessage.kind == "reminder_1"))
    assert r1.provider_thread_id == e.provider_thread_id and r1.attachments and "attached again" in r1.body_text
    reminders.run(db, date(2026, 10, 4))
    assert len(db.scalars(select(EmailMessage).where(EmailMessage.kind == "reminder_1")).all()) == 1
    outbound.send(db, r1, "ap")
    reminders.run(db, date(2026, 10, 8))
    r2 = db.scalar(select(EmailMessage).where(EmailMessage.kind == "reminder_2"))
    assert "We still don't have the signed lien waiver" in r2.body_text and "on hold" not in r2.body_text
    reminders.run(db, date(2026, 10, 11))
    esc = db.scalar(select(EmailMessage).where(EmailMessage.kind == "escalation"))
    assert esc.to_addrs == ["pmanager@buildoutpros.com"] and esc.body_text.startswith("Pat,")
    assert "Their billing contact is Dana AP, ap@deltafire.com." in esc.body_text  # empty phone left out


def test_auto_send_setting(mail):
    db = mail.db
    set_setting(db, "auto_send", {"waiver_request": True}, "admin")
    db.commit()
    w = pay(mail)
    assert request_email(db, w).status == "sent" and w.status == WaiverStatus.SENT


def test_broken_token_queues_then_sends(mail):
    db = mail.db
    w = pay(mail)
    e = request_email(db, w)
    mail.box.broken = True
    assert not outbound.send(db, e, "ap")
    assert e.status == "queued" and mail.sender.status == "broken"
    assert mailboxes.broken_mailboxes(db) == [mail.sender]
    mail.box.broken = False
    mailboxes.connect(db, CONTRACTS, "sender", "refresh-3", "gmail.modify gmail.send", "admin")
    assert outbound.send_queued(db) == 1 and e.status == "sent"


def test_sender_switch_keeps_threads_on_old_mailbox(mail):
    db = mail.db
    w, e = send_request(mail)
    new = mailboxes.connect(db, "ap2@buildoutpros.com", "sender", "refresh-4", "gmail.modify gmail.send", "admin")
    db.commit()
    assert mail.sender.role == "listening" and new.role == "sender"
    reply = outbound.draft_reply(db, e, "reminder_1")
    assert reply.mailbox_id == mail.sender.id  # thread stays on the mailbox that started it
    outbound.send(db, reply, "ap")
    assert mail.box.sent[-1].thread_id == e.provider_thread_id
    # New requests go from the new Sender.
    mail.source.row("ar_ap", 1007).values["approval"] = "Approved"  # Summit invoice off Hold, then paid
    w2 = pay(mail, 1007)
    assert request_email(db, w2).mailbox_id == new.id


def test_verify_compares_against_request(mail):
    db = mail.db
    w = pay(mail)

    class FakeClient:
        class beta:
            class messages:
                @staticmethod
                def create(**kw):
                    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
                    text = ('{"is_lien_waiver": true, "appears_signed": true, "notary_completed": false,'
                            ' "waiver_type": "unconditional_partial", "amount": "627.30", "through_date": "2026-10-31",'
                            ' "project_name_on_form": "Studio Co", "project_matches": true, "signer_company": "Delta",'
                            ' "is_supplier_waiver": false, "issues": [], "summary": "ok"}')
                    return SimpleNamespace(stop_reason="end_turn", model=kw["model"],
                                           content=[SimpleNamespace(type="text", text=text)])

    att = request_email(db, w).attachments[0]
    result = verify.verify(w, att, client=FakeClient())
    assert result["status"] == "issues" and result["problems"] == ["the notary section is blank"]
