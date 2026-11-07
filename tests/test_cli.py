from typer.testing import CliRunner

from pt_energy import __version__
from pt_energy.cli import app


def test_version_command_prints_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__
