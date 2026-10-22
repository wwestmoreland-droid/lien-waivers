"""Phase 4 pages: Contacts, New contacts queue actions, contact backfill, waiver email choice."""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.contacts import backfill, service
from app.contacts.signature import ROLE_NAMES
from app.db.models import Contact, ContactBackfill, ContactMessage, Mailbox, User, Vendor
from app.db.session import get_db
from app.web.templating import templates

EDITORS = ("Admin", "AP")
# Contacts come from the ap@ and contracts@ mailboxes, so the same people who see email see these.
router = APIRouter(dependencies=[Depends(require_role(*EDITORS))])
editor = require_role(*EDITORS)


def render(request: Request, name: str, user: User, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"user": user, "ROLE_NAMES": ROLE_NAMES, **ctx})


def _contact(db: Session, cid: int) -> Contact:
    c = db.get(Contact, cid)
    if not c:
        raise HTTPException(404, "Contact not found")
    return c


def _filtered(db: Session, q: str, role: str, vendor_id: str, status: str):
    stmt = select(Contact).outerjoin(Vendor, Contact.vendor_id == Vendor.id)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Contact.email).like(like), func.lower(Contact.name).like(like),
                              func.lower(Contact.company).like(like), func.lower(Vendor.name).like(like),
                              func.lower(Contact.title).like(like)))
    if role:
        stmt = stmt.where(Contact.role == role)
    if vendor_id == "none":
        stmt = stmt.where(Contact.vendor_id.is_(None))
    elif vendor_id:
        stmt = stmt.where(Contact.vendor_id == int(vendor_id))
    if status == "review":
        stmt = stmt.where(or_(Contact.status == "new", Contact.needs_review.is_(True)), Contact.is_active.is_(True))
    elif status:
        stmt = stmt.where(Contact.status == status)
    else:
        stmt = stmt.where(Contact.status != "ignored")
    return stmt.order_by(Vendor.name.is_(None), Vendor.name, Contact.role != "billing", Contact.name, Contact.email)


@router.get("/contacts", response_class=HTMLResponse)
def contacts_page(request: Request, q: str = "", role: str = "", vendor: str = "", status: str = "",
                  db: Session = Depends(get_db), user: User = Depends(editor)):
    rows = db.scalars(_filtered(db, q, role, vendor, status).limit(1000)).all()
    vendors = db.scalars(select(Vendor).order_by(Vendor.name)).all()
    bfs = db.scalars(select(ContactBackfill)).all()
    return render(request, "contacts.html", user, rows=rows, vendors=vendors, q=q, role=role, vendor=vendor,
                  status=status, backfills=bfs, progress=backfill.progress)


@router.get("/contacts.csv")
def contacts_csv(q: str = "", role: str = "", vendor: str = "", status: str = "", db: Session = Depends(get_db)):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Vendor", "Name", "Email", "Role", "Title", "Company", "Phone", "Mobile", "Status", "Source",
                "First seen", "Last seen", "Messages"])
    for c in db.scalars(_filtered(db, q, role, vendor, status)):
        w.writerow([c.vendor.name if c.vendor else "", c.name or "", c.email, ROLE_NAMES.get(c.role or "", ""),
                    c.title or "", c.company or "", c.phone or "", c.mobile or "", c.status, c.source,
                    c.first_seen_at.date().isoformat() if c.first_seen_at else "",
                    c.last_seen_at.date().isoformat() if c.last_seen_at else "", c.message_count])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="contacts.csv"'})


@router.get("/contacts/{cid}", response_class=HTMLResponse)
def contact_detail(request: Request, cid: int, db: Session = Depends(get_db), user: User = Depends(editor)):
    c = _contact(db, cid)
    messages = db.scalars(select(ContactMessage).where(ContactMessage.contact_id == c.id)
                          .order_by(ContactMessage.sent_at.desc().nulls_last()).limit(200)).all()
    vendors = db.scalars(select(Vendor).order_by(Vendor.name)).all()
    return render(request, "contact_detail.html", user, c=c, messages=messages, vendors=vendors)


@router.post("/contacts/{cid}")
def contact_save(cid: int, action: str = Form("save"), vendor_id: str = Form(""), role: str = Form(""),
                 name: str = Form(None), title: str = Form(None), company: str = Form(None),
                 phone: str = Form(None), mobile: str = Form(None), back: str = Form(""),
                 db: Session = Depends(get_db), user: User = Depends(editor)):
    c = _contact(db, cid)
    for field, value in (("name", name), ("title", title), ("company", company), ("phone", phone),
                         ("mobile", mobile)):
        if value is not None:
            setattr(c, field, value.strip() or None)
    vid = int(vendor_id) if vendor_id.isdigit() else None
    if action == "ignore":
        service.ignore(db, c, user.email)
    elif action in ("confirm", "save"):
        if action == "confirm" or c.status == "confirmed":
            service.confirm(db, c, user.email, vid, role or None)
        else:
            c.vendor_id, c.role = vid, role or c.role
    db.commit()
    target = back if back.startswith("/") and not back.startswith("//") else f"/contacts/{c.id}"
    return RedirectResponse(target, status_code=303)


def _back(back: str) -> str:
    return back if back.startswith("/") and not back.startswith("//") else "/"


@router.post("/vendors/{vid}/waiver-email")
def vendor_waiver_email(vid: int, email: str = Form(""), contact_id: str = Form(""), back: str = Form("/"),
                        db: Session = Depends(get_db), user: User = Depends(editor)):
    v = db.get(Vendor, vid)
    if not v:
        raise HTTPException(404)
    if contact_id.isdigit():
        # "Use for waivers" on a contact found in email: that also confirms them as this vendor's billing contact.
        c = _contact(db, int(contact_id))
        service.confirm(db, c, user.email, v.id, "billing")
        email = c.email
    service.set_waiver_email(db, v, email, user.email)
    db.commit()
    return RedirectResponse(_back(back), status_code=303)


@router.post("/contacts/{cid}/assign")
def contact_assign(cid: int, vendor_id: int = Form(...), role: str = Form(""), back: str = Form("/"),
                   db: Session = Depends(get_db), user: User = Depends(editor)):
    c = _contact(db, cid)
    service.confirm(db, c, user.email, vendor_id, role or None)
    db.commit()
    return RedirectResponse(_back(back), status_code=303)


@router.post("/vendors/{vid}/lookup")
def vendor_lookup(vid: int, back: str = Form("/"), db: Session = Depends(get_db), user: User = Depends(editor)):
    from urllib.parse import quote

    from app.contacts.lookup import lookup_vendor

    v = db.get(Vendor, vid)
    if not v:
        raise HTTPException(404)
    found, problems = lookup_vendor(db, v)
    db.commit()
    mine = [c for c in (db.get(Contact, i) for i in found) if c and c.vendor_id == v.id]
    msg = (f"Searched email for {v.name}: {len(found)} contact{'s' if len(found) != 1 else ''} on matching messages, "
           f"{len(mine)} matched to {v.name}.")
    if problems:
        msg += " " + " ".join(problems)
    target = _back(back).split("#")[0]
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}found={','.join(map(str, found))}&msg={quote(msg)}#contacts",
                            status_code=303)


@router.post("/settings/mailboxes/{mid}/contacts-backfill")
def backfill_action(mid: int, action: str = Form(...), db: Session = Depends(get_db), user: User = Depends(editor)):
    mb = db.get(Mailbox, mid)
    if not mb:
        raise HTTPException(404)
    bf = db.scalar(select(ContactBackfill).where(ContactBackfill.mailbox_id == mb.id))
    if action in ("start_tonight", "start_now"):
        backfill.start(db, mb, user.email, overnight_only=action == "start_tonight")
    elif bf is not None:
        backfill.set_status(db, bf, action, user.email)
    db.commit()
    return RedirectResponse("/settings/mailboxes#backfill-" + str(mb.id), status_code=303)
