from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ReviewItem
from app.services.settings_store import get_setting


def now() -> datetime:
    return datetime.now(timezone.utc)


def upsert_review(db: Session, kind: str, ref_type: str, ref_id: int, project_number: str | None,
                  message: str, active: bool, stats=None) -> None:
    item = db.scalar(select(ReviewItem).where(ReviewItem.kind == kind, ReviewItem.ref_type == ref_type,
                                              ReviewItem.ref_id == ref_id))
    if active:
        if item is None:
            db.add(ReviewItem(kind=kind, ref_type=ref_type, ref_id=ref_id, project_number=project_number,
                              message=message, assignee_email=get_setting(db, "default_reviewer_email")))
            if stats is not None:
                stats[f"review_{kind}_new"] += 1
        elif item.status == "resolved" and item.resolved_by == "system:sync":
            item.status = "open"
            item.resolved_by = None
            item.resolved_at = None
            item.message = message
        elif item.status == "open":
            item.message = message
    elif item is not None and item.status == "open":
        item.status = "resolved"
        item.resolved_by = "system:sync"
        item.resolved_at = now()
