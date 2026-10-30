"""Phase 3 pages: Email center, Mailboxes, email templates and auto-send, verification, digest."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import current_user, require_role
from app.db.models import (
    EmailAttachment,
    EmailMessage,
    EmailUserState,
    Mailbox,
    Payable,
    Project,
    ReviewItem,
    Subcontract,
    User,
    Vendor,
    Waiver,
)
from app.db.session import get_db
from app.mail import mailboxes, outbound, templates as mail_templates
from app.mail.inbound import receive_waiver_copy
from app.services import filestore
from app.services.audit import audit
from app.services.settings_store import get_setting, set_setting
from app.waivers import service as waiver_service
from app.waivers.types import OPEN_STATUSES, WaiverStatus, WaiverType
from app.web.security import safe_file_response
from app.web.nav import safe_back, with_msg
from app.web.templating import templates

EDITORS = ("Admin", "AP")
# Email gives access to mail in contracts@ (and ap@), so only AP and Admin can see it at all.
router = APIRouter(dependencies=[Depends(require_role(*EDITORS))])


def now() -> datetime:
    return datetime.now(timezone.utc)


def render(request: Request, name: str, user: User, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"user": user, "KIND_LABELS": outbound.KIND_LABELS,
                                                      "WaiverType": WaiverType, **ctx})


def _email(db: Session, eid: int) -> EmailMessage:
    e = db.get(EmailMessage, eid)
    if not e:
        raise HTTPException(404, "Email not found")
    return e


def _addr_list(value: str) -> list[str]:
    return [a.strip() for a in value.replace(";", ",").split(",") if a.strip()]


def _state(db: Session, user: User, eids: list[int]) -> dict[int, EmailUserState]:
    if not eids:
        return {}
    return {s.email_id: s for s in db.scalars(select(EmailUserState).where(EmailUserState.user_id == user.id,
                                                                           EmailUserState.email_id.in_(eids)))}


def _set_state(db: Session, user: User, e: EmailMessage, read: bool | None = None, handled: bool | None = None):
    st = db.scalar(select(EmailUserState).where(EmailUserState.user_id == user.id, EmailUserState.email_id == e.id))
    if st is None:
        st = EmailUserState(email_id=e.id, user_id=user.id)
        db.add(st)
    if read is not None:
        st.read_at = now() if read else None
        # Mirror to Gmail only for Lien Waivers threads in a mailbox we're allowed to change.
        mb = e.mailbox
        if mb and mb.role in ("sender", "listening") and mb.status == "connected" and e.provider_message_id \
                and (e.waiver_id or e.token) and e.direction == "in":
            try:
                mailboxes.provider_for(mb).set_read(e.provider_message_id, read)
            except Exception:
                pass
    if handled is not None:
        st.handled_at = now() if handled else None


# ---------------------------------------------------------------- email center

@router.get("/email", response_class=HTMLResponse)
def email_center(request: Request, view: str = "inbox", project: str = "", vendor: int | None = None,
                 wstatus: str = "", unread: str = "", needs: str = "", q: str = "",
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = select(EmailMessage).order_by(EmailMessage.id.desc())
    if view == "outbox":
        query = query.where(EmailMessage.direction == "out", EmailMessage.status.in_(("draft", "queued", "failed")))
    elif view == "sent":
        query = query.where(EmailMessage.direction == "out", EmailMessage.status.in_(("sent", "bounced")))
    elif view == "other":
        query = query.where(EmailMessage.direction == "in", EmailMessage.kind == "other")
    else:
        view = "inbox"
        query = query.where(EmailMessage.direction == "in", EmailMessage.kind != "other")
    if project:
        query = query.where(EmailMessage.project_number == project)
    if vendor:
        query = query.where(EmailMessage.vendor_id == vendor)
    if wstatus:
        query = query.join(Waiver, EmailMessage.waiver_id == Waiver.id).where(Waiver.status == wstatus)
    if q:
        like = f"%{q}%"
        query = query.where(or_(EmailMessage.subject.ilike(like), EmailMessage.body_text.ilike(like),
                                EmailMessage.from_addr.ilike(like), EmailMessage.from_name.ilike(like)))
    rows = db.scalars(query.limit(300)).all()
    states = _state(db, user, [e.id for e in rows])
    if unread:
        rows = [e for e in rows if not (states.get(e.id) and states[e.id].read_at)]
    if needs:
        rows = [e for e in rows if not (states.get(e.id) and states[e.id].handled_at)
                and (e.waiver_id is None or (e.waiver and e.waiver.status == WaiverStatus.RECEIVED))]
    counts = {
        "outbox": db.scalar(select(func.count(EmailMessage.id)).where(
            EmailMessage.direction == "out", EmailMessage.status.in_(("draft", "queued", "failed")))) or 0,
        "unmatched": db.scalar(select(func.count(EmailMessage.id)).where(
            EmailMessage.direction == "in", EmailMessage.waiver_id.is_(None), EmailMessage.kind == "inbound")) or 0,
        "other": db.scalar(select(func.count(EmailMessage.id)).where(
            EmailMessage.direction == "in", EmailMessage.kind == "other")) or 0,
    }
    projects = db.scalars(select(Project).where(Project.is_tracked.is_(True)).order_by(Project.number)).all()
    # No DISTINCT over vendors: Postgres can't compare their JSON column.
    tracked_vendors = select(Subcontract.vendor_id).join(Project).where(Project.is_tracked.is_(True))
    vendors = db.scalars(select(Vendor).where(Vendor.id.in_(tracked_vendors)).order_by(Vendor.name)).all()
    return render(request, "email_center.html", user, view=view, rows=rows, states=states, counts=counts,
                  projects=projects, vendors=vendors, sender=mailboxes.active_sender(db),
                  f={"project": project, "vendor": vendor, "wstatus": wstatus, "unread": unread, "needs": needs,
                     "q": q}, statuses=list(WaiverStatus))


@router.get("/email/all", response_class=HTMLResponse)
def all_mail(request: Request, q: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    """The whole Sender mailbox, read live from Gmail (nothing is stored)."""
    mb = mailboxes.active_sender(db)
    rows, error = [], None
    if mb is None:
        error = "No Sender mailbox is connected."
    else:
        try:
            rows = mailboxes.provider_for(mb).list_recent(q, 50)
        except Exception as exc:
            error = f"Couldn't read {mb.email}: {exc}"
    known = set()
    if rows:
        known = set(db.scalars(select(EmailMessage.provider_message_id).where(
            EmailMessage.provider_message_id.in_([r.message_id for r in rows]))))
    return render(request, "email_all.html", user, mb=mb, rows=rows, error=error, q=q, known=known)


@router.post("/email/all/{message_id}/import")
def import_live(message_id: str, db: Session = Depends(get_db), user: User = Depends(require_role(*EDITORS))):
    """Bring one message from the whole-mailbox view into the app so it can be linked."""
    from app.mail.inbound import _store
    mb = mailboxes.active_sender(db)
    if mb is None:
        raise HTTPException(400, "No Sender mailbox")
    existing = db.scalar(select(EmailMessage).where(EmailMessage.provider_message_id == message_id))
    if existing:
        return RedirectResponse(f"/email/{existing.id}", status_code=303)
    msg = mailboxes.provider_for(mb).get_message(message_id)
    e = _store(db, mb, msg, direction="in", status="received")
    outbound.log_email(db, e, user.email, "imported")
    db.commit()
    return RedirectResponse(f"/email/{e.id}", status_code=303)


@router.get("/email/{eid}", response_class=HTMLResponse)
def email_thread(request: Request, eid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    e = _email(db, eid)
    if e.direction == "out" and e.status in ("draft", "queued", "failed"):
        return RedirectResponse(f"/email/{eid}/edit", status_code=303)
    thread = [e]
    if e.provider_thread_id:
        thread = db.scalars(select(EmailMessage).where(EmailMessage.provider_thread_id == e.provider_thread_id,
                                                       EmailMessage.mailbox_id == e.mailbox_id)
                            .order_by(EmailMessage.id)).all()
    if e.direction == "in":
        _set_state(db, user, e, read=True)
        db.commit()
    waivers = _link_choices(db, e)
    states = _state(db, user, [x.id for x in thread])
    return render(request, "email_thread.html", user, e=e, thread=thread, waivers=waivers, states=states,
                  reply_templates=[("reply", "Free-form reply"), ("reminder_1", "Reminder"),
                                   ("reminder_2", "Second reminder"), ("correction", "Needs correction"),
                                   ("received", "Received")])


def _link_choices(db: Session, e: EmailMessage) -> list[Waiver]:
    q = select(Waiver).join(Subcontract).join(Project).where(
        Project.is_tracked.is_(True), Waiver.status.in_([s.value for s in OPEN_STATUSES]))
    if e.vendor_id:
        q = q.where(Subcontract.vendor_id == e.vendor_id)
    return db.scalars(q.order_by(Waiver.id.desc()).limit(100)).all()


@router.get("/email/attachment/{aid}")
def attachment(aid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    a = db.get(EmailAttachment, aid)
    local = filestore.ensure(db, a.path) if a else None
    if local is None:
        raise HTTPException(404, "The attachment file is missing.")
    return safe_file_response(str(local), a.filename, a.content_type)


@router.post("/email/{eid}/link")
def link(eid: int, waiver_id: int = Form(...), db: Session = Depends(get_db),
         user: User = Depends(require_role(*EDITORS))):
    e = _email(db, eid)
    w = db.get(Waiver, waiver_id)
    if not w:
        raise HTTPException(404)
    e.waiver_id, e.match_method = w.id, "manual"
    e.project_number, e.vendor_id = w.subcontract.project.number, w.subcontract.vendor_id
    outbound.log_email(db, e, user.email, "linked", waiver_id=w.id)
    audit(db, user.email, "email.linked", "email", e.id, waiver_id=w.id)
    db.commit()
    return RedirectResponse(f"/email/{eid}", status_code=303)


@router.post("/email/{eid}/use-as-signed")
def use_as_signed(eid: int, attachment_id: int = Form(...), db: Session = Depends(get_db),
                  user: User = Depends(require_role(*EDITORS))):
    """File an attachment (e.g. one found in ap@) as the signed copy of the linked waiver."""
    e = _email(db, eid)
    a = db.get(EmailAttachment, attachment_id)
    if not e.waiver or not a or a.email_id != e.id:
        raise HTTPException(400, "Link the email to a waiver first.")
    ok = receive_waiver_copy(db, e.waiver, e, user.email, attachment=a)
    db.commit()
    msg = "" if ok else "?msg=" + quote("That waiver can't take a signed copy in its current status.")
    return RedirectResponse(f"/waivers/{e.waiver_id}{msg}" if ok else f"/email/{eid}{msg}", status_code=303)


@router.post("/email/{eid}/state")
def set_state(eid: int, read: str = Form(""), handled: str = Form(""), db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    e = _email(db, eid)
    _set_state(db, user, e, read={"yes": True, "no": False}.get(read), handled={"yes": True, "no": False}.get(handled))
    db.commit()
    return RedirectResponse(f"/email/{eid}" if read != "no" else "/email", status_code=303)


@router.post("/email/{eid}/reply")
def reply(eid: int, mode: str = Form("reply"), template: str = Form("reply"), reason: str = Form(""),
          db: Session = Depends(get_db), user: User = Depends(require_role(*EDITORS))):
    e = _email(db, eid)
    if mode == "forward":
        mb = e.mailbox or mailboxes.active_sender(db)
        fwd = (f"\n\n---------- Forwarded message ----------\nFrom: {e.from_name or ''} <{e.from_addr}>\n"
               f"Subject: {e.subject}\n\n{e.body_text or ''}")
        d = outbound.draft_free(db, user.email, [], [], "Fwd: " + (e.subject or ""),
                                ((mb.signature or "") if mb else "") + fwd, waiver_id=e.waiver_id)
        for a in e.attachments:
            db.add(EmailAttachment(email_id=d.id, filename=a.filename, content_type=a.content_type, size=a.size,
                                   path=a.path))
    elif template == "reply":
        mb = e.mailbox or mailboxes.active_sender(db)
        first = mail_templates.first_name(e.from_name) if e.direction == "in" else ""
        body = (f"Hi {first},\n\n\n\nThanks,\n" if first else "Hi,\n\n\n\nThanks,\n") + ((mb.signature or mb.display_name or "") if mb else "")
        d = outbound.draft_reply(db, e, "free", body=body, actor=user.email, reply_all=(mode == "reply_all"))
    else:
        if not e.waiver_id and template != "received":
            raise HTTPException(400, "Link this email to a waiver to use that template.")
        d = outbound.draft_reply(db, e, template, values={"reason": reason} if reason else None, actor=user.email,
                                 reply_all=(mode == "reply_all"), attach_waiver=(template == "reminder_1"))
    db.commit()
    return RedirectResponse(f"/email/{d.id}/edit", status_code=303)


@router.get("/email-new", response_class=HTMLResponse)
def new_email(request: Request, waiver_id: int | None = None, db: Session = Depends(get_db),
              user: User = Depends(require_role(*EDITORS))):
    w = db.get(Waiver, waiver_id) if waiver_id else None
    to, cc = outbound.recipients_for(w) if w else ([], [])
    mb = mailboxes.sender_for_project(db, w.subcontract.project if w else None)
    body = "Hi,\n\n\n\nThanks,\n" + ((mb.signature or mb.display_name or "") if mb else "")
    d = outbound.draft_free(db, user.email, to, cc, "", body, waiver_id=waiver_id)
    db.commit()
    return RedirectResponse(f"/email/{d.id}/edit", status_code=303)


@router.get("/email/{eid}/edit", response_class=HTMLResponse)
def edit_draft(request: Request, eid: int, msg: str = "", db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    e = _email(db, eid)
    if e.direction != "out" or e.status not in ("draft", "queued", "failed"):
        return RedirectResponse(f"/email/{eid}", status_code=303)
    original = db.get(EmailMessage, e.reply_to_email_id) if e.reply_to_email_id else None
    problems = mail_templates.lint(e.body_text or "")
    return render(request, "email_edit.html", user, e=e, original=original, msg=msg, problems=problems,
                  boxes=[m for m in mailboxes.mailboxes(db) if m.can_send or m.role == "sender"])


@router.post("/email/{eid}/edit")
def save_draft(eid: int, to: str = Form(""), cc: str = Form(""), subject: str = Form(""), body: str = Form(""),
               mailbox_id: int | None = Form(None), action: str = Form("save"),
               remove_attachment: list[int] = Form([]), db: Session = Depends(get_db),
               user: User = Depends(require_role(*EDITORS))):
    e = _email(db, eid)
    if e.status not in ("draft", "queued", "failed"):
        raise HTTPException(400, "Already sent")
    e.to_addrs, e.cc_addrs, e.subject, e.body_text = _addr_list(to), _addr_list(cc), subject, body.replace("\r\n", "\n")
    if mailbox_id and not e.provider_thread_id:
        e.mailbox_id = mailbox_id
    for a in list(e.attachments):
        if a.id in remove_attachment:
            db.delete(a)
    if e.to_addrs:
        e.error = None if e.status != "failed" else e.error
    outbound.log_email(db, e, user.email, "edited")
    db.flush()
    db.refresh(e)
    if action == "send":
        return _send_one(db, e, user)
    if action == "discard":
        e.status = "discarded"
        outbound.log_email(db, e, user.email, "discarded")
        db.commit()
        return RedirectResponse("/email?view=outbox", status_code=303)
    db.commit()
    return RedirectResponse(f"/email/{eid}/edit?msg=Saved", status_code=303)


def _send_one(db: Session, e: EmailMessage, user: User, back: str = ""):
    try:
        ok = outbound.send(db, e, user.email)
    except ValueError as exc:
        db.rollback()
        if back:
            return RedirectResponse(with_msg(safe_back(back, "/"), str(exc)), status_code=303)
        return RedirectResponse(f"/email/{e.id}/edit?msg=" + quote(str(exc)), status_code=303)
    db.commit()
    if not ok:
        if back:
            return RedirectResponse(with_msg(safe_back(back, "/"), f"Not sent: {e.error or 'unknown problem'}"),
                                    status_code=303)
        return RedirectResponse(f"/email/{e.id}/edit?msg=" + quote(e.error or "Not sent"), status_code=303)
    if back:
        return RedirectResponse(with_msg(safe_back(back, "/"), "Sent."), status_code=303)
    return RedirectResponse("/email?view=outbox&sent=1", status_code=303)


@router.post("/email/{eid}/send")
def send_one(eid: int, back: str = Form(""), db: Session = Depends(get_db),
             user: User = Depends(require_role(*EDITORS))):
    return _send_one(db, _email(db, eid), user, back)


@router.post("/email/send-selected")
def send_selected(ids: list[int] = Form([]), db: Session = Depends(get_db),
                  user: User = Depends(require_role(*EDITORS))):
    sent = failed = 0
    for eid in ids:
        e = db.get(EmailMessage, eid)
        if not e or e.status not in ("draft", "queued", "failed"):
            continue
        try:
            if outbound.send(db, e, user.email):
                sent += 1
            else:
                failed += 1
        except ValueError:
            failed += 1
        db.commit()
    msg = f"Sent {sent}." + (f" {failed} couldn't be sent; see the error on each." if failed else "")
    return RedirectResponse("/email?view=outbox&msg=" + quote(msg), status_code=303)


@router.post("/email/{eid}/discard")
def discard(eid: int, db: Session = Depends(get_db), user: User = Depends(require_role(*EDITORS))):
    e = _email(db, eid)
    if e.status in ("draft", "queued", "failed"):
        e.status = "discarded"
        outbound.log_email(db, e, user.email, "discarded")
        db.commit()
    return RedirectResponse("/email?view=outbox", status_code=303)


# ---------------------------------------------------------------- mail checks

_mail_thread: threading.Thread | None = None


@router.post("/mail/check-now")
def check_now(user: User = Depends(require_role(*EDITORS))):
    global _mail_thread
    from app.services.sync_runner import mail_now

    if not (_mail_thread and _mail_thread.is_alive()):
        def work():
            try:
                mail_now()
            except Exception:
                pass
        _mail_thread = threading.Thread(target=work, daemon=True)
        _mail_thread.start()
    return RedirectResponse("/email?msg=" + quote("Checking mail. Refresh in a few seconds."), status_code=303)


# ---------------------------------------------------------------- mailboxes

@router.get("/settings/mailboxes", response_class=HTMLResponse)
def mailboxes_page(request: Request, msg: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    boxes = mailboxes.mailboxes(db)
    listening_done = {m.id: not mailboxes.open_threads(db, m) for m in boxes if m.role == "listening"}
    projects = db.scalars(select(Project).where(Project.is_tracked.is_(True)).order_by(Project.number)).all()
    from app.contacts import backfill
    from app.db.models import ContactBackfill
    backfills = {b.mailbox_id: b for b in db.scalars(select(ContactBackfill))}
    return render(request, "mailboxes.html", user, boxes=boxes, msg=msg, listening_done=listening_done,
                  backfills=backfills, progress=backfill.progress,
                  projects=projects, has_send=mailboxes.has_send_scope)


@router.post("/settings/mailboxes/{mid}")
def update_mailbox(mid: int, action: str = Form(...), display_name: str = Form(""), signature: str = Form(""),
                   only_to: str = Form(""), found_in_label: str = Form(""), db: Session = Depends(get_db),
                   user: User = Depends(require_role("Admin"))):
    mb = db.get(Mailbox, mid)
    if not mb:
        raise HTTPException(404)
    msg = "Saved."
    try:
        if action == "save":
            mb.display_name = display_name.strip() or None
            mb.signature = signature.replace("\r\n", "\n").strip() or None
            mb.only_to = only_to.strip().lower() or None
            mb.found_in_label = found_in_label.strip() or None
            audit(db, user.email, "mailbox.updated", "mailbox", mb.email, only_to=mb.only_to)
        elif action == "make_sender":
            mailboxes.make_sender(db, mb, user.email)
            msg = f"{mb.email} is now the Sender. New emails go out from it."
        elif action == "disconnect":
            mailboxes.disconnect(db, mb, user.email)
            msg = f"Disconnected {mb.email} and revoked the app's access."
        db.commit()
    except ValueError as exc:
        db.rollback()
        msg = str(exc)
    return RedirectResponse("/settings/mailboxes?msg=" + quote(msg), status_code=303)


@router.post("/projects/{number}/sender")
def project_sender(number: str, mailbox_id: str = Form(""), db: Session = Depends(get_db),
                   user: User = Depends(require_role("Admin"))):
    p = db.scalar(select(Project).where(Project.number == number))
    if not p:
        raise HTTPException(404)
    p.sender_mailbox_id = int(mailbox_id) if mailbox_id else None
    audit(db, user.email, "project.sender_override", "project", number, mailbox_id=p.sender_mailbox_id)
    db.commit()
    return RedirectResponse("/settings/mailboxes#overrides", status_code=303)


# ---------------------------------------------------------------- email settings

@router.get("/settings/email", response_class=HTMLResponse)
def email_settings(request: Request, msg: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    overrides = get_setting(db, "email_templates") or {}
    rows = []
    for key, (fname, label) in mail_templates.TEMPLATES.items():
        text = overrides.get(key) or mail_templates.file_text(key)
        rows.append({"key": key, "label": label, "text": text, "custom": key in overrides,
                     "problems": mail_templates.lint(text)})
    return render(request, "email_settings.html", user, rows=rows, msg=msg,
                  auto_send=get_setting(db, "auto_send") or {}, kinds=outbound.AUTO_SEND_KINDS,
                  received=bool(get_setting(db, "send_received_reply")),
                  reminder_days=get_setting(db, "reminder_days"), escalation_day=get_setting(db, "escalation_day"))


@router.post("/settings/email/template/{key}")
def save_template(key: str, text: str = Form(""), reset: str = Form(""), db: Session = Depends(get_db),
                  user: User = Depends(require_role("Admin"))):
    if key not in mail_templates.TEMPLATES:
        raise HTTPException(404)
    overrides = dict(get_setting(db, "email_templates") or {})
    if reset:
        overrides.pop(key, None)
        msg = "Template reset to the default."
    else:
        text = text.replace("\r\n", "\n").strip() + "\n"
        problems = mail_templates.lint(text)
        if problems:
            return RedirectResponse("/settings/email?msg=" + quote("Not saved: " + " ".join(problems)), status_code=303)
        overrides[key] = text
        msg = "Template saved."
    set_setting(db, "email_templates", overrides, user.email)
    db.commit()
    return RedirectResponse("/settings/email?msg=" + quote(msg) + f"#{key}", status_code=303)


@router.post("/settings/email/sending")
async def save_sending(request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("Admin"))):
    form = await request.form()
    auto = {k: form.get(f"auto_{k}") == "on" for k in outbound.AUTO_SEND_KINDS}
    auto = {k: v for k, v in auto.items() if v}
    if auto != (get_setting(db, "auto_send") or {}):
        set_setting(db, "auto_send", auto, user.email)
    try:
        days = sorted({int(form.get("reminder_1", 7)), int(form.get("reminder_2", 10))})
        esc = int(form.get("escalation_day", 13))
    except ValueError:
        return RedirectResponse("/settings/email?msg=" + quote("Reminder days must be whole numbers."), status_code=303)
    if len(days) != 2 or days[0] < 1 or esc <= days[-1]:
        return RedirectResponse("/settings/email?msg=" + quote(
            "Use two different reminder days, and an escalation day after the second reminder."), status_code=303)
    if days != get_setting(db, "reminder_days"):
        set_setting(db, "reminder_days", days, user.email)
    if esc != get_setting(db, "escalation_day"):
        set_setting(db, "escalation_day", esc, user.email)
    received = form.get("received") == "on"
    if received != bool(get_setting(db, "send_received_reply")):
        set_setting(db, "send_received_reply", received, user.email)
    db.commit()
    return RedirectResponse("/settings/email?msg=" + quote("Sending settings saved."), status_code=303)


# ---------------------------------------------------------------- digest

@router.get("/digest", response_class=HTMLResponse)
def digest(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    tracked = select(Project.number).where(Project.is_tracked.is_(True))
    outstanding = db.scalars(select(Waiver).join(Subcontract).join(Project).where(
        Project.is_tracked.is_(True), Waiver.status.in_((WaiverStatus.SENT.value, WaiverStatus.REJECTED.value)))
        .order_by(Waiver.sent_at)).all()
    to_verify = db.scalars(select(Waiver).join(Subcontract).join(Project).where(
        Project.is_tracked.is_(True), Waiver.status == WaiverStatus.RECEIVED.value)).all()
    holds = db.scalars(select(Payable).where(Payable.project_number.in_(tracked),
                                             func.lower(Payable.approval_status) == "hold",
                                             Payable.deleted_in_source.is_(False))).all()
    outbox = db.scalar(select(func.count(EmailMessage.id)).where(
        EmailMessage.direction == "out", EmailMessage.status.in_(("draft", "queued", "failed")))) or 0
    unmatched = db.scalar(select(func.count(EmailMessage.id)).where(
        EmailMessage.direction == "in", EmailMessage.waiver_id.is_(None), EmailMessage.kind == "inbound")) or 0
    reviews = db.scalar(select(func.count(ReviewItem.id)).where(ReviewItem.status == "open",
                                                                ReviewItem.project_number.in_(tracked))) or 0
    today = now()

    def age(w: Waiver) -> int:
        sent = w.sent_at if w.sent_at.tzinfo else w.sent_at.replace(tzinfo=timezone.utc)
        return (today - sent).days

    days = {w.id: age(w) for w in outstanding if w.sent_at}
    return render(request, "digest.html", user, outstanding=outstanding, to_verify=to_verify, holds=holds,
                  outbox=outbox, unmatched=unmatched, reviews=reviews, today=today, days=days)
