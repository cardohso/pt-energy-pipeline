"""Command line entry point."""

from __future__ import annotations

import os
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import typer

from pt_energy import __version__
from pt_energy.alerts import GhCli, apply_alerts, build_alerts, latest_ingest_report, latest_json
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
from pt_energy.lineage.graph import render as render_lineage
from pt_energy.lineage.openlineage import emit_openlineage
from pt_energy.quality.gate import run_gate, write_report
from pt_energy.settings import contract_versions, load_dotenv, pipeline_version
from pt_energy.transform.model import build, swap_directory
from pt_energy.transform.stage import stage

app = typer.Typer(help="Portugal open-energy batch pipeline.", no_args_is_help=True)
contracts_app = typer.Typer(help="Data contract checks.", no_args_is_help=True)
app.add_typer(contracts_app, name="contracts")
lineage_app = typer.Typer(help="Lineage diagram.", no_args_is_help=True)
app.add_typer(lineage_app, name="lineage")

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


@lineage_app.command("diagram")
def lineage_diagram(
    out: Path = typer.Option(Path("docs/lineage.md"), "--out", help="File to write or check."),
    contracts_dir: Path = ContractsDir,
    check: bool = typer.Option(False, "--check", help="Fail if the file is out of date."),
) -> None:
    """Generate the lineage diagram from the pipeline definition."""
    text = render_lineage(contracts_dir)
    if check:
        if not out.exists() or out.read_text(encoding="utf-8") != text:
            typer.echo(f"{out} is out of date: run `pt-energy lineage diagram`")
            raise typer.Exit(code=1)
        typer.echo("lineage OK")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {out}")


@app.command()
def alert(
    data_dir: Path | None = DataDir,
    run_url: str | None = typer.Option(None, help="Link to the workflow run, shown in the issue."),
    since: str | None = typer.Option(
        None, help="UTC time this run started (YYYY-MM-DDTHH:MM:SSZ); older reports are ignored."
    ),
    workflow_failed: bool = typer.Option(False, "--workflow-failed", help="The run itself failed."),
    repo: str | None = typer.Option(
        None, envvar="GITHUB_REPOSITORY", help="owner/name for issues."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the issues instead of touching GitHub."
    ),
) -> None:
    """Open, refresh or close GitHub issues from this run's quality and ingestion reports."""
    root = data_dir or Path(os.environ.get("PT_ENERGY_DATA_DIR", "data"))
    gate = latest_json(root / "reports" / "latest.json", since, "checked_at")
    ingest = latest_ingest_report(root / "raw", since)
    alerts = build_alerts(gate, ingest, run_url=run_url, workflow_failed=workflow_failed)
    if dry_run or not repo:
        for a in alerts:
            typer.echo(f"## {a.title} [{a.label}]\n{a.body}")
        typer.echo(f"{len(alerts)} alert(s); nothing sent" + ("" if dry_run else " (no --repo)"))
        return
    for line in apply_alerts(
        GhCli(repo), alerts, run_url=run_url, batch=gate["batch_id"] if gate else ""
    ):
        typer.echo(line)
    typer.echo(f"{len(alerts)} alert(s) raised")


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
def transform(
    data_dir: Path | None = DataDir,
    contracts_dir: Path = ContractsDir,
    now: datetime | None = typer.Option(
        None, formats=["%Y-%m-%dT%H:%M"], help="Judge freshness as of this UTC time (replays)."
    ),
) -> None:
    """Rebuild the tables from the raw layer, run the quality gate, publish only if it passes.

    The build goes to <data-dir>/serving.build. If any blocking check fails, nothing is replaced:
    the previous <data-dir>/serving stays published, the rejected build is kept in
    <data-dir>/rejected for inspection, a report is written to <data-dir>/reports, and the
    command exits 1.
    """
    root = data_dir or Path(os.environ.get("PT_ENERGY_DATA_DIR", "data"))
    moment = now.replace(tzinfo=UTC) if now else utc_now()
    staged = stage(RawStore(root))
    candidate = root / "serving.build"
    if candidate.exists():
        shutil.rmtree(candidate)
    counts = build(staged, candidate, now=moment)
    report = run_gate(candidate, contracts_dir=contracts_dir, now=moment)
    for r in report.blocking:
        typer.echo(f"BLOCKING {r.dataset} {r.name}: {r.message}")
    for r in report.warnings:
        typer.echo(f"warning  {r.dataset} {r.name}: {r.message}")
    for r in report.known:
        typer.echo(f"known    {r.dataset} {r.name}: {', '.join(r.known)}")
    report.previous_build = (root / "serving").exists()
    if report.ok:
        swap_directory(candidate, root / "serving")
        report.published = True
    else:
        rejected = root / "rejected"
        if rejected.exists():
            shutil.rmtree(rejected)
        candidate.rename(rejected)
    write_report(root / "reports", report)
    emit_openlineage(root, report, staged, counts)
    c = report.to_dict()["counts"]
    typer.echo(
        f"batch {report.batch_id}: {c['passed']}/{c['checks']} checks passed, "
        f"{c['blocking_failures']} blocking, {c['warnings']} warnings, {c['known']} known; "
        + (
            "published"
            if report.published
            else "NOT published, previous build kept"
            if report.previous_build
            else "NOT published (no earlier build exists yet)"
        )
    )
    if not report.ok:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
