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


# TEST_DATABASE_URL=postgresql://... runs the whole suite on Postgres, like production.
PG_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def engine():
    if PG_URL:
        eng = create_engine(db_session._normalize_url(PG_URL), poolclass=StaticPool)
        Base.metadata.drop_all(eng)
    else:
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


@pytest.fixture(autouse=True)
def _no_real_smartsheet_writes():
    """Tests never reach the real Smartsheet; tests that check writes install a fake."""
    from app.services import sync_runner

    class Off:
        def get_row(self, row_id):
            raise RuntimeError("no Smartsheet in tests")

    sync_runner.set_writer_factory(lambda: Off())
    yield
    sync_runner.set_writer_factory(None)
