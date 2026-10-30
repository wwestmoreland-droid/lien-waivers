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


def _mail_banner() -> list[str]:
    """Mailboxes whose sign-in stopped working, for the red banner on every page."""
    from app.db.session import SessionLocal
    from app.mail.mailboxes import broken_mailboxes

    db = SessionLocal()
    try:
        return [m.email for m in broken_mailboxes(db)]
    except Exception:
        return []
    finally:
        db.close()


def _outbox_count() -> int:
    from sqlalchemy import func, select

    from app.db.models import EmailMessage
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        return db.scalar(select(func.count(EmailMessage.id)).where(
            EmailMessage.direction == "out", EmailMessage.status.in_(("draft", "queued", "failed")))) or 0
    except Exception:
        return 0
    finally:
        db.close()


def _today_count(user) -> int:
    """The number on the Today tab: things waiting for this person."""
    from app.db.session import SessionLocal
    from app.services.today import action_count

    db = SessionLocal()
    try:
        return action_count(db, user)
    except Exception:
        return 0
    finally:
        db.close()


def _kind_labels():
    from app.mail.outbound import KIND_LABELS
    return KIND_LABELS


from app.waivers.types import WaiverType  # noqa: E402

templates.env.globals.update(mail_banner=_mail_banner, outbox_count=_outbox_count, today_count=_today_count, KIND_LABELS=_kind_labels(),
                             WaiverType=WaiverType)
