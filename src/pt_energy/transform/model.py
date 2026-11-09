"""Staging in, modelled tables out, written as Parquet and a DuckDB file.

Modelled tables (all timestamps UTC, every row carries the hash of the raw file it came from):

- ``model.generation_by_source``  ENTSO-E actual generation per production type and direction
- ``model.demand``                ENTSO-E load and E-REDES consumption, both hourly, in MW
- ``model.day_ahead_price``       OMIE and ENTSO-E prices, in EUR/MWh

Where windows overlap (ENTSO-E prices cover Central European days, so two UTC-day requests both
contain the same intervals) the newest fetched value wins.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from pt_energy.settings import pipeline_version as current_pipeline_version
from pt_energy.transform.stage import REJECTED_SCHEMA, Staged, frame

LATEST = (
    "QUALIFY row_number() OVER (PARTITION BY {part} ORDER BY fetched_at DESC, raw_window DESC) = 1"
)

GENERATION = f"""
SELECT interval_start_utc, psr_type, direction, generation_mw, resolution_minutes,
       raw_window, raw_sha256
FROM stg_entsoe_actual_generation
{LATEST.format(part="interval_start_utc, psr_type, direction")}
ORDER BY interval_start_utc, psr_type, direction
"""

# E-REDES is 15-minute energy in kWh. An hour needs all four quarters; the sum in kWh over one
# hour is the average power in kW, and dividing by 1000 gives MW (same unit as ENTSO-E load).
# The sum is ordered: adding floating-point numbers in a different order gives a different last
# digit, and a parallel aggregate does not promise an order.
DEMAND = f"""
WITH entsoe AS (
  SELECT interval_start_utc, 'entsoe' AS source, load_mw AS demand_mw, resolution_minutes,
         raw_window, raw_sha256, fetched_at
  FROM stg_entsoe_actual_load
  {LATEST.format(part="interval_start_utc")}
), eredes_quarters AS (
  SELECT * FROM stg_eredes_national_consumption
  {LATEST.format(part="interval_start_utc")}
), eredes AS (
  SELECT date_trunc('hour', interval_start_utc) AS interval_start_utc, 'eredes' AS source,
         sum(total_kwh ORDER BY interval_start_utc) / 1000.0 AS demand_mw, 60 AS resolution_minutes,
         min(raw_window) AS raw_window, min(raw_sha256) AS raw_sha256,
         max(fetched_at) AS fetched_at
  FROM eredes_quarters
  GROUP BY 1
  HAVING count(*) = 4
)
SELECT interval_start_utc, source, demand_mw, resolution_minutes, raw_window, raw_sha256 FROM (
  SELECT * FROM entsoe UNION ALL SELECT * FROM eredes
)
ORDER BY interval_start_utc, source
"""

INCOMPLETE_EREDES_HOURS = f"""
WITH q AS (
  SELECT * FROM stg_eredes_national_consumption
  {LATEST.format(part="interval_start_utc")}
)
SELECT date_trunc('hour', interval_start_utc) AS hour, min(raw_window) AS raw_window,
       min(raw_sha256) AS raw_sha256, count(*) AS quarters
FROM q GROUP BY 1 HAVING count(*) < 4 ORDER BY 1
"""

PRICE = f"""
WITH omie AS (
  SELECT interval_start_utc, 'omie' AS source, price_pt_eur_mwh AS price_eur_mwh,
         resolution_minutes, raw_window, raw_sha256, fetched_at
  FROM stg_omie_day_ahead_price
  {LATEST.format(part="interval_start_utc")}
), entsoe AS (
  SELECT interval_start_utc, 'entsoe' AS source, price_eur_mwh, resolution_minutes,
         raw_window, raw_sha256, fetched_at
  FROM stg_entsoe_day_ahead_price
  {LATEST.format(part="interval_start_utc")}
)
SELECT interval_start_utc, source, price_eur_mwh, resolution_minutes, raw_window, raw_sha256 FROM (
  SELECT * FROM omie UNION ALL SELECT * FROM entsoe
)
ORDER BY interval_start_utc, source
"""

MODELS = {
    "generation_by_source": GENERATION,
    "demand": DEMAND,
    "day_ahead_price": PRICE,
}


def build(
    staged: Staged,
    out_dir: Path,
    *,
    now: datetime | None = None,
    pipeline_version: str | None = None,
) -> dict[str, int]:
    """Write staging, model and meta tables as Parquet plus ``pt_energy.duckdb`` into ``out_dir``.

    ``out_dir`` must not exist yet. Returns the row count of every table.
    """
    if out_dir.exists():
        raise FileExistsError(f"{out_dir} already exists")
    out_dir.mkdir(parents=True)
    built_at = (now or datetime.now(UTC)).replace(microsecond=0)
    con = duckdb.connect(str(out_dir / "pt_energy.duckdb"))
    con.execute("SET TimeZone = 'UTC'")
    try:
        for schema in ("staging", "model", "meta"):
            con.execute(f"CREATE SCHEMA {schema}")
        for dataset, df in staged.tables.items():
            con.register(f"stg_{dataset}", df)
            con.execute(f"CREATE TABLE staging.{dataset} AS SELECT * FROM stg_{dataset}")
        for name, sql in MODELS.items():
            con.execute(f"CREATE TABLE model.{name} AS {sql}")

        incomplete = con.execute(INCOMPLETE_EREDES_HOURS).df()
        extra = [
            {
                "dataset": "eredes_national_consumption",
                "raw_window": r.raw_window,
                "raw_sha256": r.raw_sha256,
                "reason": f"incomplete_hour ({r.quarters} of 4 quarters)",
                "date": None,
                "time": None,
                "datahora": str(r.hour),
                "total_kwh": None,
            }
            for r in incomplete.itertuples()
        ]
        rejected = pd.concat([staged.rejected, frame(extra, REJECTED_SCHEMA)], ignore_index=True)
        meta_frames = {
            "rejected": rejected,
            "errors": staged.errors,
            "revisions": staged.revisions,
            "lineage": staged.lineage,
            "batches": pd.DataFrame(
                {
                    "batch_id": [built_at.strftime("%Y%m%dT%H%M%SZ")],
                    "built_at": [pd.Timestamp(built_at)],
                    "pipeline_version": [pipeline_version or current_pipeline_version()],
                    "raw_state_sha256": [staged.raw_state_sha256],
                    "windows": [int(staged.lineage["is_current"].sum())],
                }
            ),
        }
        for name, df in meta_frames.items():
            con.register(f"meta_{name}", df)
            con.execute(f"CREATE TABLE meta.{name} AS SELECT * FROM meta_{name}")

        counts: dict[str, int] = {}
        for schema in ("staging", "model", "meta"):
            folder = out_dir / schema
            folder.mkdir()
            rows = con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = ? "
                "ORDER BY table_name",
                [schema],
            ).fetchall()
            for (table,) in rows:
                con.execute(f"COPY {schema}.{table} TO '{folder / table}.parquet' (FORMAT PARQUET)")
                counts[f"{schema}.{table}"] = con.execute(
                    f"SELECT count(*) FROM {schema}.{table}"
                ).fetchone()[0]  # type: ignore[index]
    finally:
        con.close()
    return counts


def swap_directory(new: Path, final: Path) -> None:
    """Replace ``final`` with ``new`` so readers see the old or the new build, never a mix."""
    old = final.with_name(final.name + ".old")
    if old.exists():
        shutil.rmtree(old)
    if final.exists():
        final.rename(old)
    new.rename(final)
    if old.exists():
        shutil.rmtree(old)
