"""Publishing gate in the CLI, failure reports, OpenLineage events, the generated diagram, and the
rules files. Uses the synthetic world; no network."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from conftest import FakeWeb
from pt_energy.cli import app
from pt_energy.lineage.graph import mermaid
from pt_energy.quality.results import CheckResult, GateReport
from pt_energy.quality.rules import (
    RulesError,
    apply_known_issues,
    load_known_issues,
    load_rules,
)
from pt_energy.transform.model import MODELS
from synth import World
from test_quality_gate import CONTRACTS, EREDES, GEN, NOW, PRICE, ingest_days

runner = CliRunner()
NOW_ARG = "2026-11-09T15:00"


def transform(root: Path, now: str = NOW_ARG) -> object:
    return runner.invoke(
        app, ["transform", "--data-dir", str(root), "--contracts-dir", str(CONTRACTS), "--now", now]
    )


def healthy(tmp_path: Path) -> tuple[Path, FakeWeb]:
    web = FakeWeb()
    web.world = World(as_of=NOW)
    root = tmp_path / "data"
    ingest_days(web, root, NOW.date())
    return root, web


def latest(root: Path) -> dict:
    return json.loads((root / "reports" / "latest.json").read_text())


def served(root: Path) -> bytes:
    return (root / "serving" / "model" / "day_ahead_price.parquet").read_bytes()


def spike_price(web: FakeWeb) -> None:
    body = web.world.payload(PRICE, "2026-11-03")
    web.set(
        PRICE, "2026-11-03",
        body=re.sub(rb"<price.amount>[^<]*</price.amount>", b"<price.amount>99999</price.amount>", body, count=1),
    )  # fmt: skip


# --- publishing -----------------------------------------------------------------------------


def test_a_healthy_run_publishes_and_says_so(tmp_path: Path) -> None:
    root, _ = healthy(tmp_path)
    result = transform(root)
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "published" in result.stdout and "NOT published" not in result.stdout  # type: ignore[attr-defined]
    assert (root / "serving" / "pt_energy.duckdb").exists() and not (
        root / "serving.build"
    ).exists()
    doc = latest(root)
    assert doc["ok"] and doc["published"] and doc["counts"]["blocking_failures"] == 0


def test_a_bad_batch_is_not_published_and_the_last_good_build_stays(tmp_path: Path) -> None:
    root, web = healthy(tmp_path)
    assert transform(root).exit_code == 0  # type: ignore[attr-defined]
    good = served(root)

    spike_price(web)
    ingest_days(web, root, NOW.date(), datasets=[PRICE])  # the bad window arrives as a revision
    result = transform(root)

    assert result.exit_code == 1  # type: ignore[attr-defined]
    assert "BLOCKING entsoe_day_ahead_price contract_ranges" in result.stdout  # type: ignore[attr-defined]
    assert "NOT published, previous build kept" in result.stdout  # type: ignore[attr-defined]
    assert latest(root)["previous_build"] is True
    assert served(root) == good  # byte for byte what was published before
    assert (root / "rejected" / "pt_energy.duckdb").exists()  # kept for inspection
    assert not (root / "serving.build").exists()

    doc = latest(root)
    assert doc["ok"] is False and doc["published"] is False
    names = {(b["dataset"], b["name"]) for b in doc["blocking"]}
    assert (PRICE, "contract_ranges") in names and ("day_ahead_price", "price_cross_check") in names
    assert "2026-11-03" in doc["affected_days"]  # the affected batch, for the alert
    assert doc["raw_state_sha256"] and doc["batch_id"] == "20261109T150000Z"


def test_a_payload_that_cannot_be_parsed_keeps_the_last_good_build(tmp_path: Path) -> None:
    root, web = healthy(tmp_path)
    assert transform(root).exit_code == 0  # type: ignore[attr-defined]
    good = served(root)
    body = web.world.payload("omie_day_ahead_price", "2026-11-04").decode().replace(";\n", ";0;\n")
    web.set("omie_day_ahead_price", "2026-11-04", body=body.encode())
    ingest_days(web, root, NOW.date(), datasets=["omie_day_ahead_price"])
    result = transform(root)
    assert result.exit_code == 1  # type: ignore[attr-defined]
    assert "BLOCKING omie_day_ahead_price parse_errors" in result.stdout  # type: ignore[attr-defined]
    assert served(root) == good
    assert "2026-11-04" in latest(root)["affected_days"]


def test_the_next_good_run_publishes_again(tmp_path: Path) -> None:
    root, web = healthy(tmp_path)
    transform(root)
    spike_price(web)
    ingest_days(web, root, NOW.date(), datasets=[PRICE])
    assert transform(root).exit_code == 1  # type: ignore[attr-defined]
    before = served(root)

    web._overrides.clear()  # the publisher fixes its data
    ingest_days(web, root, NOW.date(), datasets=[PRICE])
    result = transform(root, "2026-11-09T16:00")
    assert result.exit_code == 0 and latest(root)["published"]  # type: ignore[attr-defined]
    assert served(root) == before  # the fixed data gives back exactly the original good build
    assert latest(root)["batch_id"] == "20261109T160000Z"  # and it is the new batch that is live


def test_warnings_publish_and_are_listed(tmp_path: Path) -> None:
    root, web = healthy(tmp_path)
    import json as _json

    doc = _json.loads(web.world.payload(EREDES, "2026-11-04"))
    doc["results"].insert(41, dict(doc["results"][40], total=1.0))
    doc["total_count"] += 1
    web.set(EREDES, "2026-11-04", body=_json.dumps(doc).encode())
    ingest_days(web, root, NOW.date(), datasets=[EREDES])
    result = transform(root)
    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "warning  eredes_national_consumption rejected_rows" in result.stdout  # type: ignore[attr-defined]
    assert latest(root)["published"] and latest(root)["counts"]["warnings"] >= 1


def test_known_issues_are_shown_but_do_not_fail(tmp_path: Path) -> None:
    root, web = healthy(tmp_path)
    import json as _json

    doc = _json.loads(web.world.payload(EREDES, "2026-11-01"))
    doc["results"] = doc["results"][:-3]
    doc["total_count"] = len(doc["results"])
    web.set(EREDES, "2026-11-01", body=_json.dumps(doc).encode())
    ingest_days(web, root, NOW.date(), datasets=[EREDES])
    result = transform(root)
    assert (
        result.exit_code == 0
        and "known    eredes_national_consumption completeness: 2026-11-01" in result.stdout
    )  # type: ignore[attr-defined]


def test_stale_data_blocks_when_the_run_is_late(tmp_path: Path) -> None:
    root, _ = healthy(tmp_path)
    result = transform(root, "2026-11-12T15:00")
    assert result.exit_code == 1 and "BLOCKING entsoe_actual_load freshness" in result.stdout  # type: ignore[attr-defined]
    assert not (root / "serving").exists()  # nothing was ever published
    assert "no earlier build exists yet" in result.stdout  # type: ignore[attr-defined]
    assert latest(root)["previous_build"] is False


# --- OpenLineage ----------------------------------------------------------------------------


def events(root: Path) -> list[dict]:
    (path,) = (root / "lineage" / "openlineage").glob("*.jsonl")
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_openlineage_events_have_the_required_shape(tmp_path: Path) -> None:
    root, _ = healthy(tmp_path)
    transform(root)
    evs = events(root)
    assert [(e["job"]["name"], e["eventType"]) for e in evs] == [
        ("transform", "START"), ("transform", "COMPLETE"),
        ("quality_gate", "START"), ("quality_gate", "COMPLETE"),
    ]  # fmt: skip
    for e in evs:
        assert {
            "eventType",
            "eventTime",
            "run",
            "job",
            "inputs",
            "outputs",
            "producer",
            "schemaURL",
        } <= set(e)
        assert re.fullmatch(r"[0-9a-f-]{36}", e["run"]["runId"]) and e["job"]["namespace"]
        assert e["eventTime"].endswith("Z") and e["schemaURL"].endswith("RunEvent")
    assert evs[0]["run"]["runId"] == evs[1]["run"]["runId"] != evs[2]["run"]["runId"]


def test_openlineage_names_the_raw_inputs_and_modelled_outputs(tmp_path: Path) -> None:
    root, _ = healthy(tmp_path)
    transform(root)
    done = events(root)[1]
    assert {i["name"] for i in done["inputs"]} == {
        "raw/omie/omie_day_ahead_price", "raw/eredes/eredes_national_consumption",
        "raw/entsoe/entsoe_actual_load", "raw/entsoe/entsoe_actual_generation",
        "raw/entsoe/entsoe_day_ahead_price",
    }  # fmt: skip
    assert {"model/demand", "model/day_ahead_price", "model/generation_by_source"} <= {
        o["name"] for o in done["outputs"]
    }
    version = done["inputs"][0]["facets"]["version"]["datasetVersion"]
    assert re.fullmatch(r"[0-9a-f]{64}", version)  # identifies exactly which raw files were used


def test_openlineage_carries_the_quality_checks_as_assertions(tmp_path: Path) -> None:
    root, _ = healthy(tmp_path)
    transform(root)
    gate = events(root)[3]
    facet = next(i for i in gate["inputs"] if i["name"] == "model/day_ahead_price")["inputFacets"][
        "dataQualityAssertions"
    ]
    assert facet["assertions"] and all(
        set(a) == {"assertion", "success"} for a in facet["assertions"]
    )
    assert all(a["success"] for a in facet["assertions"])
    assert [o["name"] for o in gate["outputs"]] == ["serving"]


def test_a_rejected_build_emits_a_fail_event_with_the_failing_assertion(tmp_path: Path) -> None:
    root, web = healthy(tmp_path)
    spike_price(web)
    ingest_days(web, root, NOW.date(), datasets=[PRICE])
    transform(root)
    gate = events(root)[3]
    assert gate["eventType"] == "FAIL" and gate["outputs"] == []
    facet = next(i for i in gate["inputs"] if i["name"] == "model/day_ahead_price")["inputFacets"][
        "dataQualityAssertions"
    ]
    failed = {a["assertion"] for a in facet["assertions"] if not a["success"]}
    assert "entsoe_day_ahead_price.contract_ranges" in failed


def test_openlineage_run_ids_are_stable_for_the_same_batch(tmp_path: Path) -> None:
    root, _ = healthy(tmp_path)
    transform(root)
    first = [e["run"]["runId"] for e in events(root)]
    transform(root)  # same raw files and same time: same batch id, same events
    assert [e["run"]["runId"] for e in events(root)] == first


# --- generated lineage diagram --------------------------------------------------------------


def test_the_committed_diagram_matches_the_pipeline() -> None:
    result = runner.invoke(
        app, ["lineage", "diagram", "--check", "--out", str(CONTRACTS.parent / "docs" / "lineage.md"),
              "--contracts-dir", str(CONTRACTS)]
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "lineage OK" in result.stdout


def test_a_stale_diagram_fails_the_check(tmp_path: Path) -> None:
    stale = tmp_path / "lineage.md"
    stale.write_text("```mermaid\nflowchart LR\n  a --> b\n```\n")
    result = runner.invoke(
        app,
        ["lineage", "diagram", "--check", "--out", str(stale), "--contracts-dir", str(CONTRACTS)],
    )
    assert result.exit_code == 1 and "out of date" in result.stdout


def test_the_diagram_is_derived_from_the_model_sql_not_drawn_by_hand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = mermaid(CONTRACTS)
    assert "stg_entsoe_actual_load --> model_demand" in text
    assert "stg_eredes_national_consumption --> model_demand" in text
    assert "stg_omie_day_ahead_price --> model_day_ahead_price" in text
    assert "model_extra" not in text
    monkeypatch.setitem(MODELS, "extra", "select * from stg_omie_day_ahead_price")
    assert "stg_omie_day_ahead_price --> model_extra" in mermaid(CONTRACTS)  # a new model appears


def test_every_dataset_and_every_contract_is_on_the_diagram() -> None:
    text = mermaid(CONTRACTS)
    for p in CONTRACTS.glob("*.yaml"):
        assert f"raw_{p.stem}" in text and f"stg_{p.stem}" in text and f"contract {p.stem}" in text


# --- rules and known-issue files ------------------------------------------------------------


def test_the_committed_rules_and_known_issues_are_valid() -> None:
    rules = load_rules(CONTRACTS / "quality" / "rules.yaml")
    assert rules["price_cross_check"]["tolerance_eur_mwh"] == 0.01
    assert rules["drift"]["price"] == {"warn_z": 8}
    issues = load_known_issues(CONTRACTS / "quality" / "known_issues.yaml")
    assert {i.id for i in issues} >= {"eredes-2026-10-25-null-timestamps"} and all(
        i.reason for i in issues
    )


def test_rules_with_an_unknown_key_or_missing_dataset_are_refused(tmp_path: Path) -> None:
    base = yaml.safe_load((CONTRACTS / "quality" / "rules.yaml").read_text())
    bad = {**base, "surprise": 1}
    (tmp_path / "r.yaml").write_text(yaml.safe_dump(bad))
    with pytest.raises(RulesError, match="surprise"):
        load_rules(tmp_path / "r.yaml")
    del base["freshness"]["severity"]["omie_day_ahead_price"]
    (tmp_path / "r.yaml").write_text(yaml.safe_dump(base))
    with pytest.raises(RulesError, match="omie_day_ahead_price"):
        load_rules(tmp_path / "r.yaml")


@pytest.mark.parametrize(
    "entry",
    [{"dates": ["yesterday"]}, {"dataset": "nope"}, {"checks": []}, {"reason": ""}, {"extra": 1}],
)
def test_malformed_known_issues_are_refused(tmp_path: Path, entry: dict) -> None:
    good = {
        "id": "x",
        "dataset": EREDES,
        "checks": ["completeness"],
        "dates": ["2026-11-01"],
        "reason": "r",
        "added": "2026-11-09",
    }
    (tmp_path / "k.yaml").write_text(yaml.safe_dump([{**good, **entry}]))
    with pytest.raises(RulesError):
        load_known_issues(tmp_path / "k.yaml")


def test_duplicate_known_issue_ids_are_refused(tmp_path: Path) -> None:
    good = {
        "id": "x",
        "dataset": EREDES,
        "checks": ["completeness"],
        "dates": ["2026-11-01"],
        "reason": "r",
        "added": "2026-11-09",
    }
    (tmp_path / "k.yaml").write_text(yaml.safe_dump([good, good]))
    with pytest.raises(RulesError, match="duplicate ids"):
        load_known_issues(tmp_path / "k.yaml")


def fail(dataset: str, name: str, affected: list[str]) -> CheckResult:
    return CheckResult(name, dataset, "warning", "fail", "m", affected=list(affected))


def known_issue_file(
    tmp_path: Path, dataset: str = EREDES, checks: list[str] | None = None
) -> list:
    entry = {
        "id": "k",
        "dataset": dataset,
        "checks": checks or ["completeness"],
        "dates": ["2026-11-01", "2026-11-02"],
        "reason": "r",
        "added": "2026-11-09",
    }
    (tmp_path / "k.yaml").write_text(yaml.safe_dump([entry]))
    return load_known_issues(tmp_path / "k.yaml")


def test_a_finding_wholly_explained_becomes_known(tmp_path: Path) -> None:
    r = fail(EREDES, "completeness", ["2026-11-01"])
    apply_known_issues([r], known_issue_file(tmp_path))
    assert (r.status, r.known, r.affected) == ("known", ["2026-11-01"], [])


def test_a_finding_partly_explained_keeps_failing_on_the_rest(tmp_path: Path) -> None:
    r = fail(EREDES, "completeness", ["2026-11-01", "2026-11-05"])
    apply_known_issues([r], known_issue_file(tmp_path))
    assert (r.status, r.known, r.affected) == ("fail", ["2026-11-01"], ["2026-11-05"])


@pytest.mark.parametrize("dataset, name", [(GEN, "completeness"), (EREDES, "rejected_rows")])
def test_a_known_issue_only_excuses_its_own_dataset_and_check(
    tmp_path: Path, dataset: str, name: str
) -> None:
    r = fail(dataset, name, ["2026-11-01"])
    apply_known_issues([r], known_issue_file(tmp_path))
    assert (r.status, r.known) == ("fail", [])


def test_blocking_and_issue_flags_on_results() -> None:
    block = CheckResult("a", "d", "blocking", "fail", "m")
    warn = CheckResult("b", "d", "warning", "fail", "m", opens_issue=True)
    ok = CheckResult("c", "d", "blocking", "pass", "m")
    known = CheckResult("d", "d", "blocking", "known", "m")
    report = GateReport("b", "s", "t", [block, warn, ok, known])
    assert report.blocking == [block] and report.warnings == [warn] and report.known == [known]
    assert report.issue_worthy == [block, warn] and not report.ok
    assert GateReport("b", "s", "t", [ok, known]).ok
