import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AuditLog, Payable, Project, Vendor, VendorAlias
from app.main import create_app
from app.services import sync_runner
from app.web import routes
from tests.fixtures.sheets import PROJECT


@pytest.fixture
def client(engine, source, monkeypatch):
    sync_runner.set_source_factory(lambda: source)
    # Run "background" syncs inline so tests are deterministic.
    monkeypatch.setattr(routes, "_start_background_sync",
                        lambda trigger, backfill=None: sync_runner.sync_now(trigger, backfill) or True)
    with TestClient(create_app()) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_full_phase1_flow(client, db):
    assert client.get("/").status_code == 200  # empty dashboard renders
    sync_runner.sync_now("test")

    r = client.get("/projects")
    assert r.status_code == 200 and PROJECT in r.text and "New – not tracked" in r.text

    r = client.post(f"/projects/{PROJECT}/track")
    assert r.status_code == 200 and "Tracking" in r.text

    for path in ("/", "/jobs", "/jobs?by=sub", "/jobs?by=pm", f"/jobs/{PROJECT}", "/queues", "/settings", "/sync-runs"):
        assert client.get(path).status_code == 200, path

    sub_id = db.scalar(select(Payable).where(Payable.source_row_id == 1001)).subcontract_id
    r = client.get(f"/subs/{sub_id}")
    assert r.status_code == 200 and "$4,148.60" in r.text

    csv = client.get("/reports/ctc.csv")
    assert csv.status_code == 200 and "Delta Fire Systems, Inc" in csv.text
    xlsx = client.get(f"/reports/ctc.xlsx?project={PROJECT}")
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"

    # Unmatched queue: mark BP Waste Services as a direct purchase on this job.
    bp = db.scalar(select(Payable).where(Payable.source_row_id == 1010))
    assert "BP Waste Services" in client.get("/queues").text
    r = client.post(f"/queues/payables/{bp.id}/direct-purchase", data={"scope": "job"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Payable, bp.id).match_status == "direct_purchase"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "vendor.direct_purchase"))


def test_map_vendor_from_queue(client, db, source):
    sync_runner.sync_now("test")
    client.post(f"/projects/{PROJECT}/track")
    source.row("ar_ap", 1004).values["vendor_and_project"] = "Ridge Plumbing & Heating"
    sync_runner.sync_now("test")
    p = db.scalar(select(Payable).where(Payable.source_row_id == 1004))
    assert p.match_status == "unmatched_vendor"
    ridge = db.scalar(select(Vendor).where(Vendor.name == "Ridge Plumbing"))
    client.post(f"/queues/payables/{p.id}/map-vendor", data={"vendor_id": ridge.id})
    db.expire_all()
    assert db.get(Payable, p.id).match_status == "matched"
    assert db.scalar(select(VendorAlias).where(VendorAlias.alias == "Ridge Plumbing & Heating"))


def test_untrack_asks_for_confirmation_when_items_open(client, db):
    sync_runner.sync_now("test")
    client.post(f"/projects/{PROJECT}/track")
    r = client.post(f"/projects/{PROJECT}/untrack")
    assert "Untrack anyway" in r.text
    assert db.scalar(select(Project).where(Project.number == PROJECT)).is_tracked
    r = client.post(f"/projects/{PROJECT}/untrack", data={"confirm": "yes"})
    db.expire_all()
    assert not db.scalar(select(Project).where(Project.number == PROJECT)).is_tracked


def test_pm_role_cannot_track(client, db):
    sync_runner.sync_now("test")
    from app.db.models import User
    client.get("/")
    u = db.scalar(select(User))
    u.role = "PM"
    db.commit()
    assert client.post(f"/projects/{PROJECT}/track").status_code == 403


def test_smartsheet_client_refuses_writes():
    import httpx

    from app.smartsheet.client import _ReadOnlyTransport

    t = _ReadOnlyTransport()
    with pytest.raises(PermissionError):
        t.handle_request(httpx.Request("PUT", "https://api.smartsheet.com/2.0/sheets/1/rows"))
