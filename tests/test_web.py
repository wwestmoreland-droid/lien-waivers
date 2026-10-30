import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AuditLog, Payable, Project, Vendor, VendorAlias
from app.main import create_app
from app.services import sync_runner
from app.web import routes
from tests.fixtures.sheets import PROJECT


@pytest.fixture
def client(engine, source, monkeypatch):
    sync_runner.set_source_factory(lambda: source)
    # Run "background" syncs inline so tests are deterministic.
    monkeypatch.setattr(routes, "_start_background_sync",
                        lambda trigger, backfill=None: sync_runner.sync_now(trigger, backfill) or True)
    with TestClient(create_app()) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_full_phase1_flow(client, db):
    assert client.get("/").status_code == 200  # empty dashboard renders
    sync_runner.sync_now("test")

    r = client.get("/projects")
    assert r.status_code == 200 and PROJECT in r.text and "New – not tracked" in r.text

    r = client.post(f"/projects/{PROJECT}/track")
    assert r.status_code == 200 and "Tracking" in r.text

    for path in ("/", "/jobs", "/jobs?by=sub", "/jobs?by=pm", f"/jobs/{PROJECT}", "/queues", "/settings", "/sync-runs"):
        assert client.get(path).status_code == 200, path

    sub_id = db.scalar(select(Payable).where(Payable.source_row_id == 1001)).subcontract_id
    r = client.get(f"/subs/{sub_id}")
    assert r.status_code == 200 and "$4,148.60" in r.text

    csv = client.get("/reports/ctc.csv")
    assert csv.status_code == 200 and "Delta Fire Systems, Inc" in csv.text
    xlsx = client.get(f"/reports/ctc.xlsx?project={PROJECT}")
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"

    # Unmatched queue: mark BP Waste Services as a direct purchase on this job.
    bp = db.scalar(select(Payable).where(Payable.source_row_id == 1010))
    assert "BP Waste Services" in client.get("/queues").text
    r = client.post(f"/queues/payables/{bp.id}/direct-purchase", data={"scope": "job"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Payable, bp.id).match_status == "direct_purchase"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "vendor.direct_purchase"))


def test_map_vendor_from_queue(client, db, source):
    sync_runner.sync_now("test")
    client.post(f"/projects/{PROJECT}/track")
    source.row("ar_ap", 1004).values["vendor_and_project"] = "Ridge Plumbing & Heating"
    sync_runner.sync_now("test")
    p = db.scalar(select(Payable).where(Payable.source_row_id == 1004))
    assert p.match_status == "unmatched_vendor"
    ridge = db.scalar(select(Vendor).where(Vendor.name == "Ridge Plumbing"))
    client.post(f"/queues/payables/{p.id}/map-vendor", data={"vendor_id": ridge.id})
    db.expire_all()
    assert db.get(Payable, p.id).match_status == "matched"
    assert db.scalar(select(VendorAlias).where(VendorAlias.alias == "Ridge Plumbing & Heating"))


def test_untrack_asks_for_confirmation_when_items_open(client, db):
    sync_runner.sync_now("test")
    client.post(f"/projects/{PROJECT}/track")
    r = client.post(f"/projects/{PROJECT}/untrack")
    assert "Untrack anyway" in r.text
    assert db.scalar(select(Project).where(Project.number == PROJECT)).is_tracked
    r = client.post(f"/projects/{PROJECT}/untrack", data={"confirm": "yes"})
    db.expire_all()
    assert not db.scalar(select(Project).where(Project.number == PROJECT)).is_tracked


def test_pm_role_cannot_track(client, db):
    sync_runner.sync_now("test")
    from app.db.models import User
    client.get("/")
    u = db.scalar(select(User))
    u.role = "PM"
    db.commit()
    assert client.post(f"/projects/{PROJECT}/track").status_code == 403


def test_smartsheet_client_refuses_writes():
    import httpx

    from app.smartsheet.client import _ReadOnlyTransport

    t = _ReadOnlyTransport()
    with pytest.raises(PermissionError):
        t.handle_request(httpx.Request("PUT", "https://api.smartsheet.com/2.0/sheets/1/rows"))


def test_waiver_pages_and_actions(client, db, source):
    from app.db.models import Waiver
    sync_runner.sync_now("test")
    client.post(f"/projects/{PROJECT}/track")
    source.row("ar_ap", 1003).values["paid_date"] = "2026-10-22"
    sync_runner.sync_now("test")
    w = db.scalar(select(Waiver))
    assert w is not None

    for path in ("/waivers", "/waivers?status=draft", f"/waivers/{w.id}", "/settings/templates", "/"):
        assert client.get(path).status_code == 200, path
    r = client.get(f"/waivers/{w.id}/pdf")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    r = client.get("/settings/templates/calibrate/default/conditional_final")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"

    client.post(f"/waivers/{w.id}/through-date", data={"value": "2026-10-15"})
    client.post(f"/waivers/{w.id}/mark-sent")
    client.post(f"/waivers/{w.id}/received", files={"file": ("signed.pdf", b"%PDF-1.4 x", "application/pdf")})
    client.post(f"/waivers/{w.id}/accept")
    db.expire_all()
    w = db.get(Waiver, w.id)
    assert w.status == "accepted" and w.pdf_version == 2
    assert client.get(f"/waivers/{w.id}/signed").status_code == 200

    # Sent waivers can't be edited.
    r = client.post(f"/waivers/{w.id}/through-date", data={"value": "2026-10-01"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]

    # The job page's sub card shows the waiver that counts.
    page = client.get(f"/jobs/{PROJECT}").text
    assert "Unconditional partial, signed" in page and '<span class="badge ok">accepted</span>' in page


def test_conditional_partial_flag_needs_template(client, db):
    r = client.post("/settings/flags", data={"conditional_partial": "on"}, follow_redirects=False)
    assert "Add%20a%20conditional%20partial%20template" in r.headers["location"]


def test_email_pages(client, db, source):
    from app.db.models import EmailMessage
    from app.mail import mailboxes
    from app.mail.fake import FakeMailProvider
    from app.mail.provider import InAttachment

    FakeMailProvider.reset()
    mailboxes.set_provider_factory(lambda mb: FakeMailProvider(mb.email))
    try:
        sync_runner.sync_now("test")
        client.post(f"/projects/{PROJECT}/track")
        mailboxes.connect(db, "contracts@buildoutpros.com", "sender", "tok", "gmail.modify gmail.send", "admin")
        db.commit()
        for path in ("/email", "/email?view=outbox", "/email?view=sent", "/settings/mailboxes", "/settings/email",
                     "/digest", "/queues", "/email/all"):
            assert client.get(path).status_code == 200, path

        source.row("ar_ap", 1003).values["paid_date"] = "2026-10-22"
        sync_runner.sync_now("test")
        draft = db.scalar(select(EmailMessage).where(EmailMessage.kind == "waiver_request"))
        r = client.get(f"/email/{draft.id}/edit")
        assert r.status_code == 200 and "LW-" in r.text
        r = client.post(f"/email/{draft.id}/edit", data={"to": "ap@deltafire.com", "cc": "", "subject": draft.subject,
                                                         "body": draft.body_text, "action": "send"},
                        follow_redirects=False)
        assert r.status_code == 303 and "sent=1" in r.headers["location"]
        db.expire_all()
        draft = db.get(EmailMessage, draft.id)
        assert draft.status == "sent"

        box = FakeMailProvider.boxes["contracts@buildoutpros.com"]
        box.deliver(from_addr="ap@deltafire.com", subject="Re: " + draft.subject, thread_id=draft.provider_thread_id,
                    attachments=[InAttachment("signed.pdf", "application/pdf", b"%PDF-1.4 x")])
        sync_runner.mail_now()
        reply = db.scalar(select(EmailMessage).where(EmailMessage.direction == "in"))
        r = client.get(f"/email/{reply.id}")
        assert r.status_code == 200 and "signed.pdf" in r.text
        assert client.get(f"/email/attachment/{reply.attachments[0].id}").content.startswith(b"%PDF")
        assert "Verification (1)" in client.get("/queues").text
        r = client.post(f"/waivers/{draft.waiver_id}/reject", data={"reason": "it isn't signed", "custom_reason": ""})
        assert r.status_code == 200
        assert db.scalar(select(EmailMessage).where(EmailMessage.kind == "correction")).status == "draft"

        r = client.post("/settings/email/template/partial", data={"text": "Hi,\n\nKindly sign — thanks!\n"},
                        follow_redirects=False)
        assert "Not%20saved" in r.headers["location"]
    finally:
        mailboxes.set_provider_factory(None)


def test_pm_role_cannot_see_email(client, db):
    from app.db.models import User
    client.get("/")
    u = db.scalar(select(User))
    u.role = "PM"
    db.commit()
    for path in ("/email", "/email/all", "/settings/mailboxes", "/digest", "/email/attachment/1"):
        assert client.get(path).status_code == 403, path


def test_attachments_never_served_as_html(client, db, tmp_path):
    from app.db.models import EmailAttachment, EmailMessage
    e = EmailMessage(direction="in", kind="inbound", status="received", subject="x")
    db.add(e)
    db.flush()
    evil = tmp_path / "invoice.html"
    evil.write_text("<script>alert(1)</script>")
    pdf = tmp_path / "w.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    a1 = EmailAttachment(email_id=e.id, filename="invoice.html", content_type="text/html", path=str(evil))
    a2 = EmailAttachment(email_id=e.id, filename="w.pdf", content_type="text/html", path=str(pdf))
    db.add_all([a1, a2])
    db.commit()
    r = client.get(f"/email/attachment/{a1.id}")
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff" and "sandbox" in r.headers["content-security-policy"]
    r = client.get(f"/email/attachment/{a2.id}")
    assert r.headers["content-type"] == "application/pdf"  # sender's claimed type ignored


def test_cross_site_post_refused(client):
    r = client.post(f"/projects/{PROJECT}/track", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.get("/health").headers["x-frame-options"] == "SAMEORIGIN"


def test_unsafe_production_settings_refuse_to_start():
    import pytest as _pytest

    from app.config import Settings
    from app.web.security import check_production_settings
    base = dict(app_base_url="https://lien-waivers-production.up.railway.app", google_client_id="id",
                google_client_secret="s", secret_key="x" * 64, dev_auth_email="", mail_provider="gmail")
    base["database_url"] = "postgresql://u:p@db:5432/railway"
    check_production_settings(Settings(**base))
    for bad in (dict(secret_key="dev-only-change-me"), dict(dev_auth_email="a@buildoutpros.com"),
                dict(google_client_id=""), dict(mail_provider="fake"), dict(database_url="sqlite:////app/data/x.db"),
                dict(database_url="${{Postgres.DATABASE_URL}}"), dict(database_url="  "),
                dict(database_url="mysql://x")):
        with _pytest.raises(RuntimeError):
            check_production_settings(Settings(**(base | bad)))


def test_unsafe_settings_lock_the_app_instead_of_crashing():
    from fastapi.testclient import TestClient as TC

    from app.web.security import locked_app
    c = TC(locked_app(["SECRET_KEY must be set to a long random value (at least 32 characters)."]))
    r = c.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "misconfigured"
    r = c.get("/email")
    assert r.status_code == 503 and "SECRET_KEY" in r.text
    assert c.post("/projects/x/track").status_code == 503


def test_database_url_quotes_and_spaces_are_cleaned():
    from app.config import Settings
    assert Settings(database_url=' "postgresql://u:p@h/db" ').database_url == "postgresql://u:p@h/db"


# ---------------------------------------------------------------- simple interface (Today, Jobs, More)

def _role(db, role):
    from app.db.models import User
    u = db.scalar(select(User))
    u.role = role
    db.commit()


def test_today_empty_states_for_each_role(client, db):
    client.get("/")
    for role in ("Admin", "AP", "PM"):
        _role(db, role)
        r = client.get("/")
        assert r.status_code == 200, role
        assert "caught up" in r.text and "Nothing to check." in r.text and "No questions." in r.text
        assert ("All sent." in r.text) == (role != "PM")  # PMs don't send
        assert ">Today" in r.text and ">Jobs<" in r.text and ">More<" in r.text and ">Queues<" not in r.text


@pytest.fixture
def mailing(client, db, source):
    from app.mail import mailboxes
    from app.mail.fake import FakeMailProvider

    FakeMailProvider.reset()
    mailboxes.set_provider_factory(lambda mb: FakeMailProvider(mb.email))
    sync_runner.sync_now("test")
    client.post(f"/projects/{PROJECT}/track")
    mailboxes.connect(db, "contracts@buildoutpros.com", "sender", "tok", "gmail.modify gmail.send", "admin")
    db.commit()
    yield FakeMailProvider.boxes["contracts@buildoutpros.com"]
    mailboxes.set_provider_factory(None)


def _pay(source, row_id, day="2026-10-22"):
    source.row("ar_ap", row_id).values["paid_date"] = day
    sync_runner.sync_now("test")


def test_send_and_send_all_from_today(client, db, source, mailing):
    from app.db.models import EmailMessage
    from app.waivers import service
    _pay(source, 1003)
    r = client.get("/")
    assert "Ready to send" in r.text and "Delta Fire Systems" in r.text and "Paid 10/22" in r.text
    e = db.scalar(select(EmailMessage).where(EmailMessage.kind == "waiver_request"))
    r = client.post(f"/email/{e.id}/send", data={"back": "/"})
    assert r.status_code == 200 and str(r.url).endswith("/?msg=Sent.") and len(mailing.sent) == 1
    # More from past invoices (subs with a billing email), then Send all.
    ready = [x.payable.id for x in service.past_paid_invoices(db)
             if x.can_prepare and (x.sc.billing_email or x.sc.vendor.billing_email)]
    service.prepare_past(db, ready, "ap")
    db.commit()
    assert len(ready) >= 2
    from app.services import today
    n = len([t for t in today.ready_to_send(db) if t.sendable])  # the requests, plus any supplier-list requests
    assert n >= len(ready)
    r = client.post("/today/send-all")
    assert f"Sent {n}." in r.text and len(mailing.sent) == 1 + n


def test_cant_send_shows_the_reason(client, db, source, mailing):
    from app.db.models import EmailMessage
    _pay(source, 1003)
    e = db.scalar(select(EmailMessage).where(EmailMessage.kind == "waiver_request"))
    e.to_addrs = []
    db.commit()
    r = client.get("/")
    assert "No email address for this sub." in r.text and ">Fix<" in r.text
    client.post("/today/send-all")
    assert not [m for m in mailing.sent if "Lien waiver" in m.subject]


def test_accept_and_send_back_from_today(client, db, source, mailing):
    from app.db.models import EmailMessage, Waiver
    from app.waivers import service
    from app.waivers.types import WaiverStatus
    _pay(source, 1003)
    ready = [x.payable.id for x in service.past_paid_invoices(db) if x.can_prepare][:1]
    service.prepare_past(db, ready, "ap")
    db.commit()
    w1, w2 = db.scalars(select(Waiver).order_by(Waiver.id)).all()[:2]
    for w in (w1, w2):
        service.mark_sent(db, w, "ap")
        service.mark_received(db, w, "ap", "signed.pdf", b"%PDF-1.4 signed")
    w2.verification = {"status": "issues", "problems": ["the notary section is blank"]}
    db.commit()
    page = client.get("/").text
    assert "Signed copies back" in page and "Accept anyway" in page and "Problem:" in page
    r = client.post(f"/waivers/{w1.id}/accept", data={"back": "/"})
    assert "Accepted" in r.text
    r = client.post(f"/waivers/{w2.id}/reject", data={"back": "/", "reason": "the notary section is blank"})
    assert "Sent back" in r.text
    db.expire_all()
    assert db.get(Waiver, w1.id).status == WaiverStatus.ACCEPTED
    assert db.get(Waiver, w2.id).status == WaiverStatus.REJECTED
    assert "Waiting for a corrected copy." in client.get("/").text


def test_answer_questions_from_today(client, db, source):
    from app.db.models import DirectPurchase, ReviewItem
    from app.services.reviews import upsert_review
    sync_runner.sync_now("test")
    client.post(f"/projects/{PROJECT}/track")
    # Direct purchase: a vendor billing the job with no subcontract.
    bp = db.scalar(select(Payable).where(Payable.source_row_id == 1010))
    bp.match_status = "no_subcontract"
    db.commit()
    page = client.get("/").text
    assert "Material only?" in page and "Yes, no waiver needed" in page
    r = client.post(f"/queues/payables/{bp.id}/direct-purchase", data={"back": "/", "scope": "job"})
    assert "No waiver needed" in r.text and db.scalar(select(DirectPurchase))
    # Retention: is this the retention invoice?
    p = db.scalar(select(Payable).where(Payable.source_row_id == 1003))
    upsert_review(db, "retention_check", "payable", p.id, PROJECT, "hit zero", True)
    db.commit()
    assert "their retention invoice?" in client.get("/").text
    r = client.post(f"/payables/{p.id}/retention", data={"back": "/", "value": "yes"})
    assert r.status_code == 200 and str(r.url).startswith("http://testserver/?msg=")
    db.expire_all()
    assert db.get(Payable, p.id).is_retention_invoice
    # "No, send to PM" hands it to the job's PM and takes it off AP's Today.
    p2 = db.scalar(select(Payable).where(Payable.source_row_id == 1001))
    upsert_review(db, "retention_check", "payable", p2.id, PROJECT, "hit zero", True)
    proj = db.scalar(select(Project).where(Project.number == PROJECT))
    proj.pm_email = "pm@buildoutpros.com"
    db.commit()
    item = db.scalar(select(ReviewItem).where(ReviewItem.ref_id == p2.id, ReviewItem.kind == "retention_check"))
    client.post(f"/queues/review/{item.id}/to-pm")
    db.expire_all()
    assert db.get(ReviewItem, item.id).assignee_email == "pm@buildoutpros.com"
    assert "Pay App 1" not in client.get("/").text.split("Quick questions")[1].split("Waiting on subs")[0]


def test_jobs_page_tracked_and_untracked_and_track(client, db):
    sync_runner.sync_now("test")
    r = client.get("/jobs")
    assert "No jobs tracked yet" in r.text and "other active project" in r.text and PROJECT in r.text
    r = client.post(f"/projects/{PROJECT}/track?view=jobs")
    assert r.status_code == 200 and 'hx-swap-oob="afterbegin:#tracked-list"' in r.text and "History is loading" in r.text
    db.expire_all()
    assert db.scalar(select(Project).where(Project.number == PROJECT)).is_tracked
    r = client.get("/jobs")
    assert "Download waivers" in r.text and "Cost to complete" in r.text
    assert client.get("/jobs?q=nothing-matches").text.count("job-sum") == 0
    r = client.get("/jobs?show=untracked")
    assert "<details class=\"others\" open" in r.text


def test_projects_redirects_and_old_urls_still_load(client, db):
    sync_runner.sync_now("test")
    client.post(f"/projects/{PROJECT}/track")
    r = client.get("/projects", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/jobs?show=untracked"
    for path in ("/queues", "/email", "/waivers", "/more", "/reports/ctc", "/reports/ctc?by=sub", f"/jobs/{PROJECT}",
                 "/jobs?by=pm", "/settings", "/sync-runs", "/waivers-past", "/contacts"):
        assert client.get(path).status_code == 200, path


def test_job_download_with_no_signed_waivers_is_a_cover_page(client, db):
    import io

    from pypdf import PdfReader
    sync_runner.sync_now("test")
    client.post(f"/projects/{PROJECT}/track")
    for q in ("", "?suppliers=1"):
        r = client.get(f"/jobs/{PROJECT}/signed-waivers.pdf{q}")
        assert r.status_code == 200
        pdf = PdfReader(io.BytesIO(r.content))
        assert len(pdf.pages) == 1 and "No signed waiver yet" in pdf.pages[0].extract_text()



def test_job_page_sub_cards_and_unconditional_final(client, db, source):
    from app.db.models import EmailMessage, Subcontract, Waiver
    from app.mail import mailboxes
    from app.mail.fake import FakeMailProvider

    FakeMailProvider.reset()
    mailboxes.set_provider_factory(lambda mb: FakeMailProvider(mb.email))
    try:
        sync_runner.sync_now("test")
        client.post(f"/projects/{PROJECT}/track")
        mailboxes.connect(db, "contracts@buildoutpros.com", "sender", "tok", "gmail.modify gmail.send", "admin")
        db.commit()
        page = client.get(f"/jobs/{PROJECT}").text
        # One card per sub: cost to complete, invoice, waiver, the supplier checkbox next to the name.
        assert page.count('class="subcard"') >= 5 and "Cost to complete" in page and "Latest invoice" in page
        assert "Track suppliers" in page and "Send unconditional final" in page
        assert "Signed waivers by sub" not in page and "Retainage held" in page
        delta = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 101))
        assert "Cost to complete is still" in page  # warned: Delta isn't paid in full

        r = client.post(f"/subs/{delta.id}/unconditional-final", data={"send": "1", "back": f"/jobs/{PROJECT}#subs"})
        assert r.status_code == 200 and "Sent the unconditional final to Delta Fire Systems" in r.text
        uf = db.scalar(select(Waiver).where(Waiver.subcontract_id == delta.id, Waiver.type == "UNCONDITIONAL_FINAL"))
        assert uf.status == "sent" and uf.payable.paid_date is not None
        box = FakeMailProvider.boxes["contracts@buildoutpros.com"]
        assert box.sent and box.sent[-1].attachments and "ap@deltafire.com" in box.sent[-1].to
        # Only one: asking again explains instead of making a second.
        r = client.post(f"/subs/{delta.id}/unconditional-final", data={"send": "1", "back": f"/jobs/{PROJECT}"})
        assert "already has an unconditional final" in r.text
        assert len(db.scalars(select(Waiver).where(Waiver.subcontract_id == delta.id,
                                                   Waiver.type == "UNCONDITIONAL_FINAL")).all()) == 1
        # "Put it in Ready to send" leaves it for Today.
        ridge = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 102))
        r = client.post(f"/subs/{ridge.id}/unconditional-final", data={"send": "", "back": f"/jobs/{PROJECT}"})
        assert "ready under Ready to send" in r.text
        rw = db.scalar(select(Waiver).where(Waiver.subcontract_id == ridge.id, Waiver.type == "UNCONDITIONAL_FINAL"))
        assert db.scalar(select(EmailMessage).where(EmailMessage.waiver_id == rw.id)).status == "draft"
        assert "Ridge Plumbing" in client.get("/").text.split("Signed copies back")[0]
        # A sub with nothing paid can't get one.
        zero = db.scalar(select(Subcontract).where(Subcontract.source_row_id == 106))
        if zero is not None:
            r = client.post(f"/subs/{zero.id}/unconditional-final", data={"send": "1"})
            assert "no paid invoice" in r.text
    finally:
        mailboxes.set_provider_factory(None)
