"""The pipeline workflow can only run on GitHub, so its structure and its security properties are
tested here: schedule, manual inputs, permissions, secrets handling, step order, and that every
command it calls exists with the options it passes."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from pt_energy.cli import app

ROOT = Path(__file__).parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
PIPELINE = WORKFLOWS / "pipeline.yml"


def load(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text())
    doc["on"] = doc.pop(True, doc.get("on"))  # YAML reads the key `on` as boolean True
    return doc  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def wf() -> dict[str, Any]:
    return load(PIPELINE)


def steps(wf: dict[str, Any]) -> list[dict[str, Any]]:
    return wf["jobs"]["pipeline"]["steps"]  # type: ignore[no-any-return]


def step(wf: dict[str, Any], id_or_name: str) -> dict[str, Any]:
    return next(s for s in steps(wf) if s.get("id") == id_or_name or s.get("name") == id_or_name)


def index(wf: dict[str, Any], name: str) -> int:
    return steps(wf).index(step(wf, name))


# --- triggers -------------------------------------------------------------------------------


def test_it_runs_once_a_day_after_prices_and_e_redes_are_published(wf: dict[str, Any]) -> None:
    schedule = wf["on"]["schedule"]
    assert schedule == [
        {"cron": "30 15 * * *"}
    ]  # 15:30 UTC: after ~11:45 prices and ~14:35 E-REDES


def test_manual_runs_can_backfill_a_range_and_pick_datasets(wf: dict[str, Any]) -> None:
    inputs = wf["on"]["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["type"] == "choice" and inputs["mode"]["options"] == ["daily", "backfill"]
    assert {"lookback", "start", "end", "datasets"} <= set(inputs)
    assert inputs["lookback"]["default"] == "7"


def test_there_is_no_trigger_that_could_expose_secrets_to_a_pull_request(
    wf: dict[str, Any],
) -> None:
    assert set(wf["on"]) == {"schedule", "workflow_dispatch"}


# --- safety ---------------------------------------------------------------------------------


def test_permissions_are_the_minimum_needed(wf: dict[str, Any]) -> None:
    assert wf["permissions"] == {"contents": "read", "issues": "write"}


def test_only_one_run_at_a_time_so_pushes_to_the_data_repo_cannot_race(wf: dict[str, Any]) -> None:
    assert wf["concurrency"] == {"group": "pipeline", "cancel-in-progress": False}


def test_the_job_has_a_timeout_that_fits_a_backfill_but_not_a_hung_run(wf: dict[str, Any]) -> None:
    assert 30 <= wf["jobs"]["pipeline"]["timeout-minutes"] <= 120


def test_secrets_are_never_written_into_shell_scripts(wf: dict[str, Any]) -> None:
    for s in steps(wf):
        assert "secrets." not in s.get("run", ""), s.get("name")
    used = {m for s in steps(wf) for m in re.findall(r"secrets\.(\w+)", yaml.safe_dump(s))}
    assert used == {"DATA_DEPLOY_KEY", "ENTSOE_API_TOKEN"}


def test_the_deploy_key_is_only_used_to_check_out_the_data_repo(wf: dict[str, Any]) -> None:
    with_key = [s for s in steps(wf) if "ssh-key" in s.get("with", {})]
    assert len(with_key) == 1
    assert with_key[0]["with"]["path"] == "data" and with_key[0]["with"]["repository"].startswith(
        "${{ env.DATA_"
    )
    assert re.fullmatch(r"[\w.-]+/[\w.-]+", wf["env"]["DATA_REPOSITORY"])


def test_the_entsoe_token_reaches_only_the_fetch_step(wf: dict[str, Any]) -> None:
    holders = [s["id"] for s in steps(wf) if "ENTSOE_API_TOKEN" in s.get("env", {})]
    assert holders == ["ingest"]


def test_user_supplied_inputs_reach_the_shell_through_env_not_by_interpolation(
    wf: dict[str, Any],
) -> None:
    """Interpolating `${{ inputs.x }}` into a script lets an input run commands."""
    for s in steps(wf):
        assert "${{ inputs." not in s.get("run", ""), s.get("name")
        assert "${{ github.event" not in s.get("run", ""), s.get("name")


def test_every_action_is_pinned_to_a_version(wf: dict[str, Any]) -> None:
    for path in WORKFLOWS.glob("*.yml"):
        for use in re.findall(r"uses:\s*(\S+)", path.read_text()):
            assert re.search(r"@(v\d+|[0-9a-f]{40})$", use), f"{path.name}: {use}"


def test_the_public_ci_workflow_has_no_access_to_secrets_or_the_data_repo() -> None:
    text = (WORKFLOWS / "ci.yml").read_text()
    assert "secrets." not in text and "DATA_" not in text and "ENTSOE" not in text


# --- behaviour ------------------------------------------------------------------------------


def test_steps_run_in_the_right_order(wf: dict[str, Any]) -> None:
    order = ["Check out the data repository", "ingest", "transform", "Persist data",
             "Keep a refused build for inspection", "Open, refresh or close alert issues",
             "Fail the run if anything failed"]  # fmt: skip
    positions = [index(wf, name) for name in order]
    assert positions == sorted(positions)


def test_a_failed_window_or_a_blocked_build_does_not_stop_the_run(wf: dict[str, Any]) -> None:
    assert step(wf, "ingest")["continue-on-error"] is True
    assert step(wf, "transform")["continue-on-error"] is True


def test_data_alerts_and_artifacts_are_handled_even_when_earlier_steps_fail(
    wf: dict[str, Any],
) -> None:
    for name in (
        "Persist data",
        "Keep a refused build for inspection",
        "Summary",
        "Open, refresh or close alert issues",
    ):
        assert step(wf, name)["if"] == "always()", name


def test_the_run_is_marked_failed_when_ingest_or_the_gate_failed(wf: dict[str, Any]) -> None:
    cond = step(wf, "Fail the run if anything failed")["if"]
    assert (
        "steps.ingest.outcome == 'failure'" in cond
        and "steps.transform.outcome == 'failure'" in cond
    )


def test_a_refused_build_is_kept_as_an_artifact_for_two_weeks(wf: dict[str, Any]) -> None:
    s = step(wf, "Keep a refused build for inspection")
    assert s["with"]["name"] == "rejected-build" and s["with"]["path"] == "data/rejected"
    assert s["with"]["retention-days"] == 14 and s["with"]["if-no-files-found"] == "ignore"


def test_the_alert_step_ignores_reports_from_earlier_runs(wf: dict[str, Any]) -> None:
    s = step(wf, "Open, refresh or close alert issues")
    assert "steps.start.outputs.at" in s["env"]["SINCE"] and "--since" in s["run"]
    assert s["env"]["GH_TOKEN"] == "${{ github.token }}"


def test_backfill_and_daily_use_the_same_commands_as_the_command_line(wf: dict[str, Any]) -> None:
    run = step(wf, "ingest")["run"]
    assert "pt-energy backfill --start" in run and "pt-energy daily --lookback" in run


def test_the_data_directory_is_the_checked_out_data_repo(wf: dict[str, Any]) -> None:
    for name in ("ingest", "transform", "Open, refresh or close alert issues"):
        assert step(wf, name)["env"]["PT_ENERGY_DATA_DIR"] == "data"


def test_every_command_the_workflow_calls_exists_with_the_options_it_passes(
    wf: dict[str, Any],
) -> None:
    runner = CliRunner()
    ansi = re.compile(r"\x1b\[[0-9;]*m")  # CI colours the help text; the codes split option names
    for s in steps(wf):
        for cmd, rest in re.findall(r"pt-energy (\w+)([^\n]*)", s.get("run", "")):
            help_text = ansi.sub(
                "", runner.invoke(app, [cmd, "--help"], env={"COLUMNS": "200"}).stdout
            )
            assert help_text, f"unknown command {cmd}"
            for opt in re.findall(r"(--[\w-]+)", rest):
                assert opt in help_text, f"{cmd} has no option {opt}"


def test_the_persist_script_the_workflow_calls_exists_and_is_executable(wf: dict[str, Any]) -> None:
    run = step(wf, "Persist data")["run"]
    script = ROOT / re.search(r"(scripts/\S+\.sh)", run).group(1)  # type: ignore[union-attr]
    assert script.exists() and os.access(script, os.X_OK)


def test_the_installed_package_provides_the_command_name_the_workflow_uses() -> None:
    assert 'pt-energy = "pt_energy.cli:app"' in (ROOT / "pyproject.toml").read_text()


# --- the real linter ------------------------------------------------------------------------


def _actionlint() -> str | None:
    return shutil.which("actionlint") or (
        str(Path(sys.executable).parent / "actionlint")
        if (Path(sys.executable).parent / "actionlint").exists()
        else None
    )


@pytest.mark.skipif(_actionlint() is None, reason="actionlint not installed")
def test_actionlint_finds_nothing() -> None:
    result = subprocess.run(
        [_actionlint() or "", *map(str, WORKFLOWS.glob("*.yml"))], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
