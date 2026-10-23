"""PDFs and attachments live in the database; the file on disk is a cache.

Code keeps passing file paths around (FileResponse needs one), but every write also goes to
the stored_files table, and every read first makes sure the file is on disk, restoring it
from the database if the container was replaced.
"""
from __future__ import annotations

from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import StoredFile

log = structlog.get_logger()


def key_for(path: str | Path) -> str:
    p = Path(path)
    try:
        return str(p.resolve().relative_to(get_settings().data_dir.resolve()))
    except ValueError:
        # Saved under an older DATA_DIR: keep the part after the data folder (waivers/12/x.pdf).
        parts = p.parts
        for anchor in ("waivers", "emails"):
            if anchor in parts:
                return str(Path(*parts[parts.index(anchor):]))
        return str(p)


def local_path(key: str) -> Path:
    return get_settings().data_dir / key


def write(db: Session, path: str | Path, data: bytes) -> Path:
    path = Path(path)
    key = key_for(path)
    row = db.scalar(select(StoredFile).where(StoredFile.key == key))
    if row is None:
        db.add(StoredFile(key=key, content=data, size=len(data)))
    else:
        row.content, row.size = data, len(data)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        log.warning("filestore.disk_write_failed", key=key)
    db.flush()
    return path


def has(db: Session, path: str | Path | None) -> bool:
    if not path:
        return False
    return Path(path).exists() or db.scalar(select(StoredFile.id).where(StoredFile.key == key_for(path))) is not None


def ensure(db: Session, path: str | Path | None) -> Path | None:
    """The file on disk, restored from the database if it went missing. None if it's gone."""
    if not path:
        return None
    p = Path(path)
    if p.exists():
        return p
    row = db.scalar(select(StoredFile).where(StoredFile.key == key_for(p)))
    if row is None:
        return None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(row.content)
        return p
    except OSError:
        # The old path isn't writable here (different DATA_DIR): use the current one.
        q = local_path(row.key)
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_bytes(row.content)
        return q


def read(db: Session, path: str | Path | None) -> bytes | None:
    p = ensure(db, path)
    return p.read_bytes() if p else None
