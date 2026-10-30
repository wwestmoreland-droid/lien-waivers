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
from app.mail import outbound
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
    notary = {}
    for w in signed:
        t = waivers.registry().templates.get(w.template_key or "")
        notary[w.id] = t.notary_required if t else True
    need = len(to_send) + len([w for w in signed if svc.can_review(user, w)]) + len(questions)
    first = (user.name or user.email).split()[0].split("@")[0].split(".")[0].capitalize()
    return templates.TemplateResponse(request, "today.html", {
        "user": user, "msg": msg, "first": first, "need": need, "editor": editor,
        "to_send": to_send, "signed": signed, "questions": questions, "waiting": waiting,
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


@router.get("/more", response_class=HTMLResponse)
def more(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(request, "more.html", {"user": user})
