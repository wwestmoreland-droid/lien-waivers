"""Signed copies as plain 8.5 x 11 PDFs: upright, scaled to fit the sheet, one page per page.

Subs send phone photos, scans in odd sizes, sideways pages and legal-size PDFs. Each is
turned into a US Letter PDF: photos are turned the way the phone says (EXIF), any extra
quarter-turn asked for is applied (the automatic check says which way is up), and every page
is scaled to fit inside a 1/4" margin, centred. Landscape content goes on a landscape sheet
(still 8.5 x 11). A PDF page that's already Letter and upright is left as it is.
"""
from __future__ import annotations

import io

import structlog

log = structlog.get_logger()

LETTER = (612.0, 792.0)  # points
MARGIN = 18.0            # 1/4 inch
IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif")


def is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def _sheet(w: float, h: float) -> tuple[float, float]:
    return (LETTER[1], LETTER[0]) if w > h else LETTER


def _fit(w: float, h: float) -> tuple[float, float, float, float, float]:
    """(page width, page height, scale, x, y) to centre a w x h box on a Letter sheet."""
    pw, ph = _sheet(w, h)
    s = min((pw - 2 * MARGIN) / w, (ph - 2 * MARGIN) / h)
    return pw, ph, s, (pw - w * s) / 2, (ph - h * s) / 2


def image_to_pdf(data: bytes, rotate: int = 0) -> bytes:
    from PIL import Image, ImageOps
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:  # noqa: BLE001  (HEIC support is optional)
        pass
    with Image.open(io.BytesIO(data)) as im:
        frames = []
        for i in range(getattr(im, "n_frames", 1)):
            im.seek(i)
            f = ImageOps.exif_transpose(im.copy())
            if rotate:
                f = f.rotate(-rotate, expand=True)  # PIL turns counter-clockwise
            frames.append(f.convert("RGB"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    for f in frames:
        pw, ph, s, x, y = _fit(*f.size)
        c.setPageSize((pw, ph))
        jpg = io.BytesIO()
        f.save(jpg, "JPEG", quality=88)
        jpg.seek(0)
        c.drawImage(ImageReader(jpg), x, y, f.size[0] * s, f.size[1] * s)
        c.showPage()
    c.save()
    return buf.getvalue()


def pdf_to_letter(data: bytes, rotate: int = 0) -> bytes:
    from pypdf import PageObject, PdfReader, PdfWriter, Transformation

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        reader.decrypt("")
    out = PdfWriter()
    for page in reader.pages:
        if rotate:
            page.rotate(rotate)
        page.transfer_rotation_to_content()
        box = page.mediabox
        w, h = float(box.width), float(box.height)
        if w <= 0 or h <= 0:
            continue
        if abs(w - LETTER[0]) < 2 and abs(h - LETTER[1]) < 2 and float(box.left) == 0 and float(box.bottom) == 0:
            out.add_page(page)  # already Letter and upright
            continue
        pw, ph, s, x, y = _fit(w, h)
        sheet = PageObject.create_blank_page(width=pw, height=ph)
        sheet.merge_transformed_page(
            page, Transformation().translate(-float(box.left), -float(box.bottom)).scale(s).translate(x, y))
        out.add_page(sheet)
    buf = io.BytesIO()
    out.write(buf)
    return buf.getvalue()


def to_letter_pdf(data: bytes, filename: str = "", rotate: int = 0) -> bytes | None:
    """The file as a Letter PDF, or None when it can't be converted (it's then kept as sent)."""
    rotate = int(rotate or 0) % 360
    try:
        if is_pdf(data):
            return pdf_to_letter(data, rotate)
        return image_to_pdf(data, rotate)
    except Exception as exc:  # noqa: BLE001  (a file we can't read stays as it came)
        log.warning("paper.convert_failed", filename=filename, error=str(exc)[:200])
        return None


def is_letter(data: bytes) -> bool:
    """Every page is 8.5 x 11 (either way round)."""
    if not is_pdf(data):
        return False
    try:
        from pypdf import PdfReader
        for p in PdfReader(io.BytesIO(data)).pages:
            w, h = sorted((float(p.mediabox.width), float(p.mediabox.height)))
            if abs(w - LETTER[0]) > 2 or abs(h - LETTER[1]) > 2:
                return False
        return True
    except Exception:  # noqa: BLE001
        return False
