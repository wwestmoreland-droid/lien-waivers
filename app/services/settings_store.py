"""Runtime settings a person can change from the Settings page (stored in app_settings)."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AppSetting
from app.services.audit import audit


def defaults() -> dict:
    s = get_settings()
    return {
        "sync_interval_minutes": s.sync_interval_minutes,
        "major_trade_threshold": s.major_trade_threshold,
        "default_reviewer_email": s.default_reviewer_email,
        "picker_statuses": [7, 8, 9],
    }


def get_setting(db: Session, key: str):
    row = db.get(AppSetting, key)
    if row is None:
        return defaults().get(key)
    return json.loads(row.value)


def set_setting(db: Session, key: str, value, actor: str) -> None:
    old = get_setting(db, key)
    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=json.dumps(value), updated_by=actor)
        db.add(row)
    else:
        row.value = json.dumps(value)
        row.updated_by = actor
    audit(db, actor, "setting.changed", "setting", key, old=old, new=value)
