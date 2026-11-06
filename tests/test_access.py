"""Invite-only sign-in, roles, removing and restoring access."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import EmailMessage, Invite, User
from app.main import create_app
from app.services import access

ADMIN = "wwestmoreland@buildoutpros.com"


@pytest.fixture
def admin(db):
    return access.sign_in(db, ADMIN, "Will Westmoreland")  # on ADMIN_EMAILS


def test_only_invited_people_get_in(db, admin):
    with pytest.raises(access.NoAccess) as exc:
        access.sign_in(db, "sam@buildoutpros.com", "Sam")
    assert exc.value.requested and "let the Admin know" in str(exc.value)
    assert db.scalar(select(User).where(User.email == "sam@buildoutpros.com")) is None
    req = db.scalar(select(Invite).where(Invite.email == "sam@buildoutpros.com"))
    assert req.status == "requested"

    inv, token = access.create_invite(db, "sam", "AP", ADMIN, "Sam Smith")  # bare name gets the domain
    db.commit()
    assert inv.email == "sam@buildoutpros.com" and db.get(Invite, req.id).status == "dismissed"
    assert inv.token_hash != token and len(token) > 30  # only the hash is stored
    u = access.sign_in(db, "sam@buildoutpros.com", "Sam Smith", token)
    assert (u.role, u.is_active) == ("AP", True) and db.get(Invite, inv.id).status == "accepted"
    assert access.sign_in(db, "sam@buildoutpros.com", "Sam Smith").id == u.id  # signs in again normally


def test_invite_is_for_that_address_only(db, admin):
    inv, token = access.create_invite(db, "pat@buildoutpros.com", "PM", ADMIN)
    db.commit()
    with pytest.raises(access.NoAccess, match="That invite is for pat@buildoutpros.com"):
        access.sign_in(db, "someone@buildoutpros.com", None, token)
    with pytest.raises(ValueError, match="buildoutpros.com addresses only"):
        access.create_invite(db, "friend@gmail.com", "PM", ADMIN)


def test_expired_or_withdrawn_invites_dont_work(db, admin):
    inv, token = access.create_invite(db, "old@buildoutpros.com", "PM", ADMIN)
    inv.expires_at = access.now() - timedelta(days=1)
    db.commit()
    with pytest.raises(access.NoAccess, match="expired"):
        access.sign_in(db, "old@buildoutpros.com", None, token)
    inv2, token2 = access.create_invite(db, "gone@buildoutpros.com", "PM", ADMIN)
    access.revoke_invite(db, inv2, ADMIN)
    db.commit()
    with pytest.raises(access.NoAccess):
        access.sign_in(db, "gone@buildoutpros.com", None, token2)


def test_remove_and_restore(db, admin):
    inv, token = access.create_invite(db, "pc@buildoutpros.com", "AP", ADMIN)
    db.commit()
    pc = access.sign_in(db, "pc@buildoutpros.com", "PC", token)
    access.remove(db, pc, admin)
    db.commit()
    with pytest.raises(access.NoAccess, match="removed") as exc:
        access.sign_in(db, "pc@buildoutpros.com", "PC")
    assert not exc.value.requested  # no request for someone removed on purpose
    access.restore(db, pc, admin)
    db.commit()
    assert access.sign_in(db, "pc@buildoutpros.com", "PC").role == "AP"


def test_always_one_admin(db, admin):
    with pytest.raises(ValueError, match="your own"):
        access.remove(db, admin, admin)
    with pytest.raises(ValueError, match="your own role"):
        access.set_role(db, admin, "PM", admin)
    inv, token = access.create_invite(db, "a2@buildoutpros.com", "Admin", ADMIN)
    db.commit()
    a2 = access.sign_in(db, "a2@buildoutpros.com", None, token)
    access.set_role(db, a2, "PM", admin)  # fine: Will is still an Admin
    db.commit()
    with pytest.raises(ValueError, match="at least one Admin"):
        access.set_role(db, admin, "PM", a2)


def test_people_page(client, db):
    client.get("/")  # signs the dev user (Will, Admin) in
    page = client.get("/settings/people")
    assert page.status_code == 200 and "Invite someone" in page.text and "What each role can see and do" in page.text
    r = client.post("/settings/people/invite", data={"email": "new@buildoutpros.com", "role": "AP", "name": "New"},
                    follow_redirects=False)
    assert "link=" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert "/invite/" in page and "new@buildoutpros.com" in page
    token = page.split("/invite/")[1].split('"')[0]
    u = access.sign_in(db, "new@buildoutpros.com", "New", token)
    r = client.post(f"/settings/people/{u.id}/remove", follow_redirects=False)
    db.expire_all()
    assert db.get(User, u.id).is_active is False
    client.post(f"/settings/people/{u.id}/restore")
    db.expire_all()
    assert db.get(User, u.id).is_active is True
    assert "Sign-in is by invite only" in client.get("/settings").text


def test_people_page_is_admin_only(client, db):
    client.get("/")
    me = db.scalar(select(User).where(User.email == ADMIN))
    me.role = "AP"
    db.commit()
    assert client.get("/settings/people").status_code == 403
    assert client.post("/settings/people/invite", data={"email": "x@buildoutpros.com", "role": "Admin"}).status_code == 403


def test_bad_invite_link_explains(client):
    r = client.get("/invite/not-a-real-token", follow_redirects=True)
    assert "expired, was already used or was withdrawn" in r.text


def test_invite_email_keeps_the_link_out_of_the_app(mail):
    db = mail.db
    access.sign_in(db, ADMIN, "Will")
    inv, token = access.create_invite(db, "pc2@buildoutpros.com", "AP", ADMIN, "Casey")
    ok, why = access.email_invite(db, inv, token, ADMIN)
    db.commit()
    assert ok, why
    e = db.scalars(select(EmailMessage).where(EmailMessage.to_addrs.is_not(None)).order_by(EmailMessage.id.desc())).first()
    assert e.status == "sent" and "Hi Casey" in e.body_text and token not in e.body_text
    out = [m for m in mail.box.sent if "pc2@buildoutpros.com" in m.to]
    assert out and token in out[-1].body  # the invitee's own email does carry the link


from tests.test_email_e2e import mail  # noqa: E402,F401
from tests.test_web import client  # noqa: E402,F401
