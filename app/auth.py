"""Google Workspace sign-in, restricted to the company domain, with Admin / AP / PM roles."""
from __future__ import annotations

from datetime import datetime, timezone

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import User
from app.db.session import get_db
from app.services.audit import audit

router = APIRouter()
oauth = OAuth()
_registered = False

ROLES = ("Admin", "AP", "PM")


def _ensure_registered() -> None:
    global _registered
    if _registered:
        return
    s = get_settings()
    oauth.register(
        name="google",
        client_id=s.google_client_id,
        client_secret=s.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    _registered = True


class NotAuthenticated(Exception):
    pass


def upsert_user(db: Session, email: str, name: str | None) -> User:
    email = email.lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        role = "Admin" if email in get_settings().admin_email_list else "PM"
        user = User(email=email, name=name, role=role)
        db.add(user)
        audit(db, email, "user.created", "user", email, role=role)
    user.last_login_at = datetime.now(timezone.utc)
    if name and not user.name:
        user.name = name
    db.commit()
    return user


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    s = get_settings()
    email = request.session.get("user_email")
    if not email and s.dev_auth_email and not s.google_client_id:
        email = s.dev_auth_email.lower()
        upsert_user(db, email, "Dev user")
        request.session["user_email"] = email
    if not email:
        raise NotAuthenticated()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        request.session.clear()
        raise NotAuthenticated()
    return user


def require_role(*roles: str):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, f"This needs the {' or '.join(roles)} role.")
        return user
    return dep


@router.get("/login")
async def login(request: Request):
    s = get_settings()
    if not s.google_client_id:
        raise HTTPException(500, "Google sign-in is not configured (GOOGLE_CLIENT_ID).")
    _ensure_registered()
    redirect_uri = s.app_base_url.rstrip("/") + "/auth/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri, hd=s.allowed_domain, prompt="select_account")


MAILBOX_SCOPES = {
    "sender": "openid email profile https://www.googleapis.com/auth/gmail.modify "
              "https://www.googleapis.com/auth/gmail.send",
    "readonly": "openid email profile https://www.googleapis.com/auth/gmail.readonly",
}


@router.get("/auth/callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    _ensure_registered()
    purpose = request.session.pop("oauth_purpose", None)
    token = await oauth.google.authorize_access_token(request)
    info = token.get("userinfo") or {}
    email = (info.get("email") or "").lower()
    domain = get_settings().allowed_domain
    if not info.get("email_verified") or not email.endswith("@" + domain) or info.get("hd") != domain:
        raise HTTPException(403, f"Sign in with your @{domain} account.")
    if purpose and purpose.startswith("mailbox:"):
        return _finish_mailbox(request, db, purpose.split(":", 1)[1], email, token)
    upsert_user(db, email, info.get("name"))
    request.session["user_email"] = email
    return RedirectResponse(request.session.pop("next", "/"), status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/signed-out", status_code=303)


@router.get("/settings/mailboxes/connect")
async def connect_mailbox(request: Request, role: str, login_hint: str = "", db: Session = Depends(get_db)):
    """Sign in as the mailbox (e.g. contracts@). The app user stays signed in as themselves."""
    user = current_user(request, db)
    if user.role != "Admin":
        raise HTTPException(403, "Only an Admin can connect mailboxes.")
    if role not in MAILBOX_SCOPES:
        raise HTTPException(400, "Unknown mailbox role")
    s = get_settings()
    if not s.google_client_id:
        raise HTTPException(500, "Google sign-in is not configured (GOOGLE_CLIENT_ID).")
    _ensure_registered()
    request.session["oauth_purpose"] = f"mailbox:{role}"
    redirect_uri = s.app_base_url.rstrip("/") + "/auth/callback"
    extra = {"login_hint": login_hint} if login_hint else {}
    return await oauth.google.authorize_redirect(
        request, redirect_uri, scope=MAILBOX_SCOPES[role], access_type="offline",
        prompt="consent select_account", hd=s.allowed_domain, **extra)


def _finish_mailbox(request: Request, db: Session, role: str, email: str, token: dict):
    from urllib.parse import quote

    from app.mail import mailboxes

    user = current_user(request, db)
    if user.role != "Admin":
        raise HTTPException(403, "Only an Admin can connect mailboxes.")
    try:
        mb = mailboxes.connect(db, email, role, token.get("refresh_token"), token.get("scope", ""), user.email)
        db.commit()
        msg = f"Connected {mb.email} as {'the Sender' if mb.role == 'sender' else 'a read-only source'}."
    except ValueError as exc:
        db.rollback()
        msg = str(exc)
    return RedirectResponse("/settings/mailboxes?msg=" + quote(msg), status_code=303)
