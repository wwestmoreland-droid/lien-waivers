"""CC our PM and PC on waiver emails."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import EmailMessage, Project, Subcontract
from app.main import create_app
from app.mail import outbound
from tests.fixtures.sheets import PROJECT
from tests.test_email_e2e import mail, pay, request_email  # noqa: F401


def test_pc_comes_from_intake(mail):
    p = mail.db.scalar(select(Project).where(Project.number == PROJECT))
    assert (p.pc_name, p.pc_email) == ("Casey Coordinator", "pc@buildoutpros.com")


def test_cc_pm_and_pc_from_ready_to_send(mail):
    db = mail.db
    w = pay(mail)
    e = request_email(db, w)
    with TestClient(create_app()) as c:
        page = c.get("/").text
        assert "CC PM &amp; PC" in page and f"/email/{e.id}/cc-team" in page
        r = c.post(f"/email/{e.id}/cc-team", data={"back": "/"}, follow_redirects=False)
        assert "Copied" in r.headers["location"]
        assert f"/email/{e.id}/cc-team" not in c.get("/").text  # already on it: no button
    db.expire_all()
    e = db.get(EmailMessage, e.id)
    assert "pmanager@buildoutpros.com" in e.cc_addrs and "pc@buildoutpros.com" in e.cc_addrs
    assert outbound.cc_team(db, e, "ap") == []  # never added twice
    # Sent, then the reminder replies to everyone on the thread: they stay copied.
    outbound.send(db, e, "ap")
    db.commit()
    rem = outbound.draft_reply(db, db.get(EmailMessage, e.id), "reminder_1", everyone=True)
    assert {"pmanager@buildoutpros.com", "pc@buildoutpros.com"} <= set(rem.to_addrs + rem.cc_addrs)


def test_checkbox_on_send_latest_partial(mail):
    db = mail.db
    sc = next(s for s in db.scalars(select(Subcontract)) if s.vendor.name.startswith("Delta")
              and s.project.number == PROJECT)
    with TestClient(create_app()) as c:
        assert 'name="cc_team"' in c.get(f"/jobs/{PROJECT}").text
        r = c.post(f"/subs/{sc.id}/latest-partial", data={"send": "1", "cc_team": "1"}, follow_redirects=False)
        assert "was%20sent" in r.headers["location"]
    db.expire_all()
    e = db.scalars(select(EmailMessage).where(EmailMessage.kind == "waiver_request")
                   .order_by(EmailMessage.id.desc())).first()
    assert e.status == "sent" and "pc@buildoutpros.com" in e.cc_addrs


def test_without_the_checkbox_nobody_extra_is_copied(mail):
    db = mail.db
    sc = next(s for s in db.scalars(select(Subcontract)) if s.vendor.name.startswith("Delta")
              and s.project.number == PROJECT)
    with TestClient(create_app()) as c:
        c.post(f"/subs/{sc.id}/latest-partial", data={"send": ""}, follow_redirects=False)
    e = db.scalars(select(EmailMessage).where(EmailMessage.kind == "waiver_request")
                   .order_by(EmailMessage.id.desc())).first()
    assert "pc@buildoutpros.com" not in (e.cc_addrs or [])
