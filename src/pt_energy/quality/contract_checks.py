"""Pandera schemas generated from the contracts, run on every staging table.

Nothing here is written by hand per dataset: types, nullability, ranges, allowed values and the
primary key all come from ``contracts/<dataset>.yaml``. Pandera was chosen over Great Expectations
(heavy, needs a project scaffold) and Soda Core (its anomaly detection needs the paid Soda Cloud):
it validates a DataFrame in a few lines, reports every failing row at once, and the schema is
plain Python generated from our own YAML.

The primary key is checked per raw window. Windows can legitimately overlap (ENTSO-E prices cover
Central European days), and the model resolves overlaps; inside one window a repeat is a defect.
"""

from __future__ import annotations

import warnings
from typing import Any

import pandas as pd
import pandera.pandas as pa
from pandera.engines import pandas_engine as pe

from pt_energy.quality.results import CheckResult, failed, passed

UTC_TS: Any = pe.DateTime(tz="UTC", unit="us")  # type: ignore[call-arg]

CATEGORIES = {
    "contract_schema": "schema and types",
    "contract_nulls": "nulls",
    "contract_ranges": "allowed ranges",
    "contract_allowed_values": "allowed values",
    "contract_primary_key": "primary key unique",
}
_EXTRA_COLUMNS = {
    "raw_window": pa.Column(str, nullable=False),
    "raw_sha256": pa.Column(str, nullable=False),
    "fetched_at": pa.Column(UTC_TS, nullable=False),
}


def _dtype(field_type: str) -> Any:
    return {
        "timestamp_utc": UTC_TS,
        "number": "float64",
        "integer": "int64",
        "string": str,
        "boolean": bool,
        "date": pa.Date,
    }[field_type]


def schema_from_contract(contract: dict[str, Any]) -> pa.DataFrameSchema:
    columns: dict[str, pa.Column] = {}
    for f in contract["fields"]:
        checks: list[pa.Check] = []
        rng = f.get("range")
        if rng:
            checks.append(pa.Check.in_range(rng.get("min"), rng.get("max")))
        if f.get("allowed_values"):
            checks.append(pa.Check.isin(f["allowed_values"]))
        columns[f["name"]] = pa.Column(_dtype(f["type"]), nullable=f["nullable"], checks=checks)
    return pa.DataFrameSchema(
        {**columns, **_EXTRA_COLUMNS},
        unique=[*contract["primary_key"], "raw_window"],
        report_duplicates="all",
        strict=False,
        coerce=False,
    )


def _category(check: str) -> str:
    if check == "not_nullable":
        return "contract_nulls"
    if check.startswith(("in_range", "greater_than", "less_than")):
        return "contract_ranges"
    if check.startswith("isin"):
        return "contract_allowed_values"
    if "uniqueness" in check:
        return "contract_primary_key"
    return "contract_schema"  # dtype(...), column_in_dataframe, anything structural


def _normalise_timestamps(df: pd.DataFrame, contract: dict[str, Any]) -> pd.DataFrame:
    """Unify the in-memory resolution (ns, us) of timezone-aware columns; naive ones stay naive."""
    df = df.copy()
    names = [f["name"] for f in contract["fields"] if f["type"] == "timestamp_utc"] + ["fetched_at"]
    for name in names:
        if name in df.columns and isinstance(df[name].dtype, pd.DatetimeTZDtype):
            df[name] = df[name].astype("datetime64[us, UTC]")
    return df


def validate_dataset(contract: dict[str, Any], df: pd.DataFrame) -> list[CheckResult]:
    """One result per category, for one staging table."""
    dataset = contract["name"]
    schema = schema_from_contract(contract)
    frame = _normalise_timestamps(df, contract)
    findings: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    days: dict[str, set[str]] = {c: set() for c in CATEGORIES}
    try:
        with warnings.catch_warnings():
            # pandera 0.35 on pandas 3.1 uses a deprecated argument internally; not our code
            warnings.filterwarnings("ignore", message="The inplace keyword")
            schema.validate(frame, lazy=True)
    except pa.errors.SchemaErrors as exc:
        for row in exc.failure_cases.itertuples():
            category = _category(str(row.check))
            findings[category].append(f"{row.column}: {row.check} (value {row.failure_case})")
            index = getattr(row, "index", None)
            if index is not None and pd.notna(index) and "raw_window" in frame.columns:
                days[category].add(str(frame.loc[int(index), "raw_window"]))
    results: list[CheckResult] = []
    for name, label in CATEGORIES.items():
        if findings[name]:
            sample = "; ".join(sorted(set(findings[name]))[:3])
            results.append(
                failed(
                    name,
                    dataset,
                    "blocking",
                    f"{label}: {len(findings[name])} failing values, e.g. {sample}",
                    observed=f"{len(findings[name])} failing",
                    threshold="0",
                    affected=sorted(days[name]),
                )
            )
        else:
            results.append(passed(name, dataset, "blocking", f"{label}: ok", observed="0 failing"))
    return results
