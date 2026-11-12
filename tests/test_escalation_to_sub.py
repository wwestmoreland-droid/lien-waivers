"""The last follow-up goes to the sub and copies their other contacts, never our PM."""
from datetime import date, datetime, timezone

from sqlalchemy import select

from app.db.models import Contact, EmailMessage, Waiver
from app.mail import outbound, reminders
from app.services.settings_store import set_setting
from tests.test_email_e2e import mail, send_request  # noqa: F401


def W(db, w):
    db.expire_all()
    return db.get(Waiver, w.id)


def test_copies_everyone_on_file_at_the_sub(mail):
    db = mail.db
    w, e = send_request(mail)
    sc = W(db, w).subcontract
    sc.foreman_email = "foreman@deltafire.com"
    sc.estimator_email = "pmanager@buildoutpros.com"  # one of ours typed into the sub's row: never copied
    db.add(Contact(email="owner@deltafire.com", vendor_id=sc.vendor_id, status="confirmed", source="email"))
    db.add(Contact(email="maybe@deltafire.com", vendor_id=sc.vendor_id, status="new", source="email"))
    db.commit()
    esc = outbound.draft_escalation(db, W(db, w))
    everyone = set(esc.to_addrs + esc.cc_addrs)
    assert {"ap@deltafire.com", "pm@deltafire.com", "foreman@deltafire.com", "owner@deltafire.com"} <= everyone
    assert "maybe@deltafire.com" not in everyone  # not confirmed yet
    assert not [a for a in everyone if a.endswith("@buildoutpros.com")]


def test_old_internal_escalation_drafts_are_replaced(mail):
    db = mail.db
    w, e = send_request(mail)
    ww = W(db, w)
    ww.sent_at = datetime(2026, 10, 1, 15, tzinfo=timezone.utc)
    ww.reminder_count = 2
    old = outbound.draft_free(db, "system:mail", ["pmanager@buildoutpros.com"], [], "Waiver still out", "Pat,")
    old.kind, old.waiver_id = "escalation", w.id
    db.commit()
    reminders.run(db, date(2026, 10, 14))
    db.expire_all()
    assert db.get(EmailMessage, old.id).status == "discarded"
    new = db.scalars(select(EmailMessage).where(EmailMessage.kind == "escalation", EmailMessage.status == "draft")).all()
    assert len(new) == 1 and "ap@deltafire.com" in new[0].to_addrs


def test_an_old_pm_style_custom_template_isnt_used(mail):
    db = mail.db
    set_setting(db, "email_templates", {"escalation": "Subject: Waiver still out\\n\\n{pm_first_name},\\n\\nCall them.\\n"}, "ap")
    db.commit()
    w, e = send_request(mail)
    esc = outbound.draft_escalation(db, W(db, w))
    assert "copied others at" in esc.body_text and "Call them" not in esc.body_text


def test_sent_outside_the_app_gets_a_fresh_email(mail):
    db = mail.db
    from tests.test_email_e2e import pay, request_email
    w = pay(mail)
    request_email(db, w).status = "discarded"  # it went out some other way
    db.commit()
    esc = outbound.draft_escalation(db, W(db, w))
    assert esc.to_addrs == ["ap@deltafire.com"] and esc.attachments and esc.provider_thread_id is None
