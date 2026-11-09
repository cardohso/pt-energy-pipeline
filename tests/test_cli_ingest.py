"""CLI: backfill and daily commands, exit codes, messages."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import TOKEN, FakeWeb
from pt_energy.cli import app
from pt_energy.ingest import http as ingest_http

CONTRACTS = str(Path(__file__).parent.parent / "contracts")
runner = CliRunner()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, web: FakeWeb, tmp_path: Path) -> None:
    monkeypatch.setenv("ENTSOE_API_TOKEN", TOKEN)
    monkeypatch.setenv("PT_ENERGY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(ingest_http, "make_client", web.client)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.chdir(tmp_path)  # so a stray .env in the repo cannot leak in


def invoke(*args: str) -> object:
    return runner.invoke(app, [*args, "--contracts-dir", CONTRACTS])


def test_backfill_stores_files_and_prints_a_summary(tmp_path: Path) -> None:
    result = invoke(
        "backfill", "--start", "2026-11-03", "--end", "2026-11-05", "-d", "omie_day_ahead_price"
    )
    assert result.exit_code == 0, result.stdout  # type: ignore[attr-defined]
    assert "new=1" in result.stdout and "no_data=2" in result.stdout  # type: ignore[attr-defined]
    assert list((tmp_path / "data/raw/omie").rglob("*.txt"))


def test_backfill_rerun_reports_unchanged(tmp_path: Path) -> None:
    args = (
        "backfill",
        "--start",
        "2026-11-05",
        "--end",
        "2026-11-05",
        "-d",
        "omie_day_ahead_price",
    )
    invoke(*args)
    result = invoke(*args)
    assert "unchanged=1" in result.stdout  # type: ignore[attr-defined]


def test_backfill_exits_1_and_names_the_failing_window(web: FakeWeb) -> None:
    web.set("omie_day_ahead_price", "2026-11-04", status=500)
    result = invoke(
        "backfill", "--start", "2026-11-04", "--end", "2026-11-05", "-d", "omie_day_ahead_price"
    )
    assert result.exit_code == 1  # type: ignore[attr-defined]
    assert "FAILED" in result.stdout and "2026-11-04" in result.stdout  # type: ignore[attr-defined]
    assert "new=1" in result.stdout  # type: ignore[attr-defined]


def test_backfill_rejects_a_reversed_range() -> None:
    result = invoke("backfill", "--start", "2026-11-05", "--end", "2026-11-01")
    assert result.exit_code == 2  # type: ignore[attr-defined]


def test_missing_token_is_a_clear_error_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENTSOE_API_TOKEN")
    result = invoke("backfill", "--start", "2026-11-05", "--end", "2026-11-05")
    assert result.exit_code == 2  # type: ignore[attr-defined]
    assert "ENTSOE_API_TOKEN" in result.output  # type: ignore[attr-defined]


def test_daily_fetches_the_lookback_window(web: FakeWeb) -> None:
    result = invoke("daily", "--lookback", "2", "-d", "eredes_national_consumption")
    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert web.count("eredes_national_consumption") == 3  # today and two days back


def test_revision_is_printed(web: FakeWeb) -> None:
    args = (
        "backfill",
        "--start",
        "2026-11-05",
        "--end",
        "2026-11-05",
        "-d",
        "omie_day_ahead_price",
    )
    invoke(*args)
    web.set("omie_day_ahead_price", "2026-11-05", body=b"MARGINALPDBCPT;\n2026;11;05;1;1;1;\n*\n")
    result = invoke(*args)
    assert "REVISED" in result.stdout and "revised=1" in result.stdout  # type: ignore[attr-defined]
