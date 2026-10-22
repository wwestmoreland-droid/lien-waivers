"""Project picker: which jobs the app tracks."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Project, ReviewItem
from app.services.audit import audit
from app.services.settings_store import get_setting

CLOSED_STATUS_CODES = {10}


def picker_projects(db: Session, q: str | None = None, state: str | None = None,
                    client: str | None = None, pm: str | None = None) -> list[Project]:
    """Projects in status 7/8/9, plus any tracked project whatever its status
    (a tracked job that moved to Complete or Archived stays until its finals are in)."""
    codes = set(get_setting(db, "picker_statuses"))
    rows = db.scalars(select(Project).where(Project.deleted_in_source.is_(False)).order_by(Project.number)).all()
    out = [p for p in rows if p.is_tracked or (p.status_code in codes and p.source_sheet == "INTAKE")]
    if q:
        ql = q.lower()
        out = [p for p in out if ql in (p.number or "").lower() or ql in (p.name or "").lower()
               or ql in (p.client_company or "").lower()]
    if state:
        out = [p for p in out if (p.state or "") == state]
    if client:
        out = [p for p in out if (p.client_company or "") == client]
    if pm:
        out = [p for p in out if (p.pm_name or "") == pm]
    return out


def picker_label(p: Project) -> str:
    if p.is_tracked:
        if p.status_code in CLOSED_STATUS_CODES or p.source_sheet == "Archived":
            return "Tracked – job closed, untrack when finals are in"
        return "Tracked"
    return "Untracked" if p.ever_tracked else "New – not tracked"


def open_items_count(db: Session, project: Project) -> int:
    """Open waivers and review items that untracking would pause."""
    from app.db.models import Subcontract, Waiver
    from app.waivers.types import OPEN_STATUSES

    reviews = db.scalar(select(func.count(ReviewItem.id)).where(ReviewItem.project_number == project.number,
                                                                ReviewItem.status == "open")) or 0
    waivers = db.scalar(select(func.count(Waiver.id)).join(Subcontract).where(
        Subcontract.project_id == project.id, Waiver.status.in_([s.value for s in OPEN_STATUSES]))) or 0
    return reviews + waivers


def set_tracked(db: Session, project: Project, tracked: bool, actor: str) -> None:
    if project.is_tracked == tracked:
        return
    project.is_tracked = tracked
    if tracked:
        project.tracked_by = actor
        project.tracked_at = datetime.now(timezone.utc)
        project.ever_tracked = True
        # backfilled_at stays empty, so the next sync loads its history as a backfill (no emails).
    audit(db, actor, "project.tracked" if tracked else "project.untracked", "project", project.number,
          name=project.name, status=project.status)


def filter_options(projects: list[Project]) -> dict[str, list[str]]:
    return {
        "states": sorted({p.state for p in projects if p.state}),
        "clients": sorted({p.client_company for p in projects if p.client_company}),
        "pms": sorted({p.pm_name for p in projects if p.pm_name}),
    }

