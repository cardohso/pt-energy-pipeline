"""Command line entry point. Subcommands are added in later steps."""

import typer

from pt_energy import __version__

app = typer.Typer(help="Portugal open-energy batch pipeline.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Keep subcommands explicit even while only one exists."""


@app.command()
def version() -> None:
    """Print the pipeline version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
