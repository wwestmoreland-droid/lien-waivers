"""The signed waiver that counts per sub, and all of them in one PDF."""
import io

from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from sqlalchemy import select

from app.db.models import Project, Subcontract, Waiver
from app.main import create_app
from app.waivers import service
from app.waivers.types import WaiverType
from tests.fixtures.sheets import PROJECT
from tests.test_email_e2e import mail, pay  # noqa: F401


def signed_pdf(text: str, pages: int = 1) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(pages):
        c.drawString(72, 700, f"{text} page {i + 1} -- signed")
        c.showPage()
    c.save()
    return buf.getvalue()


def photo() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (800, 1000), "white").save(buf, "PNG")
    return buf.getvalue()


def sign(db, w, data, name, accept=True):
    service.mark_sent(db, w, "ap")
    service.mark_received(db, w, "ap", name, data)
    if accept:
        service.accept(db, w, "ap")
    db.commit()


def test_best_signed_waiver_and_download(mail):
    db = mail.db
    project = db.scalar(select(Project).where(Project.number == PROJECT))
    # Delta: a signed partial.
    delta_partial = pay(mail, 1003)
    sign(db, delta_partial, signed_pdf("Delta partial", pages=2), "delta.pdf")
    # Ridge: two signed partials from past invoices (newest wins), one sent back as a phone photo.
    ready = [r for r in service.past_paid_invoices(db) if r.sc and r.sc.vendor.name == "Ridge Plumbing"]
    ridge = service.prepare_past(db, [r.payable.id for r in ready], "ap")
    db.commit()
    older, newer = sorted(ridge, key=lambda w: w.through_date)
    sign(db, older, signed_pdf("Ridge old"), "old.pdf")
    sign(db, newer, photo(), "scan.png", accept=False)  # received, not reviewed yet

    rows = {r["sc"].vendor.name: r for r in service.job_waiver_rows(db, project)}
    assert rows["Delta Fire Systems, Inc"]["waiver"].id == delta_partial.id
    assert rows["Ridge Plumbing"]["waiver"].id == newer.id
    assert rows["Ridge Plumbing"]["invoice"] is not None
    assert rows["Summit Electric, LLC"]["waiver"] is None or rows["Summit Electric, LLC"]["waiver"].signed_pdf_path

    # A signed conditional final beats partials; an unconditional final beats both.
    sc = db.get(Subcontract, delta_partial.subcontract_id)
    cf = service.create_waiver(db, delta_partial.payable, sc, WaiverType.CONDITIONAL_FINAL, None)
    sign(db, cf, signed_pdf("Delta cond final"), "cf.pdf")
    assert service.best_signed_waiver(db, sc).id == cf.id
    uf = service.create_waiver(db, delta_partial.payable, sc, WaiverType.UNCONDITIONAL_FINAL, None)
    sign(db, uf, signed_pdf("Delta uncond final"), "uf.pdf")
    assert service.best_signed_waiver(db, sc).id == uf.id
    service.void(db, uf, "ap", "wrong")
    db.commit()
    assert service.best_signed_waiver(db, sc).id == cf.id  # cancelled ones don't count

    with TestClient(create_app()) as c:
        page = c.get(f"/jobs/{PROJECT}")
        assert "Download waivers" in page.text and "not reviewed yet" in page.text
        r = c.get(f"/jobs/{PROJECT}/signed-waivers.pdf")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        pdf = PdfReader(io.BytesIO(r.content))
        texts = [p.extract_text() for p in pdf.pages]
        assert len(pdf.pages) == 3  # cover + Delta's conditional final + Ridge's photo
        assert "No signed waiver yet" in texts[0] and "Delta Fire Systems" in texts[0]  # who's missing, up front
        assert "Delta cond final" in texts[1]
        assert f"Lien waivers {PROJECT}" in r.headers["content-disposition"]
        # The waiver page and list show the signed copy, not the one we generated.
        detail = c.get(f"/waivers/{cf.id}")
        assert f"/waivers/{cf.id}/signed" in detail.text and "Open signed copy" in detail.text
        assert ">Signed</a>" in c.get("/waivers?status=").text
