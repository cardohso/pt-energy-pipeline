"""Data contracts: loading, schema validation, change classification and history checks.

Layout under a contracts root:

    <root>/<name>.yaml                      current contract
    <root>/history/<name>/<version>.yaml    frozen snapshot of every released version
    <root>/history/LOCK.json                sha256 of every snapshot (tamper evidence)
    <root>/migrations/<name>_<from>_to_<to>.yaml   required for every breaking change
"""

from __future__ import annotations

import hashlib
import itertools
import json
import shutil
from enum import IntEnum
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

_SCHEMA_DIR = Path(__file__).parent / "contracts_schema"

Contract = dict[str, Any]


class Level(IntEnum):
    """How big a change is, in semantic-versioning terms."""

    NONE = 0
    PATCH = 1
    MINOR = 2
    MAJOR = 3


# Top-level keys whose change is breaking for consumers.
_BREAKING_KEYS = ("grain", "primary_key", "timezone", "resolution_minutes", "source_timezone")
# Top-level keys whose change is only documentation or operational metadata.
_PATCH_KEYS = ("owner", "description", "source", "freshness", "notes")
# Field attributes whose change breaks consumers.
_BREAKING_FIELD_ATTRS = ("type", "unit")


def parse_version(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in version.split("."))
    return major, minor, patch


def bump_level(old: str, new: str) -> Level:
    """Size of the bump from ``old`` to ``new``. Raises if ``new`` is not greater."""
    o, n = parse_version(old), parse_version(new)
    if n < o:
        raise ValueError(f"version went backwards: {old} -> {new}")
    if n == o:
        return Level.NONE
    if n[0] != o[0]:
        return Level.MAJOR
    if n[1] != o[1]:
        return Level.MINOR
    return Level.PATCH


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((_SCHEMA_DIR / name).read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def schema_errors(doc: Any, kind: str = "contract") -> list[str]:
    """Human-readable JSON Schema violations for a contract or migration document."""
    validator = _validator(f"{kind}.schema.json")
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    ]


def classify_change(old: Contract, new: Contract) -> Level:
    """Smallest version bump that honestly describes the change from ``old`` to ``new``.

    Breaking (major): field removed (a rename is a removal plus an addition), field type or unit
    changed, nullable false -> true, or a change to grain, primary key, time zone or resolution.
    Minor: field added, range / allowed values / nullability tightened.
    Patch: owner, description, source, freshness, notes.
    """
    level = Level.NONE

    for key in _BREAKING_KEYS:
        if old.get(key) != new.get(key):
            return Level.MAJOR
    for key in _PATCH_KEYS:
        if old.get(key) != new.get(key):
            level = max(level, Level.PATCH)

    old_fields = {f["name"]: f for f in old["fields"]}
    new_fields = {f["name"]: f for f in new["fields"]}
    if set(old_fields) - set(new_fields):
        return Level.MAJOR
    if set(new_fields) - set(old_fields):
        level = max(level, Level.MINOR)

    for name, o in old_fields.items():
        n = new_fields[name]
        if any(o.get(a) != n.get(a) for a in _BREAKING_FIELD_ATTRS):
            return Level.MAJOR
        if not o["nullable"] and n["nullable"]:
            return Level.MAJOR
        if o["nullable"] != n["nullable"] or any(
            o.get(a) != n.get(a) for a in ("range", "allowed_values")
        ):
            level = max(level, Level.MINOR)
        elif o["description"] != n["description"]:
            level = max(level, Level.PATCH)
    return level


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lock_path(root: Path) -> Path:
    return root / "history" / "LOCK.json"


def _read_lock(root: Path) -> dict[str, str]:
    path = _lock_path(root)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def freeze(root: Path, name: str) -> Path:
    """Snapshot the current contract into history and record its hash in the lock file."""
    current = root / f"{name}.yaml"
    version = load_yaml(current)["version"]
    snapshot = root / "history" / name / f"{version}.yaml"
    if snapshot.exists() and snapshot.read_bytes() != current.read_bytes():
        raise ValueError(f"{snapshot} already exists with different content: bump the version")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(current, snapshot)
    lock = _read_lock(root)
    lock[snapshot.relative_to(root).as_posix()] = _sha256(snapshot)
    _lock_path(root).write_text(json.dumps(dict(sorted(lock.items())), indent=2) + "\n")
    return snapshot


def _snapshots(root: Path, name: str) -> list[tuple[str, Path]]:
    folder = root / "history" / name
    found = [(p.stem, p) for p in folder.glob("*.yaml")] if folder.is_dir() else []
    return sorted(found, key=lambda item: parse_version(item[0]))


def migration_path(root: Path, name: str, old: str, new: str) -> Path:
    return root / "migrations" / f"{name}_{old}_to_{new}.yaml"


def _check_migration(root: Path, name: str, old: str, new: str) -> list[str]:
    path = migration_path(root, name, old, new)
    if not path.exists():
        return [f"{name}: breaking change {old} -> {new} needs a migration at {path.name}"]
    doc = load_yaml(path)
    errors = [f"{path.name}: {e}" for e in schema_errors(doc, "migration")]
    if not errors and (doc["contract"], doc["from_version"], doc["to_version"]) != (name, old, new):
        errors.append(f"{path.name}: contract/from_version/to_version do not match the file name")
    return errors


def _check_history(root: Path, name: str, current: Contract) -> list[str]:
    errors: list[str] = []
    snaps = _snapshots(root, name)
    lock = _read_lock(root)
    changelog = {e["version"]: e for e in current["changelog"]}

    if not snaps:
        return [f"{name}: no snapshot in history/ (run `pt-energy contracts freeze {name}`)"]
    for _, path in snaps:
        rel = path.relative_to(root).as_posix()
        if lock.get(rel) != _sha256(path):
            errors.append(f"{rel}: snapshot is missing from or does not match LOCK.json")
    if snaps[-1][0] != current["version"]:
        errors.append(f"{name}: current version {current['version']} is not frozen in history/")
    elif load_yaml(snaps[-1][1]) != current:
        errors.append(
            f"{name}: current contract differs from its frozen snapshot: bump the version"
        )

    for version, _ in snaps:
        if version not in changelog:
            errors.append(f"{name}: no changelog entry for released version {version}")

    loaded = [(v, load_yaml(p)) for v, p in snaps]
    for (old_v, old), (new_v, new) in itertools.pairwise(loaded):
        if parse_version(old_v)[0] == 0:
            continue  # draft contracts have no consumers yet
        needed = classify_change(old, new)
        actual = bump_level(old_v, new_v)
        if actual < needed:
            errors.append(
                f"{name}: {old_v} -> {new_v} is a {actual.name.lower()} bump "
                f"but the change is {needed.name.lower()}"
            )
        if needed == Level.MAJOR:
            errors += _check_migration(root, name, old_v, new_v)
        entry = changelog.get(new_v)
        if entry is not None and entry["breaking"] != (needed == Level.MAJOR):
            errors.append(f"{name}: changelog for {new_v} has the wrong 'breaking' flag")
    return errors


def _check_one(root: Path, path: Path) -> list[str]:
    doc = load_yaml(path)
    errors = [f"{path.name}: {e}" for e in schema_errors(doc)]
    if errors:
        return errors
    name = doc["name"]
    if path.stem != name:
        errors.append(f"{path.name}: file name must match contract name '{name}'")
    if (doc["status"] == "draft") != (parse_version(doc["version"])[0] == 0):
        errors.append(f"{name}: status 'draft' must go with version 0.x and 'active' with >= 1.0.0")
    field_names = [f["name"] for f in doc["fields"]]
    if len(set(field_names)) != len(field_names):
        errors.append(f"{name}: duplicate field names")
    for key in doc["primary_key"]:
        if key not in field_names:
            errors.append(f"{name}: primary_key '{key}' is not a field")
    if doc["changelog"][0]["version"] != doc["version"]:
        errors.append(f"{name}: first changelog entry must be the current version")
    return errors + _check_history(root, name, doc)


def check_contracts(root: Path) -> list[str]:
    """Validate every contract under ``root``. Returns problems; empty means all good."""
    paths = sorted(root.glob("*.yaml"))
    if not paths:
        return [f"no contracts found in {root}"]
    errors: list[str] = []
    for path in paths:
        errors += _check_one(root, path)
    return errors
