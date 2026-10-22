"""Fill flat waiver PDFs with a coordinate overlay (reportlab draws, pypdf merges)."""
from __future__ import annotations

import io
from decimal import Decimal

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import Color
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from app.waivers.registry import Template

FONT = "Helvetica"
FONT_SIZE = 10
MIN_FONT_SIZE = 6.5


def _fit(text: str, max_width: float, size: float = FONT_SIZE) -> float:
    while size > MIN_FONT_SIZE and stringWidth(text, FONT, size) > max_width:
        size -= 0.5
    return size


def _wrap(text: str, max_width: float, lines: int, size: float) -> list[str]:
    words, out, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if stringWidth(trial, FONT, size) <= max_width:
            cur = trial
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    if len(out) > lines:
        kept = out[:lines]
        more = " (continued on attached list)"
        while kept[-1] and stringWidth(kept[-1] + more, FONT, size) > max_width:
            kept[-1] = kept[-1].rsplit(" ", 1)[0] if " " in kept[-1] else kept[-1][:-1]
        kept[-1] += more
        return kept
    return out


def _format(value, spec: dict) -> str:
    if value is None:
        return ""
    if spec.get("format") == "number" and isinstance(value, (Decimal, int, float)):
        return f"{Decimal(value):,.2f}"
    return str(value)


def _overlay(template: Template, values: dict[str, object], grid: bool = False) -> bytes:
    page = PdfReader(str(template.file)).pages[0]
    w, h = float(page.mediabox.width), float(page.mediabox.height)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(w, h))
    if grid:
        _draw_grid(c, w, h)
    for name, spec in template.fields.items():
        text = _format(values.get(name), spec)
        if not text and not grid:
            continue
        if grid and not text:
            text = f"[{name}]"
        x, y, max_w = spec["x"], spec["y"], spec.get("max_width", 300)
        if spec.get("lines", 1) > 1:
            size = spec.get("size", 9)
            c.setFont(FONT, size)
            for i, line in enumerate(_wrap(text, max_w, spec["lines"], size)):
                c.drawString(x, y - i * spec.get("line_gap", 14), line)
        else:
            size = _fit(text, max_w, spec.get("size", FONT_SIZE))
            c.setFont(FONT, size)
            c.drawString(x, y, text)
        if grid:
            c.setStrokeColor(Color(1, 0, 0, alpha=0.6))
            c.rect(x, y - 2, max_w, 12 * spec.get("lines", 1), stroke=1, fill=0)
            c.setStrokeColor(Color(0, 0, 0))
    c.save()
    return buf.getvalue()


def _draw_grid(c: canvas.Canvas, w: float, h: float) -> None:
    c.setFont(FONT, 5)
    for x in range(0, int(w) + 1, 10):
        c.setStrokeColor(Color(0, 0.4, 1, alpha=0.35 if x % 50 == 0 else 0.12))
        c.line(x, 0, x, h)
        if x % 50 == 0:
            c.setFillColor(Color(0, 0.3, 0.9))
            c.drawString(x + 1, 2, str(x))
    for y in range(0, int(h) + 1, 10):
        c.setStrokeColor(Color(0, 0.4, 1, alpha=0.35 if y % 50 == 0 else 0.12))
        c.line(0, y, w, y)
        if y % 50 == 0:
            c.setFillColor(Color(0, 0.3, 0.9))
            c.drawString(2, y + 1, str(y))
    c.setFillColor(Color(0, 0, 0))
    c.setStrokeColor(Color(0, 0, 0))


def fill(template: Template, values: dict[str, object], grid: bool = False) -> bytes:
    base = PdfReader(str(template.file))
    overlay = PdfReader(io.BytesIO(_overlay(template, values, grid=grid)))
    writer = PdfWriter()
    page = base.pages[0]
    page.merge_page(overlay.pages[0])
    writer.add_page(page)
    for extra in base.pages[1:]:
        writer.add_page(extra)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


SAMPLE_VALUES = {
    "contracted_by": "Buildout Pros LLC",
    "project_name": "IMAGE Studios - Palatine, IL (P--IL-102125-1)",
    "project_address": "1234 N Rand Rd, Palatine, IL, 60074",
    "through_date": "Sep 30, 2026",
    "amount": Decimal("29647.80"),
    "exceptions": "Pending CO: Reroute line ($4,500.00); Invoice COR #2 on hold ($1,663.00); "
                  "Pending CO: Additional lighting controls ($649.53)",
    "date": "Oct 22, 2026",
}
