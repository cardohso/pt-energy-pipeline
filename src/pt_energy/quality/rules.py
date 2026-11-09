"""Loading and validating ``quality_rules.yaml`` and ``known_issues.yaml``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from pt_energy.quality.results import CheckResult

DATASETS = [
    "omie_day_ahead_price",
    "eredes_national_consumption",
    "entsoe_actual_load",
    "entsoe_actual_generation",
    "entsoe_day_ahead_price",
]
_SEVERITY = {"enum": ["blocking", "warning"]}
_PER_DATASET = {
    "type": "object",
    "additionalProperties": False,
    "required": DATASETS,
    "properties": {d: _SEVERITY for d in DATASETS},
}
_NUM = {"type": "number", "minimum": 0}
_RATIO = {"type": "array", "minItems": 2, "maxItems": 2, "items": _NUM}


def _obj(required: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": required,
    }


RULES_SCHEMA: dict[str, Any] = _obj(
    {
        "window_days": {"type": "integer", "minimum": 1},
        "freshness": _obj(
            {"severity": _PER_DATASET, "price_publication_hour_utc": {"type": "integer"}}
        ),
        "completeness": _obj({"severity": _PER_DATASET, "series_count_min_ratio": _NUM}),
        "price_cross_check": _obj({"tolerance_eur_mwh": _NUM}),
        "demand_cross_check": _obj(
            {"daily_warn_pct": _NUM, "daily_block_pct": _NUM, "hourly_warn_pct": _NUM}
        ),
        "drift": _obj(
            {
                "baseline_days": {"type": "integer", "minimum": 2},
                "min_baseline_days": {"type": "integer", "minimum": 2},
                "load": _obj({"block_ratio": _RATIO, "warn_z": _NUM}),
                "generation": _obj({"block_ratio": _RATIO, "warn_z": _NUM}),
                "price": _obj({"warn_z": _NUM}),
            }
        ),
        "frozen": _obj(
            {
                "load_hours": {"type": "integer", "minimum": 2},
                "eredes_quarters": {"type": "integer", "minimum": 2},
            }
        ),
        "eredes_total_tolerance_kwh": _NUM,
        "rejected": _obj({"incomplete_hour_grace_days": {"type": "integer", "minimum": 0}}),
        "revisions": _obj({"warn_pct": _NUM, "lookback_days": {"type": "integer", "minimum": 1}}),
    }
)

KNOWN_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "dataset", "checks", "dates", "reason", "added"],
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "dataset": {"enum": DATASETS},
            "checks": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "dates": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
            },
            "reason": {"type": "string", "minLength": 1},
            "added": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        },
    },
}


class RulesError(ValueError):
    pass


def _validated(doc: Any, schema: dict[str, Any], label: str) -> Any:
    errors = [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in Draft202012Validator(schema).iter_errors(doc)
    ]
    if errors:
        raise RulesError(f"{label}: " + "; ".join(sorted(errors)))
    return doc


def load_rules(path: Path) -> dict[str, Any]:
    rules: dict[str, Any] = _validated(
        yaml.safe_load(path.read_text(encoding="utf-8")), RULES_SCHEMA, path.name
    )
    return rules


@dataclass(frozen=True)
class KnownIssue:
    id: str
    dataset: str
    checks: tuple[str, ...]
    dates: frozenset[str]
    reason: str


def load_known_issues(path: Path) -> list[KnownIssue]:
    if not path.exists():
        return []
    doc = _validated(
        yaml.safe_load(path.read_text(encoding="utf-8")) or [], KNOWN_SCHEMA, path.name
    )
    ids = [e["id"] for e in doc]
    if len(ids) != len(set(ids)):
        raise RulesError(f"{path.name}: duplicate ids")
    return [
        KnownIssue(e["id"], e["dataset"], tuple(e["checks"]), frozenset(e["dates"]), e["reason"])
        for e in doc
    ]


def apply_known_issues(results: list[CheckResult], known: list[KnownIssue]) -> None:
    """Move findings that are fully explained by a known issue from ``fail`` to ``known``.

    Only the listed days are excused: if a check also fails on a day nobody has looked at, it keeps
    failing and ``affected`` shrinks to the unexplained days.
    """
    for r in results:
        if r.status != "fail" or not r.affected:
            continue
        excused = {
            d for k in known if k.dataset == r.dataset and r.name in k.checks for d in k.dates
        }
        explained = [d for d in r.affected if d in excused]
        if not explained:
            continue
        r.known = explained
        r.affected = [d for d in r.affected if d not in excused]
        if not r.affected:
            r.status = "known"
