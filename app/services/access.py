"""Who can sign in, with which role: invites, access requests, removing and restoring access.

Sign-in is invite-only. A @buildoutpros.com account gets in when it already has access, when
an Admin invited that address, or when it's listed in ADMIN_EMAILS. Anyone else gets a "you
don't have access yet" page, and their attempt shows up as a request an Admin can approve.
Removing someone takes effect on their next click (current_user checks is_active every time).
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Invite, User
from app.services.audit import audit

ROLES = ("Admin", "AP", "PM")
INVITE_DAYS = 14

# What each role can see and do, in plain words (shown on the People page and in invites).
ROLE_INFO = {
    "PM": "Sees Today, Jobs, cost to complete and waivers. Accepts or sends back signed waivers on "
          "their own jobs only. Can't send email or see any email.",
    "AP": "Everything a PM can, on every job, plus: sends waiver requests and reminders, answers "
          "replies, sees waiver email from subs, adds waivers by hand. Can't change settings, "
          "templates or who has access, and can't see the mailbox's other mail.",
    "Admin": "Everything, including settings, email templates, mailboxes and who has access.",
}


class NoAccess(Exception):
    def __init__(self, message: str, requested: bool = False):
        super().__init__(message)
        self.requested = requested


def now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def invite_url(token: str) -> str:
    return get_settings().app_base_url.rstrip("/") + f"/invite/{token}"


def normalize(email: str) -> str:
    email = (email or "").strip().lower()
    domain = get_settings().allowed_domain
    if "@" not in email:
        email = f"{email}@{domain}"
    if not email.endswith("@" + domain) or email.count("@") != 1 or email.startswith("@"):
        raise ValueError(f"Invites are for @{domain} addresses only.")
    return email


def active_admins(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.role == "Admin", User.is_active.is_(True))))


def live_invite(inv: Invite | None) -> bool:
    return inv is not None and inv.status == "pending" and (
        inv.expires_at is None or _aware(inv.expires_at) > now())


def create_invite(db: Session, email: str, role: str, actor: str, name: str | None = None) -> tuple[Invite, str]:
    """A new invite (any earlier pending one for the address is replaced). Returns (invite, token);
    the token is only ever shown once, in the link."""
    email = normalize(email)
    if role not in ROLES:
        raise ValueError("Pick a role.")
    user = db.scalar(select(User).where(User.email == email))
    if user is not None and user.is_active:
        raise ValueError(f"{email} already has access ({user.role}). Change their role in the list below.")
    for old in db.scalars(select(Invite).where(Invite.email == email, Invite.status.in_(("pending", "requested")))):
        old.status = "revoked" if old.status == "pending" else "dismissed"
    token = secrets.token_urlsafe(32)
    inv = Invite(email=email, name=(name or "").strip() or None, role=role, status="pending",
                 token_hash=token_hash(token), created_by=actor, expires_at=now() + timedelta(days=INVITE_DAYS))
    db.add(inv)
    db.flush()
    audit(db, actor, "access.invited", "user", email, role=role)
    return inv, token


def revoke_invite(db: Session, inv: Invite, actor: str) -> None:
    if inv.status in ("pending", "requested"):
        inv.status = "revoked" if inv.status == "pending" else "dismissed"
        audit(db, actor, "access.invite_revoked" if inv.status == "revoked" else "access.request_dismissed",
              "user", inv.email)


def find_by_token(db: Session, token: str | None) -> Invite | None:
    if not token:
        return None
    return db.scalar(select(Invite).where(Invite.token_hash == token_hash(token)))


def sign_in(db: Session, email: str, name: str | None, token: str | None = None) -> User:
    """Decide whether this signed-in Google account gets in. Raises NoAccess otherwise."""
    email = email.lower()
    user = db.scalar(select(User).where(User.email == email))
    inv = find_by_token(db, token)
    if inv is not None and inv.email != email:
        raise NoAccess(f"That invite is for {inv.email}. Sign in with that account, or ask for an invite "
                       f"for {email}.")
    if inv is not None and not live_invite(inv):
        inv = None if (user and user.is_active) else inv
        if inv is not None:
            raise NoAccess("That invite has expired or was withdrawn. Ask for a new one.")
    if inv is None:
        inv = db.scalar(select(Invite).where(Invite.email == email, Invite.status == "pending")
                        .order_by(Invite.id.desc()))
        inv = inv if live_invite(inv) else None

    if user is not None and user.is_active:
        pass
    elif inv is not None or email in get_settings().admin_email_list:
        role = inv.role if inv is not None else "Admin"
        if user is None:
            user = User(email=email, name=name, role=role, is_active=True)
            db.add(user)
            audit(db, inv.created_by if inv else email, "user.created", "user", email, role=role,
                  via="invite" if inv else "admin list")
        else:
            user.is_active, user.role = True, role
            audit(db, inv.created_by if inv else email, "access.restored", "user", email, role=role, via="invite")
        if inv is not None:
            inv.status, inv.accepted_at = "accepted", now()
    else:
        _record_request(db, email, name)
        db.commit()
        removed = user is not None
        raise NoAccess("Your access was removed. Ask an Admin if you need it back." if removed else
                       "You don't have access yet. We've let the Admin know you asked; you'll get an "
                       "invite link if they approve it.", requested=not removed)
    user.last_login_at = now()
    if name and not user.name:
        user.name = name
    db.commit()
    return user


def _record_request(db: Session, email: str, name: str | None) -> None:
    if db.scalar(select(User.id).where(User.email == email, User.is_active.is_(False))):
        return  # removed on purpose: no request
    if db.scalar(select(Invite.id).where(Invite.email == email, Invite.status == "requested")):
        return
    db.add(Invite(email=email, name=name, role="PM", status="requested", created_by=email))
    audit(db, email, "access.requested", "user", email)


def _guard_last_admin(db: Session, target: User, removing_admin: bool) -> None:
    if target.role == "Admin" and removing_admin and len(active_admins(db)) <= 1:
        raise ValueError("There has to be at least one Admin.")


def set_role(db: Session, target: User, role: str, actor: User) -> None:
    if role not in ROLES:
        raise ValueError("Pick a role.")
    if target.id == actor.id:
        raise ValueError("You can't change your own role.")
    _guard_last_admin(db, target, role != "Admin")
    audit(db, actor.email, "user.role_changed", "user", target.email, old=target.role, new=role)
    target.role = role


def remove(db: Session, target: User, actor: User) -> None:
    if target.id == actor.id:
        raise ValueError("You can't remove your own access.")
    _guard_last_admin(db, target, True)
    target.is_active = False
    for inv in db.scalars(select(Invite).where(Invite.email == target.email, Invite.status == "pending")):
        inv.status = "revoked"
    audit(db, actor.email, "access.removed", "user", target.email, role=target.role)


def restore(db: Session, target: User, actor: User) -> None:
    target.is_active = True
    audit(db, actor.email, "access.restored", "user", target.email, role=target.role)


def email_invite(db: Session, inv: Invite, token: str, actor: str) -> tuple[bool, str]:
    """Send the invite from the connected mailbox. (False, why) when it couldn't go out."""
    from app.mail import outbound
    first = (inv.name or "").split(" ")[0]
    body = (f"Hi{(' ' + first) if first else ''},\n\n"
            f"You're invited to the Buildout Pros lien waiver app as {inv.role}. "
            f"{ROLE_INFO[inv.role]}\n\n"
            f"Open this link and sign in with your {inv.email} Google account:\n{invite_url(token)}\n\n"
            f"The link works once and expires in {INVITE_DAYS} days.\n")
    e = outbound.draft_free(db, actor, [inv.email], [], "Your invite to the lien waiver app", body)
    try:
        ok = outbound.send(db, e, actor)
    except ValueError as exc:
        ok, e.error = False, str(exc)
    if ok:
        inv.emailed_at = now()
        e.body_text = e.body_text.replace(invite_url(token), "(invite link removed from the app's copy)")
        return True, ""
    e.status = "discarded"
    return False, e.error or "the mailbox couldn't send it"
