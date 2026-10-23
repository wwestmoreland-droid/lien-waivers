"""Preparing waivers for invoices that were already paid when the job was first tracked."""
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import EmailMessage, Payable, Waiver
from app.main import create_app
from app.waivers import service
from tests.test_email_e2e import mail  # noqa: F401


def test_past_paid_invoices_can_be_prepared(mail):
    db = mail.db
    assert db.scalar(select(Waiver)) is None  # history from tracking creates no waivers on its own
    rows = service.past_paid_invoices(db)
    ready = [r for r in rows if r.can_prepare]
    assert ready and all(r.payable.paid_date for r in rows)

    first = ready[0].payable
    others = [r.payable for r in ready[1:]]
    assert service.past_paid_invoices(db, paid_from=first.paid_date, paid_to=first.paid_date)[0].payable.paid_date \
        == first.paid_date

    with TestClient(create_app()) as c:
        page = c.get("/waivers-past")
        assert page.status_code == 200 and "Prepare waivers for selected" in page.text
        r = c.post("/waivers-past/prepare", data={"payable_id": [str(first.id)]})
        assert r.status_code == 200 and "Prepared 1 waiver" in r.text
    db.expire_all()
    w = db.scalar(select(Waiver).where(Waiver.payable_id == first.id))
    assert w is not None and w.type == ready[0].wtype.value
    assert db.scalar(select(EmailMessage).where(EmailMessage.waiver_id == w.id)).status == "draft"
    # Gone from the "without a waiver" list; still there under All.
    assert first.id not in {r.payable.id for r in service.past_paid_invoices(db)}
    assert first.id in {r.payable.id for r in service.past_paid_invoices(db, only_missing=False)}
    # Preparing the same one again does nothing.
    assert service.prepare_past(db, [first.id], "ap") == []
    assert {x.id for x in others} <= {r.payable.id for r in service.past_paid_invoices(db)}


def test_past_invoices_page_for_pm_has_no_prepare(mail, monkeypatch):
    from app.db.models import User
    db = mail.db
    with TestClient(create_app()) as c:
        c.get("/")
        u = db.scalar(select(User))
        u.role = "PM"
        db.commit()
        page = c.get("/waivers-past")
        assert page.status_code == 200 and "Prepare waivers for selected" not in page.text
        assert c.post("/waivers-past/prepare", data={"payable_id": ["1"]}).status_code == 403
