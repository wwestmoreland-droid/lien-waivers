import os
import socket
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from app.db.models import JobLock
from app.db.session import SessionLocal


class JobAlreadyRunning(RuntimeError):
    pass


@contextmanager
def job_lock(name: str, ttl_minutes: int = 30):
    """DB-backed lock. Works across processes and on SQLite or Postgres."""
    holder = f"{socket.gethostname()}:{os.getpid()}"
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        db.execute(delete(JobLock).where(JobLock.name == name, JobLock.expires_at < now))
        db.add(JobLock(name=name, holder=holder, expires_at=now + timedelta(minutes=ttl_minutes)))
        db.commit()
    except IntegrityError:
        db.rollback()
        db.close()
        raise JobAlreadyRunning(name)
    try:
        yield
    finally:
        db.execute(delete(JobLock).where(JobLock.name == name, JobLock.holder == holder))
        db.commit()
        db.close()
