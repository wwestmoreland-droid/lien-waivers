from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.config import get_settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def money(v) -> str:
    if v is None or v == "":
        return ""
    v = Decimal(v)
    s = f"${abs(v):,.2f}"
    return f"-{s}" if v < 0 else s


def pct(v) -> str:
    if v is None:
        return ""
    return f"{Decimal(v) * 100:.1f}%"


def fdate(v) -> str:
    """Oct 22, 2026"""
    if not v:
        return ""
    if isinstance(v, datetime):
        v = v.astimezone(ZoneInfo(get_settings().timezone)) if v.tzinfo else v
        return v.strftime("%b %-d, %Y %-I:%M %p")
    if isinstance(v, date):
        return v.strftime("%b %-d, %Y")
    return str(v)


templates.env.filters.update(money=money, pct=pct, fdate=fdate)
