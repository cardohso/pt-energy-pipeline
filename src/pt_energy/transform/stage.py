"""Raw layer in, staging tables out. A pure function of the raw files: delete and rebuild.

Staging keeps one row per published interval per window, from that window's *current* raw version,
with the raw file's hash on every row. Rows the publisher got wrong are not repaired: they go to
``rejected`` with a reason. Windows that cannot be parsed go to ``errors`` and block the build.
Differences between successive versions of a window become ``revisions``.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import pandas as pd

from pt_energy.ingest.sources import (
    EntsoeActualGeneration,
    EntsoeActualLoad,
    EntsoeDayAheadPrice,
    EredesNationalConsumption,
    OmieDayAheadPrice,
)
from pt_energy.ingest.store import RawStore
from pt_energy.transform.parse import (
    ParseError,
    Row,
    parse_entsoe_generation,
    parse_entsoe_load,
    parse_entsoe_price,
    parse_eredes,
    parse_omie,
)

Parser = Callable[[bytes], tuple[list[Row], list[Row]]]
TS = "datetime64[ns, UTC]"


@dataclass(frozen=True)
class DatasetSpec:
    dataset: str
    source: str
    extension: str
    parser: Parser
    key: tuple[str, ...]  # columns that identify a row (within a window)
    values: tuple[str, ...]  # columns whose change counts as a revision
    schema: dict[str, str]  # parsed columns and pandas dtypes


SPECS: dict[str, DatasetSpec] = {
    s.dataset: s
    for s in (
        DatasetSpec(
            OmieDayAheadPrice.dataset,
            "omie",
            OmieDayAheadPrice.extension,
            parse_omie,
            ("interval_start_utc",),
            ("price_pt_eur_mwh",),
            {
                "interval_start_utc": TS,
                "price_pt_eur_mwh": "float64",
                "resolution_minutes": "int64",
            },
        ),
        DatasetSpec(
            EredesNationalConsumption.dataset,
            "eredes",
            EredesNationalConsumption.extension,
            parse_eredes,
            ("interval_start_utc",),
            (
                "low_voltage_kwh",
                "medium_voltage_kwh",
                "high_voltage_kwh",
                "very_high_voltage_kwh",
                "total_kwh",
            ),
            {
                "interval_start_utc": TS,
                "low_voltage_kwh": "float64",
                "medium_voltage_kwh": "float64",
                "high_voltage_kwh": "float64",
                "very_high_voltage_kwh": "float64",
                "total_kwh": "float64",
                "resolution_minutes": "int64",
            },
        ),
        DatasetSpec(
            EntsoeActualLoad.dataset,
            "entsoe",
            EntsoeActualLoad.extension,
            parse_entsoe_load,
            ("interval_start_utc",),
            ("load_mw",),
            {"interval_start_utc": TS, "load_mw": "float64", "resolution_minutes": "int64"},
        ),
        DatasetSpec(
            EntsoeActualGeneration.dataset,
            "entsoe",
            EntsoeActualGeneration.extension,
            parse_entsoe_generation,
            ("interval_start_utc", "psr_type", "direction"),
            ("generation_mw",),
            {
                "interval_start_utc": TS,
                "psr_type": "object",
                "direction": "object",
                "generation_mw": "float64",
                "resolution_minutes": "int64",
            },
        ),
        DatasetSpec(
            EntsoeDayAheadPrice.dataset,
            "entsoe",
            EntsoeDayAheadPrice.extension,
            parse_entsoe_price,
            ("interval_start_utc",),
            ("price_eur_mwh",),
            {"interval_start_utc": TS, "price_eur_mwh": "float64", "resolution_minutes": "int64"},
        ),
    )
}

PROVENANCE_SCHEMA = {"raw_window": "object", "raw_sha256": "object", "fetched_at": TS}
REJECTED_SCHEMA = {
    "dataset": "object",
    "raw_window": "object",
    "raw_sha256": "object",
    "reason": "object",
    "date": "object",
    "time": "object",
    "datahora": "object",
    "total_kwh": "float64",
}
ERRORS_SCHEMA = {
    "dataset": "object",
    "raw_window": "object",
    "raw_sha256": "object",
    "message": "object",
}
REVISIONS_SCHEMA = {
    "dataset": "object",
    "raw_window": "object",
    "key": "object",
    "field": "object",
    "old_value": "float64",
    "new_value": "float64",
    "old_sha256": "object",
    "new_sha256": "object",
    "detected_at": TS,
}
LINEAGE_SCHEMA = {
    "dataset": "object",
    "source": "object",
    "raw_window": "object",
    "content_sha256": "object",
    "response_sha256": "object",
    "path": "object",
    "fetched_at": TS,
    "pipeline_version": "object",
    "contract_version": "object",
    "is_current": "bool",
}


def frame(rows: list[dict[str, Any]], schema: dict[str, str]) -> pd.DataFrame:
    """DataFrame with exactly ``schema``'s columns and dtypes, even when ``rows`` is empty."""
    df = pd.DataFrame(rows, columns=list(schema))
    for column, dtype in schema.items():
        if dtype == TS:
            df[column] = pd.to_datetime(df[column], utc=True)
        else:
            df[column] = df[column].astype(cast(Any, dtype))
    return df


@dataclass
class Staged:
    tables: dict[str, pd.DataFrame]
    rejected: pd.DataFrame
    errors: pd.DataFrame
    revisions: pd.DataFrame
    lineage: pd.DataFrame
    raw_state_sha256: str


def _fetched_at(meta: dict[str, Any]) -> datetime:
    return datetime.strptime(meta["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _read_version(
    store: RawStore, spec: DatasetSpec, window: str, sha: str
) -> tuple[bytes, dict[str, Any]]:
    folder = store.window_dir(spec.source, spec.dataset, window)
    meta = json.loads((folder / f"{sha}.meta.json").read_text(encoding="utf-8"))
    return (folder / f"{sha}{spec.extension}").read_bytes(), meta


def _diff(
    spec: DatasetSpec, old: list[Row], new: list[Row]
) -> list[tuple[str, str, float | None, float | None]]:
    """(key, field, old value, new value) for every value that differs between two versions."""

    def index(rows: list[Row]) -> dict[tuple[Any, ...], Row]:
        return {tuple(r[c] for c in spec.key): r for r in rows}

    a, b = index(old), index(new)
    out: list[tuple[str, str, float | None, float | None]] = []
    for k in sorted(set(a) | set(b), key=str):
        label = json.dumps([x.isoformat() if isinstance(x, datetime) else x for x in k])
        for field in spec.values:
            before = a[k][field] if k in a else None
            after = b[k][field] if k in b else None
            if before != after:
                out.append((label, field, before, after))
    return out


def stage(store: RawStore) -> Staged:
    tables: dict[str, pd.DataFrame] = {}
    rejected: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    revisions: list[dict[str, Any]] = []
    lineage: list[dict[str, Any]] = []
    state: list[str] = []

    for spec in SPECS.values():
        rows: list[dict[str, Any]] = []
        for window in store.windows(spec.source, spec.dataset):
            current = store.current_sha(spec.source, spec.dataset, window)
            assert current is not None
            state.append(f"{spec.source}/{spec.dataset}/{window}/{current}")

            for meta in store.versions(spec.source, spec.dataset, window):
                lineage.append(
                    {
                        "dataset": spec.dataset,
                        "source": spec.source,
                        "raw_window": window,
                        "content_sha256": meta["content_sha256"],
                        "response_sha256": meta["response_sha256"],
                        "path": (
                            f"raw/{spec.source}/{spec.dataset}/{window}/"
                            f"{meta['content_sha256']}{spec.extension}"
                        ),
                        "fetched_at": _fetched_at(meta),
                        "pipeline_version": meta["pipeline_version"],
                        "contract_version": meta["contract_version"],
                        "is_current": meta["content_sha256"] == current,
                    }
                )

            parsed: dict[str, list[Row]] = {}
            for sha in store.history(spec.source, spec.dataset, window):
                body, meta = _read_version(store, spec, window, sha)
                try:
                    parsed[sha], bad = spec.parser(body)
                except ParseError as exc:
                    if sha == current:
                        errors.append(
                            {
                                "dataset": spec.dataset,
                                "raw_window": window,
                                "raw_sha256": sha,
                                "message": str(exc),
                            }
                        )
                    continue
                if sha == current:
                    fetched = _fetched_at(meta)
                    rows += [
                        {**r, "raw_window": window, "raw_sha256": sha, "fetched_at": fetched}
                        for r in parsed[sha]
                    ]
                    rejected += [
                        {**b, "dataset": spec.dataset, "raw_window": window, "raw_sha256": sha}
                        for b in bad
                    ]
            chain = [s for s in store.history(spec.source, spec.dataset, window) if s in parsed]
            for old_sha, new_sha in itertools.pairwise(chain):
                detected = _fetched_at(_read_version(store, spec, window, new_sha)[1])
                revisions += [
                    {
                        "dataset": spec.dataset,
                        "raw_window": window,
                        "key": key,
                        "field": field,
                        "old_value": before,
                        "new_value": after,
                        "old_sha256": old_sha,
                        "new_sha256": new_sha,
                        "detected_at": detected,
                    }
                    for key, field, before, after in _diff(spec, parsed[old_sha], parsed[new_sha])
                ]
        tables[spec.dataset] = frame(rows, {**spec.schema, **PROVENANCE_SCHEMA}).sort_values(
            [*spec.key, "raw_window"], kind="stable", ignore_index=True
        )

    raw_state = hashlib.sha256("\n".join(sorted(state)).encode()).hexdigest()
    return Staged(
        tables=tables,
        rejected=frame(rejected, REJECTED_SCHEMA),
        errors=frame(errors, ERRORS_SCHEMA),
        revisions=frame(revisions, REVISIONS_SCHEMA),
        lineage=frame(lineage, LINEAGE_SCHEMA),
        raw_state_sha256=raw_state,
    )
