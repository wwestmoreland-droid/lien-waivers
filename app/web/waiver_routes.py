"""Phase 2 pages: Waivers list and detail, template registry and calibration."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import current_user, require_role
from app.db.models import Project, Subcontract, User, Vendor, Waiver
from app.db.session import get_db
from app.services.audit import audit
from app.services.settings_store import get_setting, set_setting
from app.waivers import pdf, service
from app.waivers.types import WaiverStatus, WaiverType
from app.web.templating import templates

router = APIRouter()
EDITORS = ("Admin", "AP")

REJECT_REASONS = [
    "it isn't signed",
    "the notary section is blank",
    "the amount should be {amount}",
    "the date should be {through_date}",
    "it's for the wrong project",
    "it's the wrong waiver form",
]


def _waiver(db: Session, wid: int) -> Waiver:
    w = db.get(Waiver, wid)
    if not w:
        raise HTTPException(404, "Waiver not found")
    return w


def _back(w: Waiver, msg: str = "") -> RedirectResponse:
    return RedirectResponse(f"/waivers/{w.id}" + (f"?msg={msg}" if msg else ""), status_code=303)


@router.get("/waivers", response_class=HTMLResponse)
def waivers_page(request: Request, status: str = "open", project: str = "", type: str = "", q: str = "",
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = (select(Waiver).join(Subcontract).join(Project).join(Vendor, Subcontract.vendor_id == Vendor.id)
             .where(Project.is_tracked.is_(True)).order_by(Waiver.id.desc()))
    if status == "open":
        query = query.where(Waiver.status.in_([s.value for s in service.OPEN_STATUSES]))
    elif status:
        query = query.where(Waiver.status == status)
    if project:
        query = query.where(Project.number == project)
    if type:
        query = query.where(Waiver.type == type)
    if q:
        query = query.where(Vendor.name.ilike(f"%{q}%"))
    rows = db.scalars(query.limit(500)).all()
    counts = dict(db.execute(select(Waiver.status, func.count(Waiver.id)).join(Subcontract).join(Project)
                             .where(Project.is_tracked.is_(True)).group_by(Waiver.status)).all())
    projects = db.scalars(select(Project).where(Project.is_tracked.is_(True)).order_by(Project.number)).all()
    return templates.TemplateResponse(request, "waivers.html", {
        "user": user, "rows": rows, "counts": counts, "projects": projects, "types": list(WaiverType),
        "statuses": list(WaiverStatus), "f": {"status": status, "project": project, "type": type, "q": q},
        "WaiverType": WaiverType})


@router.get("/waivers/{wid}", response_class=HTMLResponse)
def waiver_detail(request: Request, wid: int, msg: str = "", db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    w = _waiver(db, wid)
    others = db.scalars(select(Waiver).where(Waiver.subcontract_id == w.subcontract_id, Waiver.id != w.id)
                        .order_by(Waiver.id)).all()
    reasons = [r.format(amount=f"${w.amount:,.2f}" if w.amount else "", through_date=
                        w.through_date.strftime("%b %-d, %Y") if w.through_date else "") for r in REJECT_REASONS]
    return templates.TemplateResponse(request, "waiver_detail.html", {
        "user": user, "w": w, "WaiverType": WaiverType, "editable": w.status in service.EDITABLE,
        "others": others, "reasons": reasons, "msg": msg,
        "notary_required": (service.registry().templates[w.template_key].notary_required
                            if w.template_key in service.registry().templates else True)})


@router.get("/waivers/{wid}/pdf")
def waiver_pdf(wid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _waiver(db, wid)
    if not w.generated_pdf_path or not Path(w.generated_pdf_path).exists():
        raise HTTPException(404, "No PDF generated yet")
    return FileResponse(w.generated_pdf_path, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{Path(w.generated_pdf_path).name}"'})


@router.get("/waivers/{wid}/signed")
def waiver_signed(wid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    w = _waiver(db, wid)
    if not w.signed_pdf_path or not Path(w.signed_pdf_path).exists():
        raise HTTPException(404, "No signed copy yet")
    media = "application/pdf" if w.signed_pdf_path.lower().endswith(".pdf") else "application/octet-stream"
    return FileResponse(w.signed_pdf_path, media_type=media,
                        headers={"Content-Disposition": f'inline; filename="{Path(w.signed_pdf_path).name}"'})


def _act(db: Session, w: Waiver, fn, *args):
    try:
        fn(db, w, *args)
    except ValueError as exc:
        db.rollback()
        return _back(w, str(exc))
    db.commit()
    return _back(w)


@router.post("/waivers/{wid}/through-date")
def through_date(wid: int, value: date = Form(...), db: Session = Depends(get_db),
                 user: User = Depends(require_role(*EDITORS))):
    return _act(db, _waiver(db, wid), service.set_through_date, value, user.email)


@router.post("/waivers/{wid}/exceptions")
def exceptions(wid: int, value: str = Form(""), db: Session = Depends(get_db),
               user: User = Depends(require_role(*EDITORS))):
    return _act(db, _waiver(db, wid), service.set_exceptions, value, user.email)


@router.post("/waivers/{wid}/regenerate")
def regenerate(wid: int, db: Session = Depends(get_db), user: User = Depends(require_role(*EDITORS))):
    return _act(db, _waiver(db, wid), service.regenerate, user.email)


@router.post("/waivers/{wid}/mark-sent")
def mark_sent(wid: int, db: Session = Depends(get_db), user: User = Depends(require_role(*EDITORS))):
    return _act(db, _waiver(db, wid), service.mark_sent, user.email)


@router.post("/waivers/{wid}/received")
async def received(wid: int, file: UploadFile = File(...), db: Session = Depends(get_db),
                   user: User = Depends(require_role(*EDITORS))):
    w = _waiver(db, wid)
    content = await file.read()
    if not content:
        return _back(w, "The file was empty.")
    return _act(db, w, service.mark_received, user.email, file.filename or "signed.pdf", content)


@router.post("/waivers/{wid}/accept")
def accept(wid: int, note: str = Form(""), db: Session = Depends(get_db),
           user: User = Depends(require_role("Admin", "AP", "PM"))):
    w = _waiver(db, wid)
    if user.role == "PM" and (w.subcontract.project.pm_email or "").lower() != user.email:
        raise HTTPException(403, "PMs can accept waivers on their own jobs only.")
    return _act(db, w, service.accept, user.email, note)


@router.post("/waivers/{wid}/reject")
def reject(wid: int, reason: str = Form(...), db: Session = Depends(get_db),
           user: User = Depends(require_role("Admin", "AP", "PM"))):
    w = _waiver(db, wid)
    if user.role == "PM" and (w.subcontract.project.pm_email or "").lower() != user.email:
        raise HTTPException(403, "PMs can reject waivers on their own jobs only.")
    return _act(db, w, service.reject, user.email, reason)


@router.post("/waivers/{wid}/void")
def void(wid: int, reason: str = Form(...), db: Session = Depends(get_db),
         user: User = Depends(require_role(*EDITORS))):
    return _act(db, _waiver(db, wid), service.void, user.email, reason)


# ---------------------------------------------------------------- templates

@router.get("/settings/templates", response_class=HTMLResponse)
def templates_page(request: Request, msg: str = "", db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    reg = service.registry()
    blocked = db.scalar(select(func.count(Waiver.id)).where(Waiver.status == WaiverStatus.NEEDS_REVIEW.value)) or 0
    return templates.TemplateResponse(request, "templates_registry.html", {
        "user": user, "reg": reg, "blocked": blocked, "msg": msg, "WaiverType": WaiverType,
        "cond_partial": bool(get_setting(db, "conditional_partial_enabled"))})


@router.get("/settings/templates/calibrate/{key:path}")
def calibrate(key: str, user: User = Depends(current_user)):
    t = service.registry().templates.get(key)
    if not t:
        raise HTTPException(404)
    data = pdf.fill(t, pdf.SAMPLE_VALUES, grid=True)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="calibrate-{key.replace("/", "-")}.pdf"'})


@router.post("/settings/templates/recheck")
def recheck(db: Session = Depends(get_db), user: User = Depends(require_role(*EDITORS))):
    service.reload_registry()
    n = service.recheck_blocked(db, user.email)
    db.commit()
    return RedirectResponse(f"/settings/templates?msg=Registry reloaded. {n} blocked waiver(s) now have a form.",
                            status_code=303)


@router.post("/settings/flags")
def save_flags(conditional_partial: str = Form(""), db: Session = Depends(get_db),
               user: User = Depends(require_role("Admin"))):
    value = conditional_partial == "on"
    if value and service.registry().lookup("default", WaiverType.CONDITIONAL_PARTIAL.value) is None:
        return RedirectResponse("/settings/templates?msg=Add a conditional partial template before turning this on.",
                                status_code=303)
    if value != bool(get_setting(db, "conditional_partial_enabled")):
        set_setting(db, "conditional_partial_enabled", value, user.email)
        audit(db, user.email, "flag.conditional_partial", "setting", "conditional_partial_enabled", value=value)
    db.commit()
    return RedirectResponse("/settings/templates", status_code=303)
