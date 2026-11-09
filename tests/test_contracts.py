"""Contract validation tests: real contracts pass, every planted bad change is caught."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from pt_energy.cli import app
from pt_energy.contracts import (
    Level,
    bump_level,
    check_contracts,
    classify_change,
    freeze,
    load_yaml,
    migration_path,
    schema_errors,
)

REPO_CONTRACTS = Path(__file__).parent.parent / "contracts"
NAME = "demo_price"


def base_contract(version: str = "1.0.0") -> dict[str, Any]:
    return {
        "name": NAME,
        "version": version,
        "status": "active",
        "owner": "tests",
        "description": "Demo dataset.",
        "source": {"publisher": "p", "url": "u", "licence": "l", "attribution": "a"},
        "grain": "one price per hour",
        "resolution_minutes": 60,
        "primary_key": ["ts"],
        "timezone": "UTC",
        "freshness": {"expected_lag_hours": 1, "max_age_hours": 6},
        "fields": [
            {"name": "ts", "type": "timestamp_utc", "nullable": False, "description": "Start."},
            {
                "name": "price",
                "type": "number",
                "unit": "EUR/MWh",
                "nullable": False,
                "description": "Price.",
                "range": {"min": -500, "max": 4000},
            },
        ],
        "changelog": [{"version": version, "date": "2026-01-01", "change": "x", "breaking": False}],
    }


def release(root: Path, contract: dict[str, Any]) -> None:
    """Write ``contract`` as the current version and freeze it, like a reviewed PR would."""
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{NAME}.yaml").write_text(yaml.safe_dump(contract, sort_keys=False))
    freeze(root, NAME)


def next_version(
    old: dict[str, Any], version: str, *, breaking: bool, mutate: Any = None
) -> dict[str, Any]:
    new = copy.deepcopy(old)
    new["version"] = version
    new["changelog"].insert(
        0, {"version": version, "date": "2026-02-01", "change": "y", "breaking": breaking}
    )
    if mutate:
        mutate(new)
    return new


def write_migration(root: Path, old: str, new: str, **overrides: Any) -> None:
    doc: dict[str, Any] = {
        "contract": NAME,
        "from_version": old,
        "to_version": new,
        "description": "d",
        "steps": [{"op": "cast_type", "description": "s"}],
    }
    doc.update(overrides)
    path = migration_path(root, NAME, old, new)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc))


def drop_price(c: dict[str, Any]) -> None:
    c["fields"] = [f for f in c["fields"] if f["name"] != "price"]


def add_nullable_field(c: dict[str, Any]) -> None:
    c["fields"].append({"name": "note", "type": "string", "nullable": True, "description": "n"})


def has(problems: list[str], text: str) -> bool:
    return any(text in p for p in problems)


# --- the real contracts ---------------------------------------------------------------------


def test_repo_contracts_all_pass() -> None:
    assert check_contracts(REPO_CONTRACTS) == []


def test_repo_has_one_contract_per_dataset() -> None:
    names = {p.stem for p in REPO_CONTRACTS.glob("*.yaml")}
    assert names == {
        "omie_day_ahead_price",
        "eredes_national_consumption",
        "entsoe_actual_generation",
        "entsoe_actual_load",
        "entsoe_day_ahead_price",
    }


def test_omie_hourly_to_quarter_hourly_is_a_breaking_change_with_a_migration() -> None:
    v1 = load_yaml(REPO_CONTRACTS / "history/omie_day_ahead_price/1.0.0.yaml")
    v2 = load_yaml(REPO_CONTRACTS / "history/omie_day_ahead_price/2.0.0.yaml")
    assert classify_change(v1, v2) is Level.MAJOR
    assert migration_path(REPO_CONTRACTS, "omie_day_ahead_price", "1.0.0", "2.0.0").exists()


def test_every_contract_licence_and_attribution_is_present() -> None:
    for path in REPO_CONTRACTS.glob("*.yaml"):
        src = load_yaml(path)["source"]
        assert src["licence"] and src["attribution"].startswith("Source:")


# --- classification rules -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (drop_price, Level.MAJOR),
        (lambda c: c["fields"][1].update(type="integer"), Level.MAJOR),
        (lambda c: c["fields"][1].update(unit="EUR/kWh"), Level.MAJOR),
        (lambda c: c["fields"][0].update(nullable=True), Level.MAJOR),
        (lambda c: c.update(grain="one price per day"), Level.MAJOR),
        (lambda c: c.update(primary_key=["ts", "price"]), Level.MAJOR),
        (lambda c: c.update(resolution_minutes=15), Level.MAJOR),
        (lambda c: c["fields"][1].update(name="price_eur"), Level.MAJOR),  # rename
        (add_nullable_field, Level.MINOR),
        (lambda c: c["fields"][1].update(range={"min": -600, "max": 4000}), Level.MINOR),
        (lambda c: c["fields"][1].update(nullable=False), Level.NONE),
        (lambda c: c["fields"][1].update(description="Clearing price."), Level.PATCH),
        (lambda c: c.update(owner="someone else"), Level.PATCH),
        (lambda c: c["freshness"].update(max_age_hours=12), Level.PATCH),
    ],
)
def test_classify_change(mutate: Any, expected: Level) -> None:
    old = base_contract()
    new = copy.deepcopy(old)
    mutate(new)
    assert classify_change(old, new) is expected


def test_bump_level() -> None:
    assert bump_level("1.2.3", "2.0.0") is Level.MAJOR
    assert bump_level("1.2.3", "1.3.0") is Level.MINOR
    assert bump_level("1.2.3", "1.2.4") is Level.PATCH
    assert bump_level("1.2.3", "1.2.3") is Level.NONE
    with pytest.raises(ValueError, match="backwards"):
        bump_level("1.2.3", "1.2.2")


# --- history checks: planted bad changes ----------------------------------------------------


def test_clean_history_with_additive_minor_bump_passes(tmp_path: Path) -> None:
    v1 = base_contract()
    release(tmp_path, v1)
    release(tmp_path, next_version(v1, "1.1.0", breaking=False, mutate=add_nullable_field))
    assert check_contracts(tmp_path) == []


def test_field_removed_without_major_bump_is_caught(tmp_path: Path) -> None:
    v1 = base_contract()
    release(tmp_path, v1)
    release(tmp_path, next_version(v1, "1.1.0", breaking=False, mutate=drop_price))
    assert has(check_contracts(tmp_path), "minor bump but the change is major")


def test_breaking_change_with_major_bump_but_no_migration_is_caught(tmp_path: Path) -> None:
    v1 = base_contract()
    release(tmp_path, v1)
    release(tmp_path, next_version(v1, "2.0.0", breaking=True, mutate=drop_price))
    assert has(check_contracts(tmp_path), "needs a migration")


def test_breaking_change_with_major_bump_and_migration_passes(tmp_path: Path) -> None:
    v1 = base_contract()
    release(tmp_path, v1)
    release(tmp_path, next_version(v1, "2.0.0", breaking=True, mutate=drop_price))
    write_migration(tmp_path, "1.0.0", "2.0.0")
    assert check_contracts(tmp_path) == []


def test_migration_that_does_not_match_its_file_name_is_caught(tmp_path: Path) -> None:
    v1 = base_contract()
    release(tmp_path, v1)
    release(tmp_path, next_version(v1, "2.0.0", breaking=True, mutate=drop_price))
    write_migration(tmp_path, "1.0.0", "2.0.0", to_version="3.0.0")
    assert has(check_contracts(tmp_path), "do not match the file name")


def test_migration_with_unknown_operation_is_caught(tmp_path: Path) -> None:
    v1 = base_contract()
    release(tmp_path, v1)
    release(tmp_path, next_version(v1, "2.0.0", breaking=True, mutate=drop_price))
    write_migration(tmp_path, "1.0.0", "2.0.0", steps=[{"op": "magic", "description": "s"}])
    assert has(check_contracts(tmp_path), "is not one of")


def test_breaking_change_not_flagged_in_changelog_is_caught(tmp_path: Path) -> None:
    v1 = base_contract()
    release(tmp_path, v1)
    release(tmp_path, next_version(v1, "2.0.0", breaking=False, mutate=drop_price))
    write_migration(tmp_path, "1.0.0", "2.0.0")
    assert has(check_contracts(tmp_path), "wrong 'breaking' flag")


def test_added_field_with_patch_bump_is_caught(tmp_path: Path) -> None:
    v1 = base_contract()
    release(tmp_path, v1)
    release(tmp_path, next_version(v1, "1.0.1", breaking=False, mutate=add_nullable_field))
    assert has(check_contracts(tmp_path), "patch bump but the change is minor")


def test_editing_a_frozen_snapshot_is_caught(tmp_path: Path) -> None:
    release(tmp_path, base_contract())
    snapshot = tmp_path / "history" / NAME / "1.0.0.yaml"
    snapshot.write_text(snapshot.read_text().replace("Demo dataset.", "Edited."))
    assert has(check_contracts(tmp_path), "does not match LOCK.json")


def test_editing_the_current_contract_without_a_new_version_is_caught(tmp_path: Path) -> None:
    release(tmp_path, base_contract())
    edited = base_contract()
    edited["description"] = "Edited."
    (tmp_path / f"{NAME}.yaml").write_text(yaml.safe_dump(edited, sort_keys=False))
    assert has(check_contracts(tmp_path), "differs from its frozen snapshot")


def test_freeze_refuses_to_overwrite_a_released_version(tmp_path: Path) -> None:
    release(tmp_path, base_contract())
    edited = base_contract()
    edited["description"] = "Edited."
    (tmp_path / f"{NAME}.yaml").write_text(yaml.safe_dump(edited, sort_keys=False))
    with pytest.raises(ValueError, match="bump the version"):
        freeze(tmp_path, NAME)


def test_unfrozen_new_version_is_caught(tmp_path: Path) -> None:
    v1 = base_contract()
    release(tmp_path, v1)
    v2 = next_version(v1, "1.1.0", breaking=False, mutate=add_nullable_field)
    (tmp_path / f"{NAME}.yaml").write_text(yaml.safe_dump(v2, sort_keys=False))
    assert has(check_contracts(tmp_path), "is not frozen in history")


def test_missing_changelog_entry_for_a_released_version_is_caught(tmp_path: Path) -> None:
    v1 = base_contract()
    release(tmp_path, v1)
    v2 = next_version(v1, "1.1.0", breaking=False, mutate=add_nullable_field)
    v2["changelog"] = [e for e in v2["changelog"] if e["version"] != "1.0.0"]
    release(tmp_path, v2)
    assert has(check_contracts(tmp_path), "no changelog entry for released version 1.0.0")


def test_lock_file_entry_removed_is_caught(tmp_path: Path) -> None:
    release(tmp_path, base_contract())
    (tmp_path / "history" / "LOCK.json").write_text(json.dumps({}))
    assert has(check_contracts(tmp_path), "missing from or does not match LOCK.json")


# --- single-contract rules ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: c["fields"][0].update(type="datetime"), "is not one of"),
        (lambda c: c.pop("owner"), "'owner' is a required property"),
        (lambda c: c.update(timezone="Europe/Lisbon"), "UTC"),
        (lambda c: c["fields"][1].pop("nullable"), "'nullable' is a required property"),
        (lambda c: c.update(unexpected=1), "Additional properties"),
        (lambda c: c.update(primary_key=["missing"]), "primary_key 'missing' is not a field"),
        (lambda c: c.update(status="draft"), "status 'draft' must go with version 0.x"),
        (lambda c: c["fields"].append(dict(c["fields"][0])), "duplicate field names"),
        (
            lambda c: c["changelog"][0].update(version="0.9.0"),
            "first changelog entry must be the current version",
        ),
    ],
)
def test_invalid_contract_is_caught(tmp_path: Path, mutate: Any, message: str) -> None:
    contract = base_contract()
    mutate(contract)
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / f"{NAME}.yaml").write_text(yaml.safe_dump(contract, sort_keys=False))
    assert has(check_contracts(tmp_path), message)


def test_contract_file_name_must_match_its_name(tmp_path: Path) -> None:
    (tmp_path / "other_name.yaml").write_text(yaml.safe_dump(base_contract(), sort_keys=False))
    assert has(check_contracts(tmp_path), "file name must match contract name")


def test_empty_contracts_directory_is_an_error(tmp_path: Path) -> None:
    assert has(check_contracts(tmp_path), "no contracts found")


def test_schema_errors_on_valid_contract_is_empty() -> None:
    assert schema_errors(base_contract()) == []


# --- CLI ------------------------------------------------------------------------------------


def test_cli_check_passes_on_repo_contracts() -> None:
    result = CliRunner().invoke(app, ["contracts", "check", "--contracts-dir", str(REPO_CONTRACTS)])
    assert result.exit_code == 0
    assert "contracts OK" in result.stdout


def test_cli_check_fails_with_exit_code_1_and_names_the_problem(tmp_path: Path) -> None:
    v1 = base_contract()
    release(tmp_path, v1)
    release(tmp_path, next_version(v1, "1.1.0", breaking=False, mutate=drop_price))
    result = CliRunner().invoke(app, ["contracts", "check", "--contracts-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "FAIL" in result.stdout
