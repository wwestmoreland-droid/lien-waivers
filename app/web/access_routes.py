"""Settings → People: invite, change roles, remove and restore access. Admin only."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db.models import Invite, User
from app.db.session import get_db
from app.services import access
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_role("Admin"))])


def render(request: Request, name: str, user: User, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"user": user, **ctx})


def _back(msg: str, link: str = "") -> RedirectResponse:
    url = "/settings/people?msg=" + quote(msg)
    if link:
        url += "&link=" + quote(link)
    return RedirectResponse(url + "#invite", status_code=303)


@router.get("/settings/people", response_class=HTMLResponse)
def people(request: Request, msg: str = "", link: str = "", db: Session = Depends(get_db),
           user: User = Depends(require_role("Admin"))):
    users = db.scalars(select(User).order_by(User.is_active.desc(), User.email)).all()
    invites = [i for i in db.scalars(select(Invite).where(Invite.status == "pending").order_by(Invite.id.desc()))]
    requests = db.scalars(select(Invite).where(Invite.status == "requested").order_by(Invite.id.desc())).all()
    return render(request, "people.html", user, users=users, invites=invites, requests=requests, msg=msg,
                  link=link, roles=access.ROLES, role_info=access.ROLE_INFO, live=access.live_invite,
                  days=access.INVITE_DAYS)


@router.post("/settings/people/invite")
def invite(email: str = Form(...), role: str = Form(...), name: str = Form(""), send: str = Form(""),
           db: Session = Depends(get_db), user: User = Depends(require_role("Admin"))):
    try:
        inv, token = access.create_invite(db, email, role, user.email, name)
    except ValueError as exc:
        db.rollback()
        return _back(str(exc))
    msg = f"Invite for {inv.email} ({inv.role}) is ready. It works once and expires in {access.INVITE_DAYS} days."
    if send:
        ok, why = access.email_invite(db, inv, token, user.email)
        msg = f"Invite emailed to {inv.email} ({inv.role})." if ok else f"{msg} Couldn't email it ({why}); copy the link below."
    db.commit()
    return _back(msg, access.invite_url(token))


@router.post("/settings/people/requests/{iid}/approve")
def approve(iid: int, role: str = Form(...), send: str = Form(""), db: Session = Depends(get_db),
            user: User = Depends(require_role("Admin"))):
    req = db.get(Invite, iid)
    if req is None or req.status != "requested":
        raise HTTPException(404)
    return invite(req.email, role, req.name or "", send, db, user)


@router.post("/settings/people/invites/{iid}/revoke")
def revoke(iid: int, db: Session = Depends(get_db), user: User = Depends(require_role("Admin"))):
    inv = db.get(Invite, iid)
    if inv is None:
        raise HTTPException(404)
    access.revoke_invite(db, inv, user.email)
    db.commit()
    return _back(f"{'Invite withdrawn' if inv.status == 'revoked' else 'Request dismissed'} for {inv.email}.")


@router.post("/settings/people/{uid}/role")
def role(uid: int, role: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_role("Admin"))):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404)
    try:
        access.set_role(db, target, role, user)
    except ValueError as exc:
        db.rollback()
        return _back(str(exc))
    db.commit()
    return _back(f"{target.email} is now {role}.")


@router.post("/settings/people/{uid}/remove")
def remove(uid: int, db: Session = Depends(get_db), user: User = Depends(require_role("Admin"))):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404)
    try:
        access.remove(db, target, user)
    except ValueError as exc:
        db.rollback()
        return _back(str(exc))
    db.commit()
    return _back(f"Access removed for {target.email}. They're signed out on their next click.")


@router.post("/settings/people/{uid}/restore")
def restore(uid: int, db: Session = Depends(get_db), user: User = Depends(require_role("Admin"))):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404)
    access.restore(db, target, user)
    db.commit()
    return _back(f"Access restored for {target.email} ({target.role}).")
