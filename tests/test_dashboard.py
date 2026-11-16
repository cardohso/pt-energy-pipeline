"""Dashboard export and build command. The numbers on the page must trace to real raw files."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from pt_energy.cli import app
from pt_energy.dashboard.export import GROUPS, RENEWABLE, build_export
from test_publish_and_lineage import healthy, transform

runner = CliRunner()
NOW = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)


@pytest.fixture
def published(tmp_path: Path) -> Path:
    root, _web = healthy(tmp_path)
    result = transform(root)
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    return root


def test_every_headline_number_traces_to_raw_files_that_exist(published: Path) -> None:
    doc = build_export(published, now=NOW)
    assert {k["id"] for k in doc["kpis"]} == {
        "renewable_share_24h",
        "price_avg_24h",
        "demand_peak_7d",
    }
    for kpi in doc["kpis"]:
        assert kpi["value"] is not None and kpi["trace"], kpi["id"]
        for f in kpi["trace"]:
            raw = published / f["path"]
            assert raw.is_file(), f["path"]
            # the plain SHA-256 shown on the page is the real hash of the stored bytes
            assert hashlib.sha256(raw.read_bytes()).hexdigest() == f["response_sha256"]
            assert f["rows_used"] > 0 and f["contract_version"]


def test_the_renewable_share_equals_an_independent_recomputation(published: Path) -> None:
    doc = build_export(published, now=NOW)
    kpi = next(k for k in doc["kpis"] if k["id"] == "renewable_share_24h")
    con = duckdb.connect()
    marks = ",".join(f"'{c}'" for c in RENEWABLE)
    (share, rows) = con.execute(
        f"""select 100 * sum(generation_mw) filter (where psr_type in ({marks})) / sum(generation_mw),
                   count(*)
            from '{published}/serving/model/generation_by_source.parquet'
            where direction = 'generation'
              and epoch(interval_start_utc) between {kpi["from"]} and {kpi["to"]}"""
    ).fetchone()
    assert kpi["value"] == pytest.approx(round(share, 1))
    assert sum(f["rows_used"] for f in kpi["trace"]) == rows  # the trace covers exactly those rows


def test_generation_groups_keep_their_stack_order(published: Path) -> None:
    doc = build_export(published, now=NOW)
    assert [g["name"] for g in doc["generation"]["groups"]] == list(GROUPS)
    n = len(doc["generation"]["t"])
    assert all(len(g["values"]) == n for g in doc["generation"]["groups"])


def test_health_carries_the_gate_report_and_run_history(published: Path) -> None:
    doc = build_export(published, now=NOW)
    health = doc["health"]
    latest = json.loads((published / "reports" / "latest.json").read_text())
    assert health["batch_id"] == latest["batch_id"] and health["published"] is True
    assert len(health["checks"]) == latest["counts"]["checks"]
    assert health["history"] and set(health["last_fetch"]) >= {"omie_day_ahead_price"}
    assert {c["name"] for c in doc["credits"]} == {
        "ENTSO-E Transparency Platform",
        "OMIE",
        "E-REDES Open Data Portal",
    }


def test_the_export_holds_no_raw_payloads(published: Path) -> None:
    text = json.dumps(build_export(published, now=NOW))
    assert "<Publication_MarketDocument" not in text and "datahora" not in text
    assert len(text) < 200_000  # aggregates only


def test_dashboard_build_writes_page_and_data(published: Path, tmp_path: Path) -> None:
    out = tmp_path / "site"
    result = runner.invoke(
        app,
        [
            "dashboard",
            "build",
            "--data-dir",
            str(published),
            "--site-dir",
            "dashboard",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out / "index.html").is_file() and (out / "app.js").is_file()
    assert json.loads((out / "data" / "dashboard.json").read_text())["schema_version"] == 1
    assert not (out / ".gitkeep").exists()


def test_dashboard_build_refuses_when_nothing_is_published(tmp_path: Path) -> None:
    result = runner.invoke(app, ["dashboard", "build", "--data-dir", str(tmp_path / "empty")])
    assert result.exit_code == 2


def test_the_page_loads_nothing_from_other_hosts() -> None:
    """No CDN, no fonts, no trackers: the page reads one local file and nothing else."""
    import re

    for name in ("index.html", "app.js", "style.css"):
        text = (Path(__file__).parent.parent / "dashboard" / name).read_text(encoding="utf-8")
        urls = set(re.findall(r"https?://[^\s\"')]+", text))
        allowed = {
            "http://www.w3.org/2000/svg",  # SVG namespace, not a request
            "https://github.com/cardohso/pt-energy-pipeline",
        }
        assert urls <= allowed, (name, urls - allowed)
        assert '<script src="http' not in text and "@import" not in text
