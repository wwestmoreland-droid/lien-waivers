"""Phase 4: contact logging from email headers and signatures (section 6B)."""
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.contacts import backfill, service
from app.contacts.signature import guess_role, parse
from app.db.models import Contact, ContactBackfill, ContactMessage, Subcontract, Vendor, Waiver
from app.mail import inbound, mailboxes, outbound
from app.mail.fake import FakeMailProvider
from app.main import create_app
from tests.test_email_e2e import CONTRACTS, mail, pay, request_email, send_request  # noqa: F401

SIG = """Hi,

Updated lien waiver attached.

Thanks,
Maria Lopez
Accounts Receivable
Delta Fire Systems, Inc
O: 847-555-0142 | C: (312) 555-9981
Fax 847-555-0143

On Tue, Oct 20, 2026 at 9:14 AM Contracts <contracts@buildoutpros.com> wrote:
> please sign 630-555-1111
"""
NIGHT = datetime(2026, 10, 23, 4, 0, tzinfo=timezone.utc)  # 11 PM Central
DAY = datetime(2026, 10, 22, 17, 0, tzinfo=timezone.utc)   # noon Central


def contact(db, addr):
    db.expire_all()
    return db.scalar(select(Contact).where(Contact.email == addr))


# ---------------------------------------------------------------- signature rules

def test_signature_rules():
    s = parse(SIG, "Maria Lopez")
    assert (s.title, s.company, s.phone, s.mobile) == (
        "Accounts Receivable", "Delta Fire Systems, Inc", "(847) 555-0142", "(312) 555-9981")
    assert guess_role(s.title) == "billing"
    s = parse("Sounds good.\n\nJohn Smith | Project Manager\nAce Drywall LLC\n630.555.1000 x12", "John Smith")
    assert (s.title, s.company, s.phone) == ("Project Manager", "Ace Drywall LLC", "(630) 555-1000 x12")
    assert guess_role("Superintendent") == "foreman" and guess_role("Senior Estimator") == "estimator"
    assert guess_role("President") == "owner" and guess_role("Sales") is None
    assert not parse("ok thanks", "Bob").found_anything


# ---------------------------------------------------------------- Smartsheet + live mail

def test_subcontract_contacts_start_confirmed(mail):
    c = contact(mail.db, "ap@deltafire.com")
    assert c.status == "confirmed" and c.source == "smartsheet" and c.role == "billing"
    assert c.vendor.name == "Delta Fire Systems, Inc"
    assert contact(mail.db, "pm@deltafire.com").role == "pm"
    assert contact(mail.db, "pmanager@buildoutpros.com") is None  # internal people aren't vendor contacts


def test_mail_logs_contacts_by_matching_rules(mail):
    db = mail.db
    box = mail.box
    # Rule 3: same company domain as a known Delta Fire address. Signature parsed; body not kept.
    box.deliver(from_addr="maria@deltafire.com", from_name="Lopez, Maria", subject="Invoice 1234", body=SIG,
                cc=["owner@ridgeplumbing.com"])
    # Public mail domain: never matched by domain.
    box.deliver(from_addr="someone@gmail.com", from_name="Some One", subject="hello", body="hi")
    # Skipped: internal, no-reply, bulk, calendar, auto-reply.
    box.deliver(from_addr="will@buildoutpros.com", subject="note", body="x")
    box.deliver(from_addr="no-reply@procore.com", subject="RFI", body="x")
    box.deliver(from_addr="deals@supplyhouse.com", subject="Sale", body="x", headers={"List-Unsubscribe": "<x>"})
    box.deliver(from_addr="pat@ridgeplumbing.com", subject="Invitation: site walk", body="x")
    box.deliver(from_addr="ooo@ridgeplumbing.com", subject="Automatic reply: waiver", body="x",
                headers={"Auto-Submitted": "auto-replied"})
    inbound.poll_all(db)

    maria = contact(db, "maria@deltafire.com")
    assert maria.name == "Maria Lopez" and maria.vendor.name == "Delta Fire Systems, Inc"
    # Same company for sure (deltafire.com belongs only to Delta Fire), so no one has to confirm it.
    assert maria.match_method == "domain" and maria.status == "confirmed" and maria.confirmed_by == "system:same-domain"
    assert (maria.title, maria.phone, maria.mobile, maria.role) == (
        "Accounts Receivable", "(847) 555-0142", "(312) 555-9981", "billing")
    assert maria.message_count == 1 and maria.parsed_by == "rules"
    cc = contact(db, "owner@ridgeplumbing.com")
    assert cc.vendor.name == "Ridge Plumbing" and cc.status == "confirmed" and cc.role is None
    gm = contact(db, "someone@gmail.com")
    assert gm.vendor_id is None
    for skipped in ("will@buildoutpros.com", "no-reply@procore.com", "deals@supplyhouse.com",
                    "pat@ridgeplumbing.com", "ooo@ridgeplumbing.com", CONTRACTS):
        assert contact(db, skipped) is None, skipped
    assert not hasattr(ContactMessage, "body_text")

    # Known address (rule 1) is confirmed automatically and keeps counting messages.
    box.deliver(from_addr="ap@deltafire.com", from_name="Dana AP", subject="Re: waiver", body="Attached.")
    inbound.poll_all(db)
    dana = contact(db, "ap@deltafire.com")
    assert dana.status == "confirmed" and dana.message_count == 1 and dana.last_seen_at


def test_best_guess_roles_and_only_sure_matches_confirmed(mail):
    db = mail.db
    # Sent a pay app from the company domain, no signature: billing, confirmed.
    mail.box.deliver(from_addr="tom@ridgeplumbing.com", from_name="Tom Reyes", subject="Pay App #3 - Palatine",
                     body="see attached")
    # Signature names the vendor exactly, but writes from Gmail: same company, confirmed.
    mail.box.deliver(from_addr="ridgebooks@gmail.com", from_name="Ann Lee", subject="Invoice 5521",
                     body="Attached.\n\nAnn Lee\nRidge Plumbing\n(312) 555-0188")
    # Only the display name looks like the vendor, on Gmail: a guess, left for a person.
    mail.box.deliver(from_addr="ridgeplumb@gmail.com", from_name="Ridge Plumbing Co", subject="Invoice 9",
                     body="Attached.")
    # A PM title wins over the invoice subject.
    mail.box.deliver(from_addr="pm2@ridgeplumbing.com", from_name="Lou Park", subject="Invoice question",
                     body="Q\n\nLou Park\nProject Manager\nRidge Plumbing")
    inbound.poll_all(db)
    tom = contact(db, "tom@ridgeplumbing.com")
    assert (tom.role, tom.status, tom.vendor.name) == ("billing", "confirmed", "Ridge Plumbing")
    ann = contact(db, "ridgebooks@gmail.com")
    assert (ann.role, ann.status, ann.confirmed_by) == ("billing", "confirmed", "system:company-name")
    guess = contact(db, "ridgeplumb@gmail.com")
    assert guess.vendor.name == "Ridge Plumbing" and guess.status == "new" and guess.role == "billing"
    assert contact(db, "pm2@ridgeplumbing.com").role == "pm"
    # Ridge already has a billing email in All Subcontracts, so where waivers go doesn't change.
    assert db.scalar(select(Vendor).where(Vendor.name == "Ridge Plumbing")).waiver_email is None


def test_existing_unconfirmed_contacts_get_rechecked_on_sync(mail):
    from app.smartsheet.sync import run_sync
    db = mail.db
    mail.box.deliver(from_addr="kim@tristateglass.com", from_name="Kim Ortiz", subject="Invoice 88", body="Attached.")
    inbound.poll_all(db)
    kim = contact(db, "kim@tristateglass.com")
    assert kim.status == "new" and kim.vendor_id is None and kim.role == "billing"  # domain not known yet
    tri = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 104))
    tri.vendor.email_domains = ["tristateglass.com"]
    db.commit()
    run_sync(db, mail.source, trigger="test")
    kim = contact(db, "kim@tristateglass.com")
    assert kim.status == "confirmed" and kim.vendor_id == tri.vendor_id
    # Tri-State has no billing email anywhere, so their invoice sender gets the waiver emails.
    assert service.waiver_email(db.get(Subcontract, tri.id)) == "kim@tristateglass.com"


def test_confirm_learns_domain_and_changes_go_back_to_queue(mail):
    db = mail.db
    acme = db.scalar(select(Vendor).where(Vendor.name == "Summit Electric, LLC"))
    mail.box.deliver(from_addr="jo@summitelec.com", from_name="Jo Park", subject="Q",
                     body="Q\n\nJo Park\nBilling Manager\n(773) 555-0100")
    inbound.poll_all(db)
    jo = contact(db, "jo@summitelec.com")
    assert jo.vendor_id is None  # Summit's only address is @gmail.com, so no domain to go on
    service.confirm(db, jo, "ap@buildoutpros.com", acme.id, None)
    db.commit()
    assert "summitelec.com" in db.get(Vendor, acme.id).email_domains and jo.role == "billing"

    # A coworker at the same domain is now matched by domain.
    mail.box.deliver(from_addr="lee@summitelec.com", subject="hi", body="hi")
    # Jo's phone changes: confirmed contact goes back to the queue with the old value kept.
    mail.box.deliver(from_addr="jo@summitelec.com", from_name="Jo Park", subject="Q2",
                     body="Q\n\nJo Park\nBilling Manager\n(773) 555-0199")
    inbound.poll_all(db)
    assert contact(db, "lee@summitelec.com").vendor_id == acme.id
    jo = contact(db, "jo@summitelec.com")
    assert jo.needs_review and jo.previous == {"phone": "(773) 555-0100"} and jo.phone == "(773) 555-0199"


def test_readonly_source_logs_only_ap_mail(mail):
    db = mail.db
    ro = mailboxes.connect(db, "wwestmoreland@buildoutpros.com", "readonly", "r2", "gmail.readonly", "admin")
    ro.only_to = "ap@buildoutpros.com"
    db.commit()
    box = FakeMailProvider.boxes["wwestmoreland@buildoutpros.com"]
    box.deliver(from_addr="friend@example.com", subject="dinner", body="hi", to=["wwestmoreland@buildoutpros.com"])
    box.deliver(from_addr="ar@ridgeplumbing.com", subject="Invoice", body="see attached",
                to=["ap@buildoutpros.com"], recipients_all=["ap@buildoutpros.com", "wwestmoreland@buildoutpros.com"])
    inbound.poll_all(db)
    assert contact(db, "friend@example.com") is None
    assert contact(db, "ar@ridgeplumbing.com").vendor.name == "Ridge Plumbing"
    assert contact(db, "ap@buildoutpros.com") is None


def test_waiver_email_switch_and_bounce_suggestions(mail):
    db = mail.db
    mail.box.deliver(from_addr="maria@deltafire.com", from_name="Maria Lopez", subject="Invoice", body=SIG)
    inbound.poll_all(db)
    maria = contact(db, "maria@deltafire.com")
    service.confirm(db, maria, "ap", maria.vendor_id, "billing")
    db.commit()
    sc = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 101))
    assert service.waiver_email(sc) == "ap@deltafire.com"
    assert service.billing_suggestion(db, sc).email == "maria@deltafire.com"

    service.set_waiver_email(db, sc.vendor, "maria@deltafire.com", "ap")
    db.commit()
    w = pay(mail, 1003)
    e = request_email(db, w)
    assert e.to_addrs == ["maria@deltafire.com"] and e.body_text.startswith("Hi Maria,")
    assert service.billing_suggestion(db, sc) is None

    # A bounce suggests the vendor's other confirmed contacts.
    outbound.send(db, e, "ap")
    db.commit()
    mail.box.deliver(from_addr="mailer-daemon@googlemail.com", subject="Delivery Status Notification (Failure)",
                     body="Address not found: maria@deltafire.com", thread_id=e.provider_thread_id)
    inbound.poll_all(db)
    from app.db.models import ReviewItem
    r = db.scalar(select(ReviewItem).where(ReviewItem.kind == "email_bounced"))
    assert "ap@deltafire.com" in r.message and "est@deltafire.com" in r.message


# ---------------------------------------------------------------- backfill

def _old_mail(box, n):
    for i in range(n):
        box.deliver(from_addr=f"person{i}@ridgeplumbing.com", from_name=f"Person {i}", subject=f"old {i}",
                    body=f"Hello\n\nPerson {i}\nEstimator\nRidge Plumbing\n(312) 555-01{i:02d}")
    # The same sender many times: only the first (newest) message is opened for the signature.
    for i in range(3):
        box.deliver(from_addr="person0@ridgeplumbing.com", from_name="Person 0", subject=f"again {i}", body="ok")
    for i in range(3):  # a sender who never signs: opened a few times, then left alone
        box.deliver(from_addr="terse@ridgeplumbing.com", subject=f"terse {i}", body="ok")
    box.deliver(from_addr="terse@ridgeplumbing.com", subject="terse 3", body="ok")
    box.deliver(from_addr="terse@ridgeplumbing.com", subject="terse 4", body="ok")


def test_backfill_overnight_throttled_and_resumable(mail):
    db = mail.db
    _old_mail(mail.box, 12)
    bf = backfill.start(db, mail.sender, "ap@buildoutpros.com")
    db.commit()
    assert backfill.run_due(db, at=DAY) == {}  # overnight by default
    assert db.get(ContactBackfill, bf.id).status == "queued"

    # A tiny quota forces several slices; progress is saved between them.
    slices = 0
    while db.get(ContactBackfill, bf.id).status != "done" and slices < 50:
        backfill.run_due(db, at=NIGHT, budget=120)
        slices += 1
    bf = db.get(ContactBackfill, bf.id)
    assert slices > 3 and bf.status == "done" and backfill.progress(bf) == 100
    assert bf.scanned == 20 and bf.contacts_new == 13
    # One body per new sender, plus person0's three one-line replies before the signed message,
    # plus terse's capped attempts. Never a body once a signature is found.
    assert mail.box.full_gets == bf.bodies_fetched == 12 + 3 + 4
    p0 = contact(db, "person0@ridgeplumbing.com")
    assert p0.message_count == 4 and p0.phone == "(312) 555-0100" and p0.role == "estimator" and p0.vendor.name == "Ridge Plumbing"

    # Running it again skips messages already logged.
    gets = mail.box.metadata_gets
    backfill.start(db, mail.sender, "ap", overnight_only=False)
    backfill.run_due(db, at=DAY)
    assert mail.box.metadata_gets == gets and contact(db, "person0@ridgeplumbing.com").message_count == 4


def test_backfill_pauses_when_mailbox_breaks(mail):
    db = mail.db
    _old_mail(mail.box, 2)
    bf = backfill.start(db, mail.sender, "ap", overnight_only=False)
    db.commit()
    mail.box.broken = True
    backfill.run_due(db)
    bf = db.get(ContactBackfill, bf.id)
    assert bf.status == "paused" and "reconnecting" in bf.error
    mail.box.broken = False
    mailboxes.connect(db, CONTRACTS, "sender", "refresh-9", "gmail.modify gmail.send", "admin")
    backfill.set_status(db, bf, "resume", "ap")
    backfill.run_due(db)
    assert db.get(ContactBackfill, bf.id).status == "done"


# ---------------------------------------------------------------- pages

def test_contact_pages(mail):
    db = mail.db
    mail.box.deliver(from_addr="maria@deltafire.com", from_name="Maria Lopez", subject="Invoice", body=SIG)
    mail.box.deliver(from_addr="maybe@gmail.com", from_name="Pat Q", subject="question", body="hi")
    inbound.poll_all(db)
    db.commit()
    maria = contact(db, "maria@deltafire.com")
    maybe = contact(db, "maybe@gmail.com")
    with TestClient(create_app()) as c:
        r = c.get("/queues")
        assert r.status_code == 200 and "maybe@gmail.com" in r.text and "New contacts (1)" in r.text
        for path in ("/contacts", "/contacts?status=review", f"/contacts/{maria.id}", "/settings/mailboxes", "/"):
            assert c.get(path).status_code == 200, path
        csv = c.get("/contacts.csv")
        assert csv.status_code == 200 and "maria@deltafire.com" in csv.text and "(312) 555-9981" in csv.text
        r = c.post(f"/contacts/{maybe.id}", data={"action": "confirm", "vendor_id": str(maria.vendor_id),
                                                  "role": "pm", "back": "/queues#contacts"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/queues#contacts"
        assert contact(db, "maybe@gmail.com").status == "confirmed"
        sc = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 101))
        r = c.get(f"/subs/{sc.id}")
        assert "Send waivers to maria@deltafire.com" in r.text
        c.post(f"/vendors/{sc.vendor_id}/waiver-email", data={"email": "maria@deltafire.com", "back": f"/subs/{sc.id}"})
        db.expire_all()
        assert db.get(Vendor, sc.vendor_id).waiver_email == "maria@deltafire.com"
        r = c.post(f"/settings/mailboxes/{mail.sender.id}/contacts-backfill", data={"action": "start_tonight"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert db.scalar(select(ContactBackfill)).overnight_only


def test_connecting_queues_overnight_backfill(mail):
    bf = mail.db.scalar(select(ContactBackfill).where(ContactBackfill.mailbox_id == mail.sender.id))
    assert bf is not None and bf.status == "queued" and bf.overnight_only


def test_look_up_vendor_in_email_from_sub_page(mail):
    db = mail.db
    tri = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 104))
    assert service.waiver_email(tri) is None  # no billing email in All Subcontracts
    # Billed by email before the mailbox was connected; seen only when we look it up.
    mail.box.deliver(from_addr="kim@tristateglass.com", from_name="Kim Ortiz", subject="Tri-State Glass invoice 88",
                     body="Invoice attached.\n\nKim Ortiz\nAccounts Payable\nTri-State Glass - Mirror\n(708) 555-0123")
    mail.box.deliver(from_addr="books4you@gmail.com", from_name="Sam Books", subject="Tri-State Glass billing",
                     body="hi")
    tri.vendor.email_domains = ["tristateglass.com"]
    db.commit()
    with TestClient(create_app()) as c:
        r = c.post(f"/vendors/{tri.vendor_id}/lookup", data={"back": f"/subs/{tri.id}"})
        assert r.status_code == 200 and "Searched email for Tri-State Glass" in r.text
        assert "kim@tristateglass.com" in r.text and "gets waivers" in r.text  # sent the invoice, same domain
        assert "Add to Tri-State Glass" in r.text and "books4you@gmail.com" in r.text
        kim = contact(db, "kim@tristateglass.com")
        assert kim.status == "confirmed" and kim.role == "billing"
        assert service.waiver_email(db.get(Subcontract, tri.id)) == "kim@tristateglass.com"
        sam = contact(db, "books4you@gmail.com")
        c.post(f"/contacts/{sam.id}/assign", data={"vendor_id": str(tri.vendor_id), "role": "billing",
                                                   "back": f"/subs/{tri.id}"})
        assert contact(db, "books4you@gmail.com").vendor_id == tri.vendor_id
        assert "gmail.com" not in (db.get(Vendor, tri.vendor_id).email_domains or [])
        assert c.get(f"/subs/{tri.id}").status_code == 200


def test_existing_mailboxes_get_a_backfill(mail, monkeypatch):
    from app.services import sync_runner
    db = mail.db
    db.query(ContactBackfill).delete()
    db.commit()
    monkeypatch.setattr(backfill, "is_night", lambda at=None: False)
    sync_runner.contacts_backfill_now()
    db.expire_all()
    bf = db.scalar(select(ContactBackfill).where(ContactBackfill.mailbox_id == mail.sender.id))
    assert bf.status == "queued" and bf.overnight_only
