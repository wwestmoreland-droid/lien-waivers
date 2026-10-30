"""Today (the home page) and More (links to everything else)."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_user, require_role
from app.db.models import Project, ReviewItem, User, Vendor
from app.db.session import get_db
from app.mail import outbound, unsigned
from app.services import today as svc
from app.services.audit import audit
from app.web.templating import templates
from app.web.waiver_routes import REJECT_REASONS

router = APIRouter()
EDITORS = ("Admin", "AP")


def _reasons(w) -> list[str]:
    return [r.format(amount=f"${w.amount:,.2f}" if w.amount is not None else "",
                     through_date=w.through_date.strftime("%b %-d, %Y") if w.through_date else "")
            for r in REJECT_REASONS]


@router.get("/", response_class=HTMLResponse)
def today(request: Request, msg: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    from app.waivers import service as waivers

    editor = user.role in EDITORS
    to_send = svc.ready_to_send(db) if editor else []
    signed = svc.signed_back(db)
    questions = svc.quick_questions(db, user)
    waiting = svc.waiting_on_subs(db)
    lists = svc.waiting_lists(db)
    notary = {}
    for w in signed:
        t = waivers.registry().templates.get(w.template_key or "")
        notary[w.id] = t.notary_required if t else True
    need = len(to_send) + len([w for w in signed if svc.can_review(user, w)]) + len(questions)
    first = (user.name or user.email).split()[0].split("@")[0].split(".")[0].capitalize()
    return templates.TemplateResponse(request, "today.html", {
        "user": user, "msg": msg, "first": first, "need": need, "editor": editor,
        "to_send": to_send, "signed": signed, "questions": questions, "waiting": waiting, "lists": lists,
        "list_next": lambda sc: svc.list_next_step(db, sc),
        "last_word": lambda w: unsigned.last_word(db, w),
        "can_review": lambda w: svc.can_review(user, w), "check": lambda w: svc.check_words(w, notary[w.id]),
        "reasons": _reasons, "next_step": lambda w: svc.next_step(db, w), "today": svc.local_today(),
        "accepted_week": svc.accepted_this_week(db),
        "vendors": db.scalars(select(Vendor).order_by(Vendor.name)).all() if questions else []})


@router.post("/today/send-all")
def send_all(db: Session = Depends(get_db), user: User = Depends(require_role(*EDITORS))):
    """Send every item in Ready to send that can go out; the same send path as the Outbox."""
    sent = failed = 0
    for item in svc.ready_to_send(db):
        if not item.sendable:
            continue
        try:
            ok = outbound.send(db, item.email, user.email)
        except ValueError:
            ok = False
        sent += ok
        failed += not ok
        db.commit()
    msg = f"Sent {sent}." + (f" {failed} couldn't be sent; the reason is shown on each." if failed else "")
    return RedirectResponse("/?msg=" + quote(msg), status_code=303)


@router.post("/today/delete-drafts")
async def delete_drafts(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role(*EDITORS))):
    """Delete the selected unsent emails (Today's Ready to send, or the Outbox)."""
    from app.web.nav import safe_back, with_msg

    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    out = outbound.delete_drafts(db, ids, user.email)
    db.commit()
    if not out["deleted"]:
        msg = "Pick at least one email to delete."
    else:
        msg = f"Deleted {out['deleted']} draft{'s' if out['deleted'] != 1 else ''}."
        if out["waivers_cancelled"]:
            msg += (f" {out['waivers_cancelled']} waiver{'s were' if out['waivers_cancelled'] != 1 else ' was'} cancelled "
                    "with them (Restore on the waiver brings it back).")
    return RedirectResponse(with_msg(safe_back(form.get("back"), "/"), msg), status_code=303)


@router.post("/queues/review/{rid}/to-pm")
def review_to_pm(rid: int, db: Session = Depends(get_db), user: User = Depends(require_role(*EDITORS))):
    """'No, send to PM': the question goes to the job's PM (it stays open and on their Today)."""
    item = db.get(ReviewItem, rid)
    if not item:
        raise HTTPException(404)
    project = db.scalar(select(Project).where(Project.number == item.project_number))
    if not project or not project.pm_email:
        return RedirectResponse("/?msg=" + quote("This job has no PM email in INTAKE, so it stays with you."),
                                status_code=303)
    item.assignee_email = project.pm_email.lower()
    audit(db, user.email, "review.sent_to_pm", "review_item", item.id, kind=item.kind, pm=project.pm_email)
    db.commit()
    return RedirectResponse("/?msg=" + quote(f"Sent to {project.pm_name or project.pm_email}."), status_code=303)


@router.post("/queues/review/{rid}/use-forward")
def use_forward(rid: int, db: Session = Depends(get_db), user: User = Depends(require_role(*EDITORS))):
    """'Yes' on "X sent the waiver on to billing@...": waivers for that sub go there from now on."""
    import re

    from app.contacts import service as contacts
    from app.db.models import Contact, EmailMessage

    item = db.get(ReviewItem, rid)
    if not item or item.kind != "waiver_forwarded":
        raise HTTPException(404)
    e = db.get(EmailMessage, item.ref_id)
    m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", item.message)
    if e is None or e.vendor is None or not m:
        return RedirectResponse("/?msg=" + quote("Couldn't tell which sub or address this is; open the email."),
                                status_code=303)
    addr, v = m.group(0).rstrip(".").lower(), e.vendor
    c = db.scalar(select(Contact).where(Contact.email == addr))
    if c is None:
        c = Contact(email=addr, source="email", message_count=0)
        db.add(c)
        db.flush()
    contacts.confirm(db, c, user.email, v.id, "billing")
    contacts.set_waiver_email(db, v, addr, user.email)
    item.status, item.resolved_by = "resolved", user.email
    db.commit()
    return RedirectResponse("/?msg=" + quote(f"{v.name}'s waivers will go to {addr} from now on."), status_code=303)


@router.get("/more", response_class=HTMLResponse)
def more(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(request, "more.html", {"user": user})
