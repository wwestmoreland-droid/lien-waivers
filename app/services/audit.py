from sqlalchemy.orm import Session

from app.db.models import AuditLog


def audit(db: Session, actor: str, action: str, entity_type: str | None = None,
          entity_id=None, **details) -> AuditLog:
    entry = AuditLog(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        details=details or None,
    )
    db.add(entry)
    return entry
