"""Waiver PDFs generate on the current pypdf, and missing ones are made again."""
import io
from pathlib import Path

import pytest
from pypdf import PdfReader
from sqlalchemy import delete, select

from app.db.models import EmailAttachment, EmailMessage, StoredFile, Subcontract, Waiver
from app.services import filestore
from app.smartsheet.sync import run_sync
from app.waivers import pdf as waiver_pdf
from app.waivers import service as waivers
from tests.test_email_e2e import mail, pay, request_email  # noqa: F401


def test_unconditional_final_pdf_generates(mail):
    db = mail.db
    sc = [s for s in db.scalars(select(Subcontract)) if waivers.final_checks(s)["last_paid"]][0]
    w = waivers.prepare_unconditional_final(db, sc, "ap")
    db.commit()
    data = filestore.read(db, w.generated_pdf_path)
    assert data[:5] == b"%PDF-" and len(PdfReader(io.BytesIO(data)).pages) >= 1
    waivers.regenerate(db, w, "ap")  # what the Regenerate button does
    db.commit()
    assert w.pdf_version == 2 and filestore.has(db, w.generated_pdf_path)


def test_missing_pdfs_are_made_again_on_sync(mail):
    db = mail.db
    w = pay(mail)
    path = w.generated_pdf_path
    db.execute(delete(StoredFile).where(StoredFile.key == filestore.key_for(path)))
    Path(path).unlink(missing_ok=True)
    db.commit()
    assert not filestore.has(db, path)
    run_sync(db, mail.source, trigger="test")
    db.commit()
    db.expire_all()
    w = db.get(Waiver, w.id)
    assert filestore.has(db, w.generated_pdf_path)
    e = request_email(db, w)
    atts = db.scalars(select(EmailAttachment).where(EmailAttachment.email_id == e.id)).all()
    assert atts and all(filestore.has(db, a.path) for a in atts)


def test_one_failing_waiver_doesnt_stop_the_sync(mail, monkeypatch):
    db = mail.db
    real = waiver_pdf.fill
    monkeypatch.setattr(waiver_pdf, "fill", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    mail.source.row("ar_ap", 1003).values["paid_date"] = "2026-10-22"
    run = run_sync(db, mail.source, trigger="test")
    db.commit()
    assert run.stats.get("events_failed", 0) >= 1 and run.finished_at is not None
    monkeypatch.setattr(waiver_pdf, "fill", real)
    run_sync(db, mail.source, trigger="test")  # tried again once it works
    db.commit()
    assert db.scalars(select(Waiver)).all()


def test_regenerate_explains_instead_of_crashing(mail, monkeypatch):
    db = mail.db
    w = pay(mail)
    monkeypatch.setattr(waiver_pdf, "fill", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad template")))
    with pytest.raises(ValueError, match="Couldn't make the waiver PDF"):
        waivers.regenerate(db, db.get(Waiver, w.id), "ap")
