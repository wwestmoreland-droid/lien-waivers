"""Suppliers under each sub: add, edit, remove, request the list, and answer list suggestions."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db.models import Subcontract, Supplier, SupplierCandidate, User
from app.db.session import get_db
from app.suppliers import service
from app.web.nav import safe_back, with_msg

router = APIRouter()
editor = require_role("Admin", "AP")


def _go(back: str, sc: Subcontract, msg: str) -> RedirectResponse:
    return RedirectResponse(with_msg(safe_back(back, f"/jobs/{sc.project.number}"), msg), status_code=303)


def _sub(db: Session, sc_id: int) -> Subcontract:
    sc = db.get(Subcontract, sc_id)
    if not sc:
        raise HTTPException(404)
    return sc


@router.post("/subs/{sc_id}/suppliers")
def add_supplier(sc_id: int, name: str = Form(...), supplies: str = Form(""), amount: str = Form(""),
                 contact_name: str = Form(""), email: str = Form(""), phone: str = Form(""), back: str = Form(""),
                 db: Session = Depends(get_db), user: User = Depends(editor)):
    sc = _sub(db, sc_id)
    try:
        s = service.add(db, sc, user.email, name, supplies, amount, contact_name, email, phone)
    except ValueError as exc:
        return _go(back, sc, str(exc))
    db.commit()
    return _go(back, sc, f"Added {s.name} as a supplier to {sc.vendor.name}.")


@router.post("/suppliers/{sid}")
def edit_supplier(sid: int, name: str = Form(None), supplies: str = Form(None), amount: str = Form(None),
                  contact_name: str = Form(None), email: str = Form(None), phone: str = Form(None),
                  back: str = Form(""), db: Session = Depends(get_db), user: User = Depends(editor)):
    s = db.get(Supplier, sid)
    if not s:
        raise HTTPException(404)
    service.edit(db, s, user.email, name=name, supplies=supplies, amount=amount, contact_name=contact_name,
                 email=email, phone=phone)
    db.commit()
    return _go(back, s.subcontract, f"Saved {s.name}.")


@router.post("/suppliers/{sid}/remove")
def remove_supplier(sid: int, back: str = Form(""), db: Session = Depends(get_db), user: User = Depends(editor)):
    s = db.get(Supplier, sid)
    if not s:
        raise HTTPException(404)
    service.remove(db, s, user.email)
    db.commit()
    return _go(back, s.subcontract, f"Removed {s.name}.")


@router.post("/subs/{sc_id}/supplier-list/request")
def request_list(sc_id: int, back: str = Form(""), db: Session = Depends(get_db), user: User = Depends(editor)):
    sc = _sub(db, sc_id)
    service.request_list(db, sc, user.email)
    db.commit()
    return _go(back, sc, f"The supplier list request to {sc.vendor.name} is under Ready to send on Today.")


@router.post("/subs/{sc_id}/supplier-list/none")
def no_suppliers(sc_id: int, back: str = Form(""), db: Session = Depends(get_db), user: User = Depends(editor)):
    sc = _sub(db, sc_id)
    service.mark_no_suppliers(db, sc, user.email)
    db.commit()
    return _go(back, sc, f"Noted: {sc.vendor.name} has no suppliers over the minimum.")


@router.post("/supplier-candidates/{cid}/{answer}")
def answer_candidate(cid: int, answer: str, back: str = Form(""), db: Session = Depends(get_db),
                     user: User = Depends(editor)):
    c = db.get(SupplierCandidate, cid)
    if not c or answer not in ("yes", "no") or c.status != "pending":
        raise HTTPException(404)
    service.decide(db, c, answer == "yes", user.email)
    db.commit()
    msg = f"Added {c.name}." if answer == "yes" else f"Left {c.name} off."
    return _go(back, c.subcontract, msg)


@router.post("/subs/{sc_id}/track-suppliers")
def sub_supplier_tracking(sc_id: int, on: str = Form(""), back: str = Form(""), db: Session = Depends(get_db),
                          user: User = Depends(editor)):
    """The 'Track suppliers' checkbox under each sub."""
    sc = _sub(db, sc_id)
    try:
        dropped = service.set_tracking(db, sc, on == "1", user.email)
    except ValueError as exc:
        return _go(back, sc, str(exc))
    db.commit()
    if on == "1":
        msg = f"Tracking suppliers for {sc.vendor.name} again."
    else:
        msg = f"Suppliers are off for {sc.vendor.name} on {sc.project.number}." + (
            f" {dropped} unsent supplier-list email{'s' if dropped != 1 else ''} thrown away." if dropped else "")
    return _go(back, sc, msg)
