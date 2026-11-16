"""Dashboard export: the published tables become one small JSON file the static page reads.

Everything the page shows comes from this file, and every headline number carries a ``trace``:
the exact raw files (content hash, window, fetch time, path in the data repository, contract
version) of the rows it was computed from. The trace is computed from the same rows as the number
(``raw_sha256`` on each model row joined to ``meta.lineage``), so it cannot drift from it.

The file holds aggregates (hourly values for the last days, three headline numbers) and the
quality report. It does not contain raw payloads, which stay in the private data repository.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

SCHEMA_VERSION = 1
WINDOW_DAYS = 7

# ENTSO-E production types (PSR) grouped for the chart. Pumped storage is shown on its own because
# it is not a primary source; the renewable share leaves it out of the numerator.
GROUPS: dict[str, tuple[str, ...]] = {
    "Wind": ("B18", "B19"),
    "Hydro": ("B11", "B12"),
    "Solar": ("B16",),
    "Gas": ("B04", "B05"),
    "Biomass and other": ("B01", "B20"),
    "Pumped storage": ("B10",),
}
RENEWABLE = ("B01", "B11", "B12", "B16", "B18", "B19")

CREDITS = [
    {
        "name": "ENTSO-E Transparency Platform",
        "use": "actual generation by type, actual load, day-ahead prices",
        "licence": "Terms of Use of the ENTSO-E Transparency Platform; shown here as hourly "
        "aggregates with attribution. Raw files are not republished.",
        "url": "https://transparency.entsoe.eu/",
    },
    {
        "name": "OMIE",
        "use": "day-ahead market prices for Portugal",
        "licence": "Source: OMIE (omie.es); values shown with attribution.",
        "url": "https://www.omie.es/",
    },
    {
        "name": "E-REDES Open Data Portal",
        "use": "national distribution-grid consumption (cross-check)",
        "licence": "CC BY 4.0",
        "url": "https://e-redes.opendatasoft.com/",
    },
]


@dataclass(frozen=True)
class Paths:
    model: Path
    meta: Path
    reports: Path

    @classmethod
    def of(cls, data_dir: Path) -> Paths:
        return cls(
            data_dir / "serving" / "model", data_dir / "serving" / "meta", data_dir / "reports"
        )


def _q(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None
) -> list[tuple[Any, ...]]:
    return con.execute(sql, params or []).fetchall()


def _trace(
    con: duckdb.DuckDBPyConnection, p: Paths, table: Path, where: str, dataset: str
) -> list[dict[str, Any]]:
    """Raw files behind the rows of ``table`` (alias ``m``) matching ``where``, one per file."""
    lineage = p.meta / "lineage.parquet"
    rows = _q(
        con,
        f"""select l.dataset, l.raw_window, l.content_sha256, l.response_sha256,
                   cast(epoch(l.fetched_at) as bigint), l.path, l.contract_version,
                   l.pipeline_version, count(*) as n
            from '{table}' m join '{lineage}' l on m.raw_sha256 = l.content_sha256
            where {where} and l.dataset = ?
            group by all order by 1, 2, 3""",
        [dataset],
    )
    return [
        {
            "dataset": d,
            "window": w,
            "content_sha256": sha,
            "response_sha256": resp,
            "fetched_at": fetched,
            "path": path,
            "contract_version": contract,
            "pipeline_version": pipeline,
            "rows_used": n,
        }
        for d, w, sha, resp, fetched, path, contract, pipeline, n in rows
    ]


def _hourly_generation(
    con: duckdb.DuckDBPyConnection, p: Paths, start: int, end: int
) -> dict[str, Any]:
    hours = [
        r[0]
        for r in _q(
            con,
            f"""select distinct cast(epoch(interval_start_utc) as bigint) t
                from '{p.model / "generation_by_source.parquet"}'
                where epoch(interval_start_utc) between ? and ? order by 1""",
            [start, end],
        )
    ]
    groups: list[dict[str, Any]] = []
    for name, codes in GROUPS.items():
        marks = ",".join("?" for _ in codes)
        by_hour = dict(
            _q(
                con,
                f"""select cast(epoch(interval_start_utc) as bigint), sum(generation_mw)
                    from '{p.model / "generation_by_source.parquet"}'
                    where direction = 'generation' and psr_type in ({marks})
                      and epoch(interval_start_utc) >= ? and epoch(interval_start_utc) <= ?
                    group by 1""",
                [*codes, start, end],
            )
        )
        values = [None if h not in by_hour else round(by_hour[h], 1) for h in hours]
        groups.append({"name": name, "values": values})  # a list: the stack order matters
    return {"t": hours, "groups": groups}


def _hourly_series(
    con: duckdb.DuckDBPyConnection, table: Path, value: str, source: str, start: int, end: int
) -> dict[str, Any]:
    rows = _q(
        con,
        f"""select cast(floor(epoch(interval_start_utc) / 3600) * 3600 as bigint) h, avg({value})
            from '{table}' where source = ?
              and epoch(interval_start_utc) >= ? and epoch(interval_start_utc) <= ?
            group by 1 order by 1""",
        [source, start, end],
    )
    return {"t": [r[0] for r in rows], "v": [round(r[1], 2) for r in rows]}


def _kpis(
    con: duckdb.DuckDBPyConnection, p: Paths, end: int, week_start: int
) -> list[dict[str, Any]]:
    day_start = end - 23 * 3600
    gen = p.model / "generation_by_source.parquet"
    renewable_marks = ",".join("?" for _ in RENEWABLE)
    ((renew, total),) = _q(
        con,
        f"""select sum(generation_mw) filter (where psr_type in ({renewable_marks})),
                   sum(generation_mw)
            from '{gen}' where direction = 'generation'
              and epoch(interval_start_utc) between ? and ?""",
        [*RENEWABLE, day_start, end],
    )
    out: list[dict[str, Any]] = []
    window = f"epoch(m.interval_start_utc) between {day_start} and {end}"  # ints, ours
    window_15 = f"epoch(m.interval_start_utc) between {day_start} and {end + 3599}"
    out.append(
        {
            "id": "renewable_share_24h",
            "label": "Renewable share of generation, last 24 h",
            "value": None if not total else round(100 * renew / total, 1),
            "unit": "%",
            "formula": "sum of generation_mw for biomass, hydro (without pumped storage), solar "
            "and wind, divided by sum of generation_mw of all types, over the last 24 hourly "
            "intervals; table model.generation_by_source (direction = generation)",
            "from": day_start,
            "to": end,
            "trace": _trace(
                con, p, gen, window + " and m.direction = 'generation'", "entsoe_actual_generation"
            ),
        }
    )
    price = p.model / "day_ahead_price.parquet"
    ((avg_price,),) = _q(
        con,
        f"""select avg(price_eur_mwh) from '{price}' where source = 'omie'
            and epoch(interval_start_utc) between ? and ?""",
        [day_start, end],
    )
    out.append(
        {
            "id": "price_avg_24h",
            "label": "Average day-ahead price, same 24 h",
            "value": None if avg_price is None else round(avg_price, 2),
            "unit": "EUR/MWh",
            "formula": "mean of price_eur_mwh over the same 24 hours, OMIE series; "
            "table model.day_ahead_price (source = omie)",
            "from": day_start,
            "to": end,
            "trace": _trace(
                con, p, price, window_15 + " and m.source = 'omie'", "omie_day_ahead_price"
            ),
        }
    )
    demand = p.model / "demand.parquet"
    peak = _q(
        con,
        f"""select max(demand_mw), arg_max(cast(epoch(interval_start_utc) as bigint), demand_mw)
            from '{demand}' where source = 'entsoe' and epoch(interval_start_utc) >= ?""",
        [week_start],
    )[0]
    out.append(
        {
            "id": "demand_peak_7d",
            "label": "Peak demand, last 7 days",
            "value": None if peak[0] is None else round(peak[0], 0),
            "unit": "MW",
            "formula": "maximum of demand_mw over the last 7 days, ENTSO-E actual load; "
            "table model.demand (source = entsoe)",
            "from": week_start,
            "to": end,
            "at": peak[1],
            "trace": _trace(
                con,
                p,
                demand,
                f"epoch(m.interval_start_utc) >= {week_start} and m.source = 'entsoe'",
                "entsoe_actual_load",
            ),
        }
    )
    return out


def _health(con: duckdb.DuckDBPyConnection, p: Paths, now: datetime) -> dict[str, Any]:
    latest = json.loads((p.reports / "latest.json").read_text(encoding="utf-8"))
    history = []
    for path in sorted(p.reports.glob("2*.json"))[-30:]:
        doc = json.loads(path.read_text(encoding="utf-8"))
        history.append(
            {
                "batch_id": doc["batch_id"],
                "checked_at": doc["checked_at"],
                "published": doc["published"],
                "checks": doc["counts"]["checks"],
                "passed": doc["counts"]["passed"],
                "blocking": doc["counts"]["blocking_failures"],
                "warnings": doc["counts"]["warnings"],
            }
        )
    checks = [
        {
            "dataset": r["dataset"],
            "name": r["name"],
            "severity": r["severity"],
            "status": r["status"],
            "message": r["message"],
            "observed": r["observed"],
            "threshold": r["threshold"],
            "known": r["known"],
        }
        for r in latest["results"]
    ]
    newest = {
        d: t
        for d, t in _q(
            con,
            f"select dataset, cast(max(epoch(fetched_at)) as bigint) "
            f"from '{p.meta / 'lineage.parquet'}' group by 1",
        )
    }
    revisions = _q(
        con,
        f"""select dataset,
                   count(*) filter (where old_value is null),
                   count(*) filter (where old_value is not null and new_value is not null),
                   count(*) filter (where new_value is null)
            from '{p.meta / "revisions.parquet"}' group by 1 order by 1""",
    )
    rejected = _q(
        con,
        f"select dataset, reason, count(*) from '{p.meta / 'rejected.parquet'}' "
        "group by all order by 1, 2",
    )
    return {
        "batch_id": latest["batch_id"],
        "checked_at": latest["checked_at"],
        "ok": latest["ok"],
        "published": latest["published"],
        "counts": latest["counts"],
        "raw_state_sha256": latest["raw_state_sha256"],
        "checks": checks,
        "history": history,
        "last_fetch": newest,
        "revisions": [
            {"dataset": d, "added": a, "changed": c, "removed": r} for d, a, c, r in revisions
        ],
        "rejected": [{"dataset": d, "reason": reason, "rows": n} for d, reason, n in rejected],
        "generated_at": int(now.timestamp()),
    }


def build_export(data_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """The dashboard document for the build currently published in ``data_dir/serving``."""
    now = now or datetime.now(UTC)
    p = Paths.of(data_dir)
    con = duckdb.connect()
    (end,) = _q(
        con,
        f"select cast(max(epoch(interval_start_utc)) as bigint) "
        f"from '{p.model / 'generation_by_source.parquet'}'",
    )[0:1][0]
    week_start = end - WINDOW_DAYS * 24 * 3600 + 3600
    batch = _q(
        con,
        f"select batch_id, cast(epoch(built_at) as bigint), pipeline_version, raw_state_sha256 "
        f"from '{p.meta / 'batches.parquet'}' order by built_at desc limit 1",
    )[0]
    return {
        "schema_version": SCHEMA_VERSION,
        "batch": {
            "id": batch[0],
            "built_at": batch[1],
            "pipeline_version": batch[2],
            "raw_state_sha256": batch[3],
        },
        "window": {"from": week_start, "to": end},
        "generation": _hourly_generation(con, p, week_start, end),
        "demand": _hourly_series(
            con, p.model / "demand.parquet", "demand_mw", "entsoe", week_start, end
        ),
        "price": _hourly_series(
            con, p.model / "day_ahead_price.parquet", "price_eur_mwh", "omie", week_start, end
        ),
        "kpis": _kpis(con, p, end, week_start),
        "health": _health(con, p, now),
        "credits": CREDITS,
    }


def write_export(doc: dict[str, Any], out_dir: Path) -> Path:
    path = out_dir / "data" / "dashboard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    return path
