"""Command line entry point. Subcommands are added in later steps."""

from pathlib import Path

import typer

from pt_energy import __version__
from pt_energy.contracts import check_contracts, freeze

app = typer.Typer(help="Portugal open-energy batch pipeline.", no_args_is_help=True)
contracts_app = typer.Typer(help="Data contract checks.", no_args_is_help=True)
app.add_typer(contracts_app, name="contracts")

ContractsDir = typer.Option(Path("contracts"), "--dir", help="Contracts directory.")


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


if __name__ == "__main__":
    app()
