import os
import tempfile

os.environ.setdefault("RUN_SCHEDULER", "false")
os.environ.setdefault("DEV_AUTH_EMAIL", "wwestmoreland@buildoutpros.com")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="lw-test-")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.db import session as db_session
from app.db.models import Base
from tests.fixtures.sheets import FakeSource


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db_session.set_engine(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    s = db_session.SessionLocal()
    yield s
    s.close()


@pytest.fixture
def source():
    return FakeSource()
