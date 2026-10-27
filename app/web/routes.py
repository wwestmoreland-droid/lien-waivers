"""Phase 1 pages: Dashboard, Projects, Jobs, Queues, Settings, sync log."""
from __future__ import annotations

import threading
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import ROLES, current_user, require_role
from app.ctc import report
from app.ctc.engine import compute_for_subcontract, order_key
from app.db.models import (
    AuditLog,
    ChangeOrder,
    Contact,
    CtcSnapshot,
    DirectPurchase,
    Payable,
    PayableEvent,
    Project,
    ReviewItem,
    Subcontract,
    SyncRun,
    User,
    Vendor,
    VendorAlias,
    Waiver,
)
from app.db.session import get_db
from app.jobs import scheduler
from app.matching import resolver
from app.services import projects as project_svc
from app.services.audit import audit
from app.services.locks import JobAlreadyRunning
from app.services.settings_store import get_setting, set_setting
from app.services.sync_runner import sync_now
from app.waivers import service as waiver_service
from app.web.templating import templates

router = APIRouter()
EDITORS = ("Admin", "AP")

UNMATCHED_STATUSES = ("unmatched_vendor", "no_subcontract", "ambiguous")


def render(request: Request, name: str, user: User, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"user": user, **ctx})


def _last_sync(db: Session) -> SyncRun | None:
    return db.scalar(select(SyncRun).order_by(SyncRun.id.desc()).limit(1))


def queue_counts(db: Session) -> dict[str, int]:
    tracked = select(Project.number).where(Project.is_tracked.is_(True))
    return {
        "unmatched_ap": db.scalar(select(func.count(Payable.id)).where(
            Payable.match_status.in_(UNMATCHED_STATUSES), Payable.deleted_in_source.is_(False),
            Payable.project_number.in_(tracked))) or 0,
        "unmatched_co": db.scalar(select(func.count(ChangeOrder.id)).where(
            ChangeOrder.match_status.in_(UNMATCHED_STATUSES), ChangeOrder.deleted_in_source.is_(False),
            ChangeOrder.project_number.in_(tracked))) or 0,
        "review": db.scalar(select(func.count(ReviewItem.id)).where(
            ReviewItem.status == "open", ReviewItem.project_number.in_(tracked))) or 0,
        "new_contacts": db.scalar(select(func.count(Contact.id)).where(
            or_(Contact.status == "new", Contact.needs_review.is_(True)), Contact.is_active.is_(True))) or 0,
        "verify": db.scalar(select(func.count(Waiver.id)).join(Subcontract).join(Project).where(
            Project.is_tracked.is_(True), Waiver.status == "received")) or 0,
    }


# ---------------------------------------------------------------- dashboard

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = report.report_rows(db)
    totals = {f: sum((getattr(r, f) for r in rows), report.ZERO) for f in report.MONEY_FIELDS}
    status_counts: dict[str, int] = {}
    for r in rows:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1
    tracked = db.scalars(select(Project).where(Project.is_tracked.is_(True)).order_by(Project.number)).all()
    by_job = report.grouped(rows, "job")
    new_projects = [p for p in project_svc.picker_projects(db) if not p.is_tracked and not p.ever_tracked]
    waiver_counts = dict(db.execute(select(Waiver.status, func.count(Waiver.id)).join(Subcontract).join(Project)
                                    .where(Project.is_tracked.is_(True)).group_by(Waiver.status)).all())
    return render(request, "dashboard.html", user, waiver_counts=waiver_counts, totals=totals, status_counts=status_counts, tracked=tracked,
                  by_job=by_job, counts=queue_counts(db), last_sync=_last_sync(db),
                  next_sync=scheduler.next_run_time(), new_projects=new_projects,
                  over_billed=[r for r in rows if r.status == "over_billed"])


# ---------------------------------------------------------------- projects

@router.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request, q: str = "", state: str = "", client: str = "", pm: str = "",
                  db: Session = Depends(get_db), user: User = Depends(current_user)):
    all_rows = project_svc.picker_projects(db)
    rows = project_svc.picker_projects(db, q or None, state or None, client or None, pm or None)
    return render(request, "projects.html", user, projects=rows, opts=project_svc.filter_options(all_rows),
                  f={"q": q, "state": state, "client": client, "pm": pm}, label=project_svc.picker_label,
                  last_sync=_last_sync(db))


def _project(db: Session, number: str) -> Project:
    p = db.scalar(select(Project).where(Project.number == number))
    if not p:
        raise HTTPException(404, "Project not found")
    return p


@router.post("/projects/{number}/track", response_class=HTMLResponse)
def track_project(request: Request, number: str, db: Session = Depends(get_db),
                  user: User = Depends(require_role(*EDITORS))):
    p = _project(db, number)
    project_svc.set_tracked(db, p, True, user.email)
    db.commit()
    _start_background_sync("backfill", {p.number})
    return render(request, "partials/project_row.html", user, p=p, label=project_svc.picker_label,
                  flash="Tracking. History is loading now (no emails are sent for past activity).")


@router.post("/projects/{number}/untrack", response_class=HTMLResponse)
def untrack_project(request: Request, number: str, confirm: str = Form(""), db: Session = Depends(get_db),
                    user: User = Depends(require_role(*EDITORS))):
    p = _project(db, number)
    open_items = project_svc.open_items_count(db, p)
    if open_items and confirm != "yes":
        return render(request, "partials/project_row.html", user, p=p, label=project_svc.picker_label,
                      confirm_untrack=open_items)
    project_svc.set_tracked(db, p, False, user.email)
    db.commit()
    return render(request, "partials/project_row.html", user, p=p, label=project_svc.picker_label,
                  flash="Untracked. Automation is paused; history is kept.")


# ---------------------------------------------------------------- jobs / CTC

@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, by: str = "job", db: Session = Depends(get_db), user: User = Depends(current_user)):
    if by not in ("job", "sub", "pm"):
        by = "job"
    rows = report.report_rows(db)
    return render(request, "jobs.html", user, groups=report.grouped(rows, by), by=by, rows=rows)


@router.get("/jobs/{number}", response_class=HTMLResponse)
def job_detail(request: Request, number: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = _project(db, number)
    rows = report.report_rows(db, number)
    unmatched = db.scalars(select(Payable).where(Payable.project_number == number,
                                                 Payable.match_status.in_(UNMATCHED_STATUSES),
                                                 Payable.deleted_in_source.is_(False))).all()
    direct = db.scalars(select(Payable).where(Payable.project_number == number,
                                              Payable.match_status == "direct_purchase",
                                              Payable.deleted_in_source.is_(False))).all()
    return render(request, "job_detail.html", user, p=p, rows=rows, unmatched=unmatched, direct=direct,
                  waiver_rows=waiver_service.job_waiver_rows(db, p),
                  totals={f: sum((getattr(r, f) for r in rows), report.ZERO) for f in report.MONEY_FIELDS})


@router.get("/jobs/{number}/signed-waivers.pdf")
def job_signed_waivers(number: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = _project(db, number)
    waivers = [r["waiver"] for r in waiver_service.job_waiver_rows(db, p) if r["waiver"]]
    if not waivers:
        raise HTTPException(404, "No signed waivers on this job yet.")
    data = waiver_service.combined_signed_pdf(db, waivers)
    name = f"Signed lien waivers {p.number}.pdf"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/subs/{sc_id}", response_class=HTMLResponse)
def sub_detail(request: Request, sc_id: int, found: str = "", msg: str = "", db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    sc = db.scalar(select(Subcontract).where(Subcontract.id == sc_id).options(
        selectinload(Subcontract.payables), selectinload(Subcontract.change_orders)))
    if not sc:
        raise HTTPException(404)
    ctc = compute_for_subcontract(sc)
    pays = sorted((p for p in sc.payables if not p.deleted_in_source), key=order_key)
    # CTC after each payable, so the history shows the point-in-time view the waiver logic uses.
    timeline = [(p, compute_for_subcontract(sc, as_of=p)) for p in pays]
    events = {}
    if pays:
        for e in db.scalars(select(PayableEvent).where(PayableEvent.payable_id.in_([p.id for p in pays]))):
            events.setdefault(e.payable_id, []).append(e)
    snaps = db.scalars(select(CtcSnapshot).where(CtcSnapshot.subcontract_id == sc.id)
                       .order_by(CtcSnapshot.snapshot_date)).all()
    reviews = db.scalars(select(ReviewItem).where(ReviewItem.ref_type == "subcontract",
                                                  ReviewItem.ref_id == sc.id)).all()
    from app.contacts import service as contacts
    from app.contacts import writeback
    from app.contacts.signature import ROLE_NAMES
    return render(request, "sub_detail.html", user, sc=sc, ctc=ctc, timeline=timeline, events=events,
                  waiver_to=contacts.waiver_email(sc), ROLE_NAMES=ROLE_NAMES,
                  suggestion=contacts.billing_suggestion(db, sc),
                  sheet_mismatch=writeback.billing_mismatch(db, sc),
                  sheet_writes=db.scalars(select(AuditLog).where(
                      AuditLog.action.in_(("smartsheet.cells_filled", "smartsheet.billing_replaced")),
                      AuditLog.entity_type == "subcontract", AuditLog.entity_id == str(sc.id))
                      .order_by(AuditLog.id.desc()).limit(10)).all(),
                  vendor_contacts=contacts.vendor_contacts(db, sc.vendor_id, include_new=True),
                  found_elsewhere=[c for c in (db.get(Contact, int(i)) for i in found.split(",") if i.isdigit())
                                   if c and c.vendor_id != sc.vendor_id and c.status != "ignored"][:30],
                  msg=msg,
                  cos=sorted(sc.change_orders, key=lambda c: c.source_row_id),
                  snaps=snaps, reviews=reviews)


@router.post("/payables/{pid}/retention")
def toggle_retention(pid: int, value: str = Form(...), db: Session = Depends(get_db),
                     user: User = Depends(require_role(*EDITORS))):
    p = db.get(Payable, pid)
    if not p:
        raise HTTPException(404)
    old = p.is_retention_invoice
    p.is_retention_override = {"yes": True, "no": False}.get(value)
    audit(db, user.email, "payable.retention_flag", "payable", p.source_row_id, old=old, new=p.is_retention_invoice)
    if old != p.is_retention_invoice:
        waiver_service.reprocess_payable(db, p, user.email)
    db.commit()
    return RedirectResponse(f"/subs/{p.subcontract_id}" if p.subcontract_id else "/queues", status_code=303)


@router.get("/reports/ctc.csv")
def ctc_csv(project: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    body = report.to_csv(report.report_rows(db, project or None))
    name = f"ctc-{project or 'all'}-{date.today().isoformat()}.csv"
    return Response(body, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/reports/ctc.xlsx")
def ctc_xlsx(project: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    body = report.to_xlsx(report.report_rows(db, project or None))
    name = f"ctc-{project or 'all'}-{date.today().isoformat()}.xlsx"
    return Response(body, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- queues

@router.get("/queues", response_class=HTMLResponse)
def queues(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    tracked = select(Project.number).where(Project.is_tracked.is_(True))
    unmatched = db.scalars(select(Payable).where(
        Payable.match_status.in_(UNMATCHED_STATUSES), Payable.deleted_in_source.is_(False),
        Payable.project_number.in_(tracked)).order_by(Payable.project_number, Payable.vendor_parsed)).all()
    unmatched_cos = db.scalars(select(ChangeOrder).where(
        ChangeOrder.match_status.in_(UNMATCHED_STATUSES), ChangeOrder.deleted_in_source.is_(False),
        ChangeOrder.project_number.in_(tracked))).all()
    reviews = db.scalars(select(ReviewItem).where(ReviewItem.status == "open", ReviewItem.project_number.in_(tracked))
                         .order_by(ReviewItem.kind, ReviewItem.project_number)).all()
    vendors = db.scalars(select(Vendor).order_by(Vendor.name)).all()
    subs_by_project: dict[str, list[Subcontract]] = {}
    for sc in db.scalars(select(Subcontract).join(Project).where(Project.is_tracked.is_(True),
                                                                Subcontract.deleted_in_source.is_(False))):
        subs_by_project.setdefault(sc.project.number, []).append(sc)
    projects = {p.number: p for p in db.scalars(select(Project).where(Project.is_tracked.is_(True)))}
    to_verify = db.scalars(select(Waiver).join(Subcontract).join(Project).where(
        Project.is_tracked.is_(True), Waiver.status == "received").order_by(Waiver.received_at)).all()
    from app.db.models import EmailMessage
    unmatched_mail = db.scalars(select(EmailMessage).where(
        EmailMessage.direction == "in", EmailMessage.waiver_id.is_(None), EmailMessage.kind == "inbound")
        .order_by(EmailMessage.id.desc()).limit(100)).all()
    new_contacts = []
    if user.role in EDITORS:
        new_contacts = db.scalars(select(Contact).where(
            or_(Contact.status == "new", Contact.needs_review.is_(True)), Contact.is_active.is_(True))
            .order_by(Contact.vendor_id.is_(None), Contact.last_seen_at.desc().nulls_last()).limit(200)).all()
    pids = [r.ref_id for r in reviews if r.ref_type == "payable"]
    payable_subs = {p.id: p.subcontract_id for p in db.scalars(select(Payable).where(Payable.id.in_(pids)))} if pids else {}
    from app.contacts.signature import ROLE_NAMES
    return render(request, "queues.html", user, to_verify=to_verify, unmatched_mail=unmatched_mail,
                  new_contacts=new_contacts, ROLE_NAMES=ROLE_NAMES,
                  payable_subs=payable_subs, unmatched=unmatched, unmatched_cos=unmatched_cos, reviews=reviews,
                  vendors=vendors, subs_by_project=subs_by_project, projects=projects, counts=queue_counts(db))


def _reresolve_all(db: Session) -> None:
    tracked = select(Project.number).where(Project.is_tracked.is_(True))
    for p in db.scalars(select(Payable).where(Payable.match_status != "matched", Payable.project_number.in_(tracked))):
        resolver.resolve_payable(db, p)
    for co in db.scalars(select(ChangeOrder).where(ChangeOrder.match_status.in_(UNMATCHED_STATUSES),
                                                   ChangeOrder.project_number.in_(tracked))):
        resolver.resolve_change_order(db, co)


@router.post("/queues/payables/{pid}/map-vendor")
def map_vendor(pid: int, vendor_id: int = Form(...), db: Session = Depends(get_db),
               user: User = Depends(require_role(*EDITORS))):
    p = db.get(Payable, pid)
    vendor = db.get(Vendor, vendor_id)
    if not p or not vendor:
        raise HTTPException(404)
    resolver.add_alias(db, vendor, p.vendor_parsed or "", actor=user.email)
    audit(db, user.email, "vendor.alias_added", "vendor", vendor.id, alias=p.vendor_parsed, from_payable=p.source_row_id)
    _reresolve_all(db)
    db.commit()
    return RedirectResponse("/queues#ap", status_code=303)


@router.post("/queues/payables/{pid}/choose-subcontract")
def choose_subcontract(pid: int, subcontract_id: int = Form(...), db: Session = Depends(get_db),
                       user: User = Depends(require_role(*EDITORS))):
    p = db.get(Payable, pid)
    sc = db.get(Subcontract, subcontract_id)
    if not p or not sc or sc.project.number != p.project_number:
        raise HTTPException(400, "Pick a subcontract on the same job.")
    p.manual_subcontract_id = sc.id
    resolver.resolve_payable(db, p)
    if p.vendor_parsed and resolver.find_vendor(db, p.vendor_parsed) is None:
        resolver.add_alias(db, sc.vendor, p.vendor_parsed, actor=user.email)
    audit(db, user.email, "payable.subcontract_assigned", "payable", p.source_row_id, subcontract_id=sc.id)
    db.commit()
    return RedirectResponse("/queues#ap", status_code=303)


@router.post("/queues/payables/{pid}/direct-purchase")
def direct_purchase(pid: int, scope: str = Form("job"), note: str = Form(""), db: Session = Depends(get_db),
                    user: User = Depends(require_role(*EDITORS))):
    p = db.get(Payable, pid)
    if not p:
        raise HTTPException(404)
    vendor = resolver.get_or_create_vendor(db, p.vendor_parsed or p.vendor_raw or "Unknown vendor")
    if vendor.type == "sub":
        vendor.type = "supplier"
    project_number = p.project_number if scope == "job" else None
    if not resolver.is_direct_purchase(db, vendor.id, project_number):
        db.add(DirectPurchase(vendor_id=vendor.id, project_number=project_number, note=note or None,
                              created_by=user.email))
    audit(db, user.email, "vendor.direct_purchase", "vendor", vendor.id, project=project_number or "all jobs",
          note=note)
    db.flush()
    _reresolve_all(db)
    db.commit()
    return RedirectResponse("/queues#ap", status_code=303)


@router.post("/queues/change-orders/{cid}/map-vendor")
def map_co_vendor(cid: int, vendor_id: int = Form(...), db: Session = Depends(get_db),
                  user: User = Depends(require_role(*EDITORS))):
    co = db.get(ChangeOrder, cid)
    vendor = db.get(Vendor, vendor_id)
    if not co or not vendor:
        raise HTTPException(404)
    resolver.add_alias(db, vendor, co.vendor_parsed or "", actor=user.email)
    audit(db, user.email, "vendor.alias_added", "vendor", vendor.id, alias=co.vendor_parsed, from_co=co.source_row_id)
    _reresolve_all(db)
    db.commit()
    return RedirectResponse("/queues#co", status_code=303)


@router.post("/queues/review/{rid}/{action}")
def review_action(rid: int, action: str, note: str = Form(""), db: Session = Depends(get_db),
                  user: User = Depends(require_role(*EDITORS))):
    item = db.get(ReviewItem, rid)
    if not item or action not in ("resolve", "dismiss"):
        raise HTTPException(404)
    item.status = "resolved" if action == "resolve" else "dismissed"
    item.resolved_by = user.email
    item.resolved_at = datetime.now(timezone.utc)
    item.note = note or None
    audit(db, user.email, f"review.{action}", "review_item", item.id, kind=item.kind, note=note)
    db.commit()
    return RedirectResponse("/queues#review", status_code=303)


# ---------------------------------------------------------------- sync

_sync_thread: threading.Thread | None = None
_sync_message = ""


def _start_background_sync(trigger: str, backfill: set[str] | None = None) -> bool:
    global _sync_thread, _sync_message
    if _sync_thread and _sync_thread.is_alive():
        return False

    def work():
        global _sync_message
        try:
            run_id = sync_now(trigger=trigger, backfill_projects=backfill)
            _sync_message = f"Sync #{run_id} finished."
        except JobAlreadyRunning:
            _sync_message = "A sync was already running."
        except Exception as exc:
            _sync_message = f"Sync failed: {exc}"

    _sync_message = "Sync running…"
    _sync_thread = threading.Thread(target=work, daemon=True)
    _sync_thread.start()
    return True


@router.post("/sync", response_class=HTMLResponse)
def sync_button(request: Request, user: User = Depends(require_role(*EDITORS)), db: Session = Depends(get_db)):
    started = _start_background_sync("manual")
    audit(db, user.email, "sync.manual", "sync", None, started=started)
    db.commit()
    return sync_status(request, db, user)


@router.get("/sync/status", response_class=HTMLResponse)
def sync_status(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    running = bool(_sync_thread and _sync_thread.is_alive())
    return render(request, "partials/sync_status.html", user, running=running, message=_sync_message,
                  last_sync=_last_sync(db), next_sync=scheduler.next_run_time())


@router.get("/sync-runs", response_class=HTMLResponse)
def sync_runs(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    runs = db.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(100)).all()
    return render(request, "sync_runs.html", user, runs=runs)


# ---------------------------------------------------------------- settings

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return render(
        request, "settings.html", user,
        interval=get_setting(db, "sync_interval_minutes"),
        threshold=get_setting(db, "major_trade_threshold"),
        reviewer=get_setting(db, "default_reviewer_email"),
        contact_fill=get_setting(db, "smartsheet_contact_fill"),
        fills=db.scalars(select(AuditLog).where(AuditLog.action.in_(
            ("smartsheet.cells_filled", "smartsheet.billing_replaced"))).order_by(AuditLog.id.desc()).limit(25)).all(),
        aliases=db.scalars(select(VendorAlias).join(Vendor).order_by(Vendor.name)).all(),
        directs=db.scalars(select(DirectPurchase).order_by(DirectPurchase.created_at.desc())).all(),
        vendors={v.id: v for v in db.scalars(select(Vendor))},
        users=db.scalars(select(User).order_by(User.email)).all(), roles=ROLES,
        audit_rows=db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(50)).all(),
    )


@router.post("/settings/general")
def save_general(interval: int = Form(...), threshold: float = Form(...), reviewer: str = Form(...),
                 db: Session = Depends(get_db), user: User = Depends(require_role("Admin"))):
    if interval < 5:
        raise HTTPException(400, "Sync interval must be at least 5 minutes.")
    if interval != get_setting(db, "sync_interval_minutes"):
        set_setting(db, "sync_interval_minutes", interval, user.email)
        scheduler.reschedule(interval)
    if threshold != get_setting(db, "major_trade_threshold"):
        set_setting(db, "major_trade_threshold", threshold, user.email)
    if reviewer.strip().lower() != get_setting(db, "default_reviewer_email"):
        set_setting(db, "default_reviewer_email", reviewer.strip().lower(), user.email)
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/smartsheet-contact-fill")
def toggle_contact_fill(on: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_role("Admin"))):
    set_setting(db, "smartsheet_contact_fill", on == "1", user.email)
    db.commit()
    return RedirectResponse("/settings#smartsheet-contacts", status_code=303)


@router.post("/subs/{sc_id}/smartsheet-billing")
def update_smartsheet_billing(sc_id: int, expected: str = Form(...), db: Session = Depends(get_db),
                              user: User = Depends(require_role(*EDITORS))):
    from urllib.parse import quote

    from app.contacts import writeback
    from app.services.sync_runner import contact_writer

    sc = db.get(Subcontract, sc_id)
    if not sc:
        raise HTTPException(404)
    try:
        writeback.replace_billing(db, contact_writer(), sc, expected, user.email)
        db.commit()
        msg = "Smartsheet updated."
    except ValueError as exc:
        msg = str(exc)
    except Exception as exc:
        msg = f"Smartsheet didn't accept the change: {exc}"[:300]
    return RedirectResponse(f"/subs/{sc_id}?msg={quote(msg)}", status_code=303)


@router.post("/settings/aliases/{aid}/delete")
def delete_alias(aid: int, db: Session = Depends(get_db), user: User = Depends(require_role(*EDITORS))):
    a = db.get(VendorAlias, aid)
    if a:
        audit(db, user.email, "vendor.alias_removed", "vendor", a.vendor_id, alias=a.alias)
        db.delete(a)
        db.commit()
    return RedirectResponse("/settings#aliases", status_code=303)


@router.post("/settings/direct/{did}/delete")
def delete_direct(did: int, db: Session = Depends(get_db), user: User = Depends(require_role(*EDITORS))):
    d = db.get(DirectPurchase, did)
    if d:
        audit(db, user.email, "vendor.direct_purchase_removed", "vendor", d.vendor_id, project=d.project_number)
        db.delete(d)
        db.flush()
        _reresolve_all(db)
        db.commit()
    return RedirectResponse("/settings#direct", status_code=303)


@router.post("/settings/users/{uid}/role")
def set_role(uid: int, role: str = Form(...), db: Session = Depends(get_db),
             user: User = Depends(require_role("Admin"))):
    u = db.get(User, uid)
    if not u or role not in ROLES:
        raise HTTPException(400)
    audit(db, user.email, "user.role_changed", "user", u.email, old=u.role, new=role)
    u.role = role
    db.commit()
    return RedirectResponse("/settings#users", status_code=303)
