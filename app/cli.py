"""Command line: `lw <command>`."""
from __future__ import annotations

import typer
from sqlalchemy import select

from app.ctc import report
from app.db.models import Payable, Project
from app.db.session import session_scope
from app.logs import configure_logging

cli = typer.Typer(no_args_is_help=True, add_completion=False)


@cli.command("migrate")
def migrate() -> None:
    """Create or upgrade the database schema."""
    from alembic import command
    from alembic.config import Config

    from app.config import BASE_DIR

    command.upgrade(Config(str(BASE_DIR / "alembic.ini")), "head")
    typer.echo("Database is up to date.")


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
    """Load history for tracked projects, compute CTC, and list paid payables that look like they
    never got a waiver. Creates no emails."""
    from app.services.sync_runner import sync_now

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
            # Until Phase 2 holds the app's own waiver records, Smartsheet's "Waiver Created"
            # column is the only signal of what was sent before.
            missing = db.scalars(select(Payable).where(
                Payable.project_number == number, Payable.paid_date.is_not(None),
                Payable.match_status == "matched", Payable.deleted_in_source.is_(False),
                (Payable.waiver_created_source.is_(None)) | (Payable.waiver_created_source == ""),
            ).order_by(Payable.paid_date)).all()
            if missing:
                typer.echo(f"  Paid with no waiver recorded in Smartsheet ({len(missing)}):")
                for p in missing:
                    typer.echo(f"    {p.paid_date}  {p.vendor.name if p.vendor else p.vendor_parsed}  "
                               f"inv {p.invoice_no}  ${p.amount:,.2f}")
            typer.echo("")


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
