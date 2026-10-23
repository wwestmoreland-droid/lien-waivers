"""Command line: `lw <command>`."""
from __future__ import annotations

import typer
from sqlalchemy import select

from app.ctc import report
from app.db.models import Payable, Project, Waiver
from app.db.session import session_scope
from app.logs import configure_logging

cli = typer.Typer(no_args_is_help=True, add_completion=False)


@cli.command("migrate")
def migrate() -> None:
    """Create or upgrade the database schema."""
    from alembic import command
    from alembic.config import Config

    from app.config import BASE_DIR, get_settings
    from app.web.security import production_problems

    problems = production_problems(get_settings())
    if any("DATABASE_URL" in p for p in problems):
        # Don't crash-loop: skip migrating so the app starts and its page says what to fix.
        typer.echo("Skipping migration: " + " ".join(p for p in problems if "DATABASE_URL" in p))
        return
    command.upgrade(Config(str(BASE_DIR / "alembic.ini")), "head")
    typer.echo("Database is up to date.")


@cli.command("recover-files")
def recover_files() -> None:
    """Rebuild or re-download PDFs and attachments missing from disk and the database."""
    from app.services.sync_runner import recover_files_now

    typer.echo(recover_files_now())


@cli.command("sync")
def sync() -> None:
    """Run one Smartsheet sync now (read-only)."""
    from app.services.sync_runner import sync_now

    configure_logging()
    run_id = sync_now(trigger="cli")
    typer.echo(f"Sync #{run_id} finished.")


@cli.command("track")
def track(numbers: list[str], actor: str = typer.Option("cli", help="Recorded in the audit log")) -> None:
    """Turn tracking on for one or more project numbers."""
    from app.services.projects import set_tracked

    with session_scope() as db:
        for n in numbers:
            p = db.scalar(select(Project).where(Project.number == n))
            if not p:
                typer.echo(f"{n}: not found (run a sync first)")
                continue
            set_tracked(db, p, True, f"cli:{actor}")
            typer.echo(f"{n}: tracked")


@cli.command("backfill")
def backfill(project: list[str] = typer.Option(None, help="Project number(s); default all tracked")) -> None:
    """Load history for tracked projects, compute CTC, and list waivers that appear to be missing.
    Creates no waivers and no emails."""
    from app.services.sync_runner import sync_now
    from app.waivers.service import expected_waiver_types

    configure_logging()
    with session_scope() as db:
        numbers = set(project or db.scalars(select(Project.number).where(Project.is_tracked.is_(True))))
        for p in db.scalars(select(Project).where(Project.number.in_(numbers))):
            p.backfilled_at = None
    run_id = sync_now(trigger="backfill", backfill_projects=numbers)
    typer.echo(f"Backfill sync #{run_id} finished.\n")

    with session_scope() as db:
        for number in sorted(numbers):
            rows = report.report_rows(db, number)
            typer.echo(f"== {number}")
            for r in rows:
                typer.echo(f"  {r.vendor[:40]:40} revised {r.revised_contract:>12,.2f}  billed {r.billed:>12,.2f}  "
                           f"CTC {r.ctc:>12,.2f}  {r.status}")
            # History loaded by a backfill has no waivers in the app, so for those rows the only
            # record of a past waiver is Smartsheet's "Waiver Created" column.
            missing = []
            for p in db.scalars(select(Payable).where(
                    Payable.project_number == number, Payable.match_status == "matched",
                    Payable.deleted_in_source.is_(False)).order_by(Payable.date_received)):
                have = {w.type for w in db.scalars(select(Waiver).where(Waiver.payable_id == p.id))}
                for t in expected_waiver_types(db, p):
                    if t.value not in have and not (p.waiver_created_source or "").strip():
                        missing.append((p, t))
            if missing:
                typer.echo(f"  Waivers that appear to be missing ({len(missing)}):")
                for p, t in missing:
                    typer.echo(f"    {t.label:22} {p.vendor.name[:34]:34} inv {p.invoice_no or '-':16} "
                               f"${p.amount:>12,.2f}  received {p.date_received}  paid {p.paid_date or '-'}")
            typer.echo("")


@cli.command("calibrate")
def calibrate(key: str = typer.Argument(None, help="Template key, e.g. default/conditional_final; omit for all"),
              out_dir: str = typer.Option(".", help="Where to write the PDFs")) -> None:
    """Render test fills with a coordinate grid so field positions can be tuned in registry.yaml."""
    from pathlib import Path

    from app.waivers.pdf import SAMPLE_VALUES, fill
    from app.waivers.registry import load_registry

    reg = load_registry()
    keys = [key] if key else list(reg.templates)
    for k in keys:
        if k not in reg.templates:
            raise typer.BadParameter(f"Unknown template {k}. Known: {', '.join(reg.templates)}")
        path = Path(out_dir) / f"calibrate-{k.replace('/', '-')}.pdf"
        path.write_bytes(fill(reg.templates[k], SAMPLE_VALUES, grid=True))
        typer.echo(f"Wrote {path}")


@cli.command("report")
def ctc_report(out: str = typer.Option("ctc.xlsx", help="Output .xlsx or .csv path")) -> None:
    """Write the CTC report for all tracked projects."""
    with session_scope() as db:
        rows = report.report_rows(db)
        if out.endswith(".csv"):
            open(out, "w").write(report.to_csv(rows))
        else:
            open(out, "wb").write(report.to_xlsx(rows))
    typer.echo(f"Wrote {out}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
