"""Alerting: what opens an issue, what updates one, what closes it. A fake GitHub, no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pt_energy.alerts import (
    Alert,
    Issue,
    apply_alerts,
    build_alerts,
    latest_ingest_report,
    latest_json,
)
from pt_energy.cli import app

BLOCK = {
    "dataset": "entsoe_day_ahead_price", "name": "contract_ranges", "severity": "blocking",
    "status": "fail", "message": "allowed ranges: 1 failing values (value 99999.0)",
    "affected": ["2026-11-03"], "known": [], "opens_issue": False,
}  # fmt: skip
REVISION = {
    "dataset": "omie_day_ahead_price", "name": "revisions", "severity": "warning", "status": "fail",
    "message": "1 revised values", "affected": ["2026-11-08"], "known": [], "opens_issue": True,
}  # fmt: skip


def gate(
    blocking: list | None = None, issue_worthy: list | None = None, ok: bool | None = None
) -> dict:
    blocking = blocking or []
    return {
        "batch_id": "20261109T150000Z", "raw_state_sha256": "ab" * 32, "checked_at": "2026-11-09T15:00:00Z",
        "ok": not blocking if ok is None else ok, "published": not blocking,
        "counts": {"checks": 57, "passed": 56 - len(blocking), "blocking_failures": len(blocking), "warnings": 0, "known": 0},
        "affected_days": sorted({d for b in blocking for d in b["affected"]}),
        "blocking": blocking, "warnings": [], "known": [], "issue_worthy": issue_worthy or list(blocking), "results": [],
    }  # fmt: skip


def ingest(failed: int = 0, skipped: int = 0) -> dict:
    results = [
        {
            "dataset": "omie_day_ahead_price",
            "window": f"2026-11-0{i}",
            "status": "failed",
            "message": "HTTP 503 after 4 attempts",
        }
        for i in range(1, failed + 1)
    ]
    results += [
        {
            "dataset": "omie_day_ahead_price",
            "window": "2026-11-09",
            "status": "skipped",
            "message": "skipped",
        }
    ] * skipped
    results.append(
        {
            "dataset": "omie_day_ahead_price",
            "window": "2026-11-08",
            "status": "new",
            "message": None,
        }
    )
    return {
        "run_id": "20261109T153000Z",
        "started_at": "2026-11-09T15:30:00Z",
        "counts": {"failed": failed, "skipped": skipped, "new": 1},
        "results": results,
    }


class FakeIssues:
    def __init__(self) -> None:
        self.issues: dict[int, dict] = {}
        self.log: list[str] = []
        self._n = 0

    def ensure_label(self, name: str) -> None:
        self.log.append(f"label {name}")

    def find_open(self, label: str) -> list[Issue]:
        return [
            Issue(n, i["title"], i["body"])
            for n, i in self.issues.items()
            if label in i["labels"] and i["open"]
        ]

    def create(self, title: str, body: str, labels: list[str]) -> int:
        self._n += 1
        self.issues[self._n] = {
            "title": title,
            "body": body,
            "labels": labels,
            "open": True,
            "comments": [],
        }
        return self._n

    def edit_body(self, number: int, body: str) -> None:
        self.issues[number]["body"] = body

    def comment(self, number: int, text: str) -> None:
        self.issues[number]["comments"].append(text)

    def close(self, number: int, text: str) -> None:
        self.issues[number]["open"] = False
        self.issues[number]["comments"].append(text)


# --- what is raised -------------------------------------------------------------------------


def test_a_healthy_run_raises_nothing() -> None:
    assert build_alerts(gate(), ingest(), run_url="u") == []
    assert build_alerts(None, None) == []


def test_a_blocking_check_raises_an_issue_naming_the_check_the_days_and_the_batch() -> None:
    (alert,) = build_alerts(gate([BLOCK]), None, run_url="https://github.com/x/y/actions/runs/1")
    assert alert.kind == "blocking_check" and alert.label == "alert:blocking_check"
    for needle in (
        "entsoe_day_ahead_price",
        "contract_ranges",
        "2026-11-03",
        "20261109T150000Z",
        "previous build is still",
        "99999.0",
        "actions/runs/1",
        "rejected-build",
        "known_issues.yaml",
    ):
        assert needle in alert.body, needle


def test_failed_windows_raise_an_ingest_issue_listing_them() -> None:
    (alert,) = build_alerts(gate(), ingest(failed=2), run_url="u")
    assert alert.kind == "ingest_failure"
    assert (
        "2026-11-01" in alert.body
        and "HTTP 503 after 4 attempts" in alert.body
        and "retried by the next run" in alert.body
    )


def test_skipped_windows_count_as_an_ingest_problem() -> None:
    assert [a.kind for a in build_alerts(None, ingest(skipped=1))] == ["ingest_failure"]


def test_a_price_revision_raises_its_own_issue_even_though_it_only_warns() -> None:
    (alert,) = build_alerts(gate(issue_worthy=[REVISION], ok=True), None)
    assert (
        alert.kind == "price_revision"
        and "2026-11-08" in alert.body
        and "meta.revisions" in alert.body
    )


def test_a_blocking_revision_raises_both_issues() -> None:
    alerts = build_alerts(gate([BLOCK], issue_worthy=[BLOCK, REVISION]), None)
    assert [a.kind for a in alerts] == ["blocking_check", "price_revision"]


def test_a_crashed_run_with_no_report_raises_a_workflow_failure() -> None:
    (alert,) = build_alerts(None, None, run_url="https://x/runs/9", workflow_failed=True)
    assert alert.kind == "workflow_failure" and "https://x/runs/9" in alert.body


def test_a_workflow_failure_is_not_added_when_a_specific_alert_already_explains_it() -> None:
    kinds = [a.kind for a in build_alerts(gate([BLOCK]), None, workflow_failed=True)]
    assert kinds == ["blocking_check"]


def test_the_same_failure_gives_the_same_fingerprint_and_a_different_one_changes_it() -> None:
    a = build_alerts(gate([BLOCK]), None)[0]
    b = build_alerts(gate([BLOCK]), None)[0]
    other = build_alerts(gate([{**BLOCK, "affected": ["2026-11-04"]}]), None)[0]
    assert a.fingerprint == b.fingerprint != other.fingerprint


# --- opening, refreshing, closing -----------------------------------------------------------


def test_the_first_failure_opens_one_issue_with_labels() -> None:
    gh = FakeIssues()
    done = apply_alerts(gh, build_alerts(gate([BLOCK]), None), run_url="u")
    assert done == ["opened #1 (blocking_check)"]
    assert gh.issues[1]["labels"] == ["alert:blocking_check", "pipeline-alert"]


def test_the_same_failure_the_next_day_does_not_open_a_second_issue_or_comment() -> None:
    gh = FakeIssues()
    apply_alerts(gh, build_alerts(gate([BLOCK]), None))
    done = apply_alerts(gh, build_alerts(gate([BLOCK]), None))
    assert len(gh.issues) == 1 and gh.issues[1]["comments"] == []
    assert done == ["refreshed #1 (blocking_check: same failure)"]


def test_a_changed_failure_updates_the_issue_and_comments() -> None:
    gh = FakeIssues()
    apply_alerts(gh, build_alerts(gate([BLOCK]), None))
    worse = {**BLOCK, "name": "contract_nulls", "message": "nulls"}
    apply_alerts(gh, build_alerts(gate([BLOCK, worse]), None), run_url="https://x/runs/2")
    assert len(gh.issues) == 1
    assert "contract_nulls" in gh.issues[1]["body"]  # the body shows the latest state
    assert len(gh.issues[1]["comments"]) == 1 and "runs/2" in gh.issues[1]["comments"][0]


def test_recovery_comments_and_closes_the_issue() -> None:
    gh = FakeIssues()
    apply_alerts(gh, build_alerts(gate([BLOCK]), None))
    done = apply_alerts(gh, [], batch="20261110T153000Z")
    assert done == ["closed #1 (blocking_check: recovered)"]
    assert gh.issues[1]["open"] is False and "20261110T153000Z" in gh.issues[1]["comments"][0]


def test_a_new_failure_after_recovery_opens_a_new_issue() -> None:
    gh = FakeIssues()
    apply_alerts(gh, build_alerts(gate([BLOCK]), None))
    apply_alerts(gh, [])
    apply_alerts(gh, build_alerts(gate([BLOCK]), None))
    assert len(gh.issues) == 2 and gh.issues[2]["open"] and not gh.issues[1]["open"]


def test_kinds_are_independent() -> None:
    gh = FakeIssues()
    apply_alerts(gh, build_alerts(gate([BLOCK]), ingest(failed=1)))
    assert {i["labels"][0] for i in gh.issues.values()} == {
        "alert:blocking_check",
        "alert:ingest_failure",
    }
    apply_alerts(gh, build_alerts(gate([BLOCK]), None))  # ingestion recovered, gate still blocked
    status = {i["labels"][0]: i["open"] for i in gh.issues.values()}
    assert status == {"alert:blocking_check": True, "alert:ingest_failure": False}


def test_a_healthy_run_with_nothing_open_does_nothing() -> None:
    gh = FakeIssues()
    assert apply_alerts(gh, []) == [] and gh.issues == {}


# --- reading this run's reports -------------------------------------------------------------


def test_reports_left_over_from_an_earlier_run_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(gate([BLOCK])))
    assert latest_json(path, "2026-11-09T15:00:00Z", "checked_at") is not None
    assert latest_json(path, "2026-11-10T15:00:00Z", "checked_at") is None  # this run made none
    assert latest_json(tmp_path / "missing.json", None, "checked_at") is None


def test_the_newest_ingest_report_is_used(tmp_path: Path) -> None:
    runs = tmp_path / "_runs"
    runs.mkdir()
    (runs / "20261108T153000Z.json").write_text(
        json.dumps({**ingest(failed=3), "started_at": "2026-11-08T15:30:00Z"})
    )
    (runs / "20261109T153000Z.json").write_text(json.dumps(ingest()))
    assert latest_ingest_report(tmp_path, None)["counts"]["failed"] == 0
    assert latest_ingest_report(tmp_path, "2026-11-10T00:00:00Z") is None
    assert latest_ingest_report(tmp_path / "none", None) is None


# --- command line ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "latest.json").write_text(json.dumps(gate([BLOCK])))
    return tmp_path


def test_cli_dry_run_prints_the_issue_and_sends_nothing(data_dir: Path) -> None:
    result = CliRunner().invoke(
        app, ["alert", "--data-dir", str(data_dir), "--dry-run", "--run-url", "https://x/runs/1"]
    )
    assert (
        result.exit_code == 0
        and "[alert:blocking_check]" in result.stdout
        and "1 alert(s); nothing sent" in result.stdout
    )


def test_cli_without_a_repo_never_touches_github(data_dir: Path) -> None:
    result = CliRunner().invoke(
        app, ["alert", "--data-dir", str(data_dir)], env={"GITHUB_REPOSITORY": ""}
    )
    assert result.exit_code == 0 and "no --repo" in result.stdout


def test_cli_ignores_a_stale_report(data_dir: Path) -> None:
    result = CliRunner().invoke(
        app, ["alert", "--data-dir", str(data_dir), "--dry-run", "--since", "2026-11-10T00:00:00Z"]
    )
    assert "0 alert(s)" in result.stdout


def test_cli_sends_through_the_github_client(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeIssues()
    monkeypatch.setattr("pt_energy.cli.GhCli", lambda repo: fake)
    result = CliRunner().invoke(
        app, ["alert", "--data-dir", str(data_dir), "--repo", "cardohso/pt-energy-pipeline"]
    )
    assert (
        result.exit_code == 0
        and "opened #1 (blocking_check)" in result.stdout
        and len(fake.issues) == 1
    )


def test_alert_dataclass_label() -> None:
    assert Alert("price_revision", "t", "b", "f").label == "alert:price_revision"
