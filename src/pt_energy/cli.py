"""Command line entry point."""

from __future__ import annotations

import os
import shutil
from datetime import date, datetime
from pathlib import Path

import typer

from pt_energy import __version__
from pt_energy.contracts import check_contracts, freeze
from pt_energy.ingest import http as ingest_http
from pt_energy.ingest.runner import (
    RunReport,
    days_between,
    lookback_days,
    run_ingest,
    utc_now,
)
from pt_energy.ingest.sources import ALL_DATASETS, build_sources
from pt_energy.ingest.store import RawStore
from pt_energy.settings import contract_versions, load_dotenv, pipeline_version
from pt_energy.transform.model import build, swap_directory
from pt_energy.transform.stage import stage

app = typer.Typer(help="Portugal open-energy batch pipeline.", no_args_is_help=True)
contracts_app = typer.Typer(help="Data contract checks.", no_args_is_help=True)
app.add_typer(contracts_app, name="contracts")

ContractsDir = typer.Option(Path("contracts"), "--contracts-dir", help="Contracts directory.")
DataDir = typer.Option(
    None, "--data-dir", envvar="PT_ENERGY_DATA_DIR", help="Where raw and processed data live."
)
Datasets = typer.Option(
    None, "--dataset", "-d", help=f"Dataset to fetch (repeatable). Default: all of {ALL_DATASETS}."
)


@app.callback()
def main() -> None:
    """Load .env (if present) before any command runs."""
    load_dotenv()


@app.command()
def version() -> None:
    """Print the pipeline version."""
    typer.echo(__version__)


@contracts_app.command("check")
def contracts_check(root: Path = ContractsDir) -> None:
    """Validate all contracts, their history and migrations. Exit 1 on any problem."""
    problems = check_contracts(root)
    for problem in problems:
        typer.echo(f"FAIL {problem}")
    if problems:
        raise typer.Exit(code=1)
    typer.echo("contracts OK")


@contracts_app.command("freeze")
def contracts_freeze(name: str, root: Path = ContractsDir) -> None:
    """Snapshot the current version of a contract into history and update LOCK.json."""
    typer.echo(f"froze {freeze(root, name)}")


def _print_report(report: RunReport) -> None:
    for r in report.results:
        if r.status in ("failed", "skipped"):
            typer.echo(f"{r.status.upper():8} {r.dataset} {r.window}: {r.message}")
        elif r.status == "revised":
            typer.echo(f"REVISED  {r.dataset} {r.window}: now {r.sha256}, was {r.supersedes}")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(report.counts.items())) or "nothing to do"
    typer.echo(f"run {report.run_id}: {summary}")


def _run(datasets: list[str], days_for: object, data_dir: Path | None, contracts_dir: Path) -> None:
    root = data_dir or Path(os.environ.get("PT_ENERGY_DATA_DIR", "data"))
    try:
        sources = build_sources(datasets, os.environ.get("ENTSOE_API_TOKEN"))
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    plan = [(s, days_for(s)) for s in sources]  # type: ignore[operator]
    with ingest_http.make_client() as client:
        report = run_ingest(
            plan,
            client=client,
            store=RawStore(root),
            pipeline_version=pipeline_version(),
            contract_versions=contract_versions(contracts_dir),
        )
    _print_report(report)
    if not report.ok:
        raise typer.Exit(code=1)


@app.command()
def daily(
    lookback: int = typer.Option(7, min=0, help="Refetch this many past days to catch revisions."),
    datasets: list[str] = Datasets,
    data_dir: Path | None = DataDir,
    contracts_dir: Path = ContractsDir,
) -> None:
    """Fetch the last days (and tomorrow's prices) into the raw layer."""
    today = utc_now().date()
    _run(datasets or [], lambda s: lookback_days(s, today, lookback), data_dir, contracts_dir)


@app.command()
def backfill(
    start: datetime = typer.Option(..., formats=["%Y-%m-%d"], help="First day, YYYY-MM-DD."),
    end: datetime = typer.Option(..., formats=["%Y-%m-%d"], help="Last day, YYYY-MM-DD, included."),
    datasets: list[str] = Datasets,
    data_dir: Path | None = DataDir,
    contracts_dir: Path = ContractsDir,
) -> None:
    """Fetch every day from START to END (both included). Safe to rerun: nothing is duplicated."""
    first: date = start.date()
    last: date = end.date()
    try:
        days = days_between(first, last)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _run(datasets or [], lambda _s: days, data_dir, contracts_dir)


@app.command()
def transform(data_dir: Path | None = DataDir) -> None:
    """Rebuild staging and modelled tables (Parquet + DuckDB) from the raw layer.

    Output goes to <data-dir>/serving. If any raw window cannot be parsed, nothing is replaced:
    the previous build stays in place and the command exits 1.
    """
    root = data_dir or Path(os.environ.get("PT_ENERGY_DATA_DIR", "data"))
    staged = stage(RawStore(root))
    if len(staged.errors):
        for e in staged.errors.itertuples():
            typer.echo(f"PARSE ERROR {e.dataset} {e.raw_window}: {e.message}")
        typer.echo("serving/ not replaced; fix the raw data or the parser", err=True)
        raise typer.Exit(code=1)
    candidate = root / "serving.build"
    if candidate.exists():
        shutil.rmtree(candidate)
    counts = build(staged, candidate)
    swap_directory(candidate, root / "serving")
    for name, n in counts.items():
        typer.echo(f"{name:45} {n:>8}")
    reasons = staged.rejected["reason"].value_counts().to_dict() if len(staged.rejected) else {}
    typer.echo(f"rejected rows by reason: {reasons or 'none'}")


if __name__ == "__main__":
    app()
