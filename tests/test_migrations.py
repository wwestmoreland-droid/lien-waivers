"""The suppliers migration upgrades a copy of the current schema, data included."""
import os
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "9f3538579a9f"  # 0007, the schema in production before suppliers


def alembic(url: str, *args: str) -> None:
    env = dict(os.environ, DATABASE_URL=url, RUN_SCHEDULER="false")
    r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]


def test_suppliers_migration_on_existing_data(tmp_path):
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'copy.db'}"
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    eng = sa.create_engine(url)
    with eng.begin() as c:  # start clean (a Postgres test database may hold tables from other tests)
        md = sa.MetaData()
        md.reflect(c)
        md.drop_all(c)
        c.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
    alembic(url, "upgrade", BEFORE)
    with eng.begin() as c:
        now = "2026-10-30 12:00:00"
        c.execute(sa.text(f"insert into vendors (id, name, normalized_name, type, created_at) values (1, 'Crown', 'crown', 'sub', '{now}')"))
        c.execute(sa.text(f"insert into projects (id, number, source_sheet, sub_count, sub_total, is_tracked, ever_tracked, first_seen_at, deleted_in_source) "
                          f"values (1, 'P--IL-1', 'INTAKE', 1, 0, true, true, '{now}', false)"))
        c.execute(sa.text("insert into subcontracts (id, source_row_id, vendor_id, project_id, contractor_name_raw, deleted_in_source) "
                          "values (1, 11, 1, 1, 'Crown', false)"))
        c.execute(sa.text(f"insert into payables (id, source_row_id, match_status, entered_in_qb, is_retention_auto, first_seen_at, deleted_in_source) "
                          f"values (1, 101, 'matched', false, false, '{now}', false)"))
        for wid, t in ((1, "UNCONDITIONAL_PARTIAL"), (2, "CONDITIONAL_FINAL")):
            c.execute(sa.text(f"insert into waivers (id, payable_id, subcontract_id, type, through_date_is_fallback, status, pdf_version, created_at, reminder_count) "
                              f"values ({wid}, 1, 1, '{t}', true, 'sent', 1, '{now}', 0)"))
    alembic(url, "upgrade", "head")
    with eng.begin() as c:
        assert c.execute(sa.text("select count(*) from waivers")).scalar() == 2
        assert c.execute(sa.text("select supplier_list_reminders from subcontracts")).scalar() == 0
        c.execute(sa.text("insert into suppliers (id, subcontract_id, name, source, status, created_at) "
                          "values (1, 1, 'ABC Supply', 'manual', 'active', '2026-10-30 12:00:00')"))
        # A supplier's waiver for the same payable and type as the sub's own is allowed...
        c.execute(sa.text("insert into waivers (id, payable_id, subcontract_id, supplier_id, type, through_date_is_fallback, status, pdf_version, created_at, reminder_count) "
                          "values (3, 1, 1, 1, 'UNCONDITIONAL_PARTIAL', true, 'received', 0, '2026-10-30 12:00:00', 0)"))
    # ...but a second copy of the sub's own waiver still isn't.
    try:
        with eng.begin() as c:
            c.execute(sa.text("insert into waivers (id, payable_id, subcontract_id, type, through_date_is_fallback, status, pdf_version, created_at, reminder_count) "
                              "values (4, 1, 1, 'UNCONDITIONAL_PARTIAL', true, 'draft', 0, '2026-10-30 12:00:00', 0)"))
        raise AssertionError("duplicate sub waiver was allowed")
    except sa.exc.IntegrityError:
        pass
    with eng.begin() as c:  # going back means giving up supplier waivers (the old rule can't hold them)
        c.execute(sa.text("delete from waivers where supplier_id is not null"))
    alembic(url, "downgrade", BEFORE)
    eng.dispose()
