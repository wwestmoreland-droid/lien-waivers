"""Signed copies become upright 8.5 x 11 PDFs."""
import io

from pypdf import PdfReader, PdfWriter

from app.services import filestore, paper
from app.waivers import service as waivers
from app.waivers.types import WaiverStatus
from tests.test_email_e2e import mail  # noqa: F401
from tests.test_reply_scenarios import W, photo, reply, send_request
from app.mail.provider import InAttachment


def pages(data):
    return [(round(float(p.mediabox.width)), round(float(p.mediabox.height))) for p in PdfReader(io.BytesIO(data)).pages]


def jpeg(size, orientation=None):
    from PIL import Image
    im = Image.effect_noise(size, 60).convert("RGB")
    buf = io.BytesIO()
    if orientation:
        exif = im.getexif()
        exif[274] = orientation
        im.save(buf, "JPEG", exif=exif)
    else:
        im.save(buf, "JPEG")
    return buf.getvalue()


def pdf(*sizes, rotate=0):
    w = PdfWriter()
    for size in sizes:
        p = w.add_blank_page(*size)
        if rotate:
            p.rotate(rotate)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_phone_photo_is_turned_and_fit_on_a_letter_page():
    # A sideways phone photo (stored 1200 x 900, EXIF says turn it): a portrait Letter page.
    out = paper.to_letter_pdf(jpeg((1200, 900), orientation=6), "IMG_1.jpg")
    assert pages(out) == [(612, 792)]
    # A plain landscape photo stays landscape, on a landscape Letter sheet.
    assert pages(paper.to_letter_pdf(jpeg((1200, 900)), "IMG_2.jpg")) == [(792, 612)]
    # Asked to turn it a quarter (the check said it reads sideways): portrait.
    assert pages(paper.to_letter_pdf(jpeg((1200, 900)), "IMG_3.jpg", rotate=90)) == [(612, 792)]


def test_pdf_pages_become_letter_and_upright():
    legal = (612, 1008)
    out = paper.to_letter_pdf(pdf(legal, (612, 792), (200, 300)), "mixed.pdf")
    assert pages(out) == [(612, 792)] * 3 and paper.is_letter(out)
    turned = paper.to_letter_pdf(pdf((612, 792), rotate=90), "sideways.pdf")  # /Rotate baked in
    assert pages(turned) == [(792, 612)] and not PdfReader(io.BytesIO(turned)).pages[0].get("/Rotate")
    assert paper.to_letter_pdf(b"not a file", "x.docx") is None  # kept as sent


def test_signed_copy_on_record_is_letter_and_the_original_is_kept(mail):
    db = mail.db
    w, e = send_request(mail)
    big = photo()
    reply(mail, e, attachments=[InAttachment("IMG_2231.jpg", "image/jpeg", big)])
    ww = W(db, w)
    assert ww.status == WaiverStatus.RECEIVED and ww.signed_pdf_path.endswith(".pdf")
    assert paper.is_letter(filestore.read(db, ww.signed_pdf_path))
    assert filestore.read(db, waivers.original_file(db, ww)) == big


def test_turned_upright_when_the_check_says_so(mail):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, attachments=[InAttachment("scan.jpg", "image/jpeg", jpeg((1200, 900)))])
    ww = W(db, w)
    assert waivers.straighten(db, ww, "90")
    db.commit()
    ww = W(db, w)
    assert ww.signed_pdf_path.endswith("-upright.pdf") and pages(filestore.read(db, ww.signed_pdf_path)) == [(612, 792)]
    assert not waivers.straighten(db, ww, "0")


def test_phone_orientation_isnt_turned_twice(mail):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, attachments=[InAttachment("IMG_9.jpg", "image/jpeg", jpeg((1200, 900), orientation=6))])
    assert not waivers.straighten(db, W(db, w), "90")


def test_copies_filed_before_are_caught_up(mail):
    db = mail.db
    w, e = send_request(mail)
    reply(mail, e, attachments=[InAttachment("IMG_2231.jpg", "image/jpeg", photo())])
    ww = W(db, w)
    old = ww.signed_pdf_path.replace(".pdf", ".jpg")
    filestore.write(db, old, photo())  # how it was stored before this change
    ww.signed_pdf_path = old
    db.commit()
    assert waivers.letter_size_all(db) == 1
    db.commit()
    assert paper.is_letter(filestore.read(db, W(db, w).signed_pdf_path))
    assert waivers.letter_size_all(db) == 0  # once each
