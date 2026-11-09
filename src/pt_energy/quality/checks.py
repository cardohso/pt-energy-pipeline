"""Checks on the modelled tables, run on a candidate build before it is published.

Every threshold comes from ``contracts/quality/rules.yaml`` (and freshness from the contracts).
Timestamps are exchanged with DuckDB as epoch seconds, so no time zone conversion happens in
Python. ``affected`` is always a list of UTC days (YYYY-MM-DD).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import duckdb

from pt_energy.quality.results import CheckResult, Severity, failed, passed
from pt_energy.quality.rules import DATASETS

# dataset -> (table, row filter, columns that identify one series)
SERIES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "omie_day_ahead_price": ("model.day_ahead_price", "source = 'omie'", ()),
    "entsoe_day_ahead_price": ("model.day_ahead_price", "source = 'entsoe'", ()),
    "entsoe_actual_load": ("model.demand", "source = 'entsoe'", ()),
    "eredes_national_consumption": ("model.demand", "source = 'eredes'", ()),
    "entsoe_actual_generation": ("model.generation_by_source", "true", ("psr_type", "direction")),
}
PRICE_DATASETS = ("omie_day_ahead_price", "entsoe_day_ahead_price")


@dataclass(frozen=True)
class Context:
    con: duckdb.DuckDBPyConnection
    now: datetime
    rules: dict[str, Any]
    contracts: dict[str, dict[str, Any]]

    @property
    def window_start(self) -> datetime:
        return self.now - timedelta(days=int(self.rules["window_days"]))

    @property
    def today(self) -> date:
        return self.now.date()

    def q(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        return self.con.execute(sql, params or []).fetchall()


def _utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def _days(values: list[Any]) -> list[str]:
    return sorted({str(v)[:10] for v in values})


# --- freshness ------------------------------------------------------------------------------


def freshness(ctx: Context) -> list[CheckResult]:
    out: list[CheckResult] = []
    hour = int(ctx.rules["freshness"]["price_publication_hour_utc"])
    for dataset, (table, where, _) in SERIES.items():
        severity: Severity = ctx.rules["freshness"]["severity"][dataset]
        max_age = float(ctx.contracts[dataset]["freshness"]["max_age_hours"])
        ((newest,),) = ctx.q(
            f"select epoch(max(interval_start_utc + resolution_minutes * interval '1 minute')) "
            f"from {table} where {where}"
        )
        if newest is None:
            out.append(
                failed(
                    "freshness", dataset, severity, "no data at all", threshold=f"<= {max_age:g} h"
                )
            )
            continue
        age = (ctx.now - _utc(newest)).total_seconds() / 3600
        threshold = f"<= {max_age:g} h old"
        problems = []
        if age > max_age:
            problems.append(f"newest data ends {age:.1f} h ago")
        if dataset in PRICE_DATASETS and ctx.now.hour >= hour:
            tomorrow = datetime.combine(ctx.today + timedelta(days=1), datetime.min.time(), UTC)
            if _utc(newest) < tomorrow:
                problems.append(
                    f"no prices for tomorrow yet (newest ends {_utc(newest):%Y-%m-%d %H:%M} UTC)"
                )
                threshold += f", tomorrow covered after {hour}:00 UTC"
        if problems:
            out.append(
                failed(
                    "freshness",
                    dataset,
                    severity,
                    "; ".join(problems),
                    observed=f"{age:.1f} h",
                    threshold=threshold,
                )
            )
        else:
            out.append(
                passed(
                    "freshness",
                    dataset,
                    severity,
                    f"newest data ends {age:.1f} h ago",
                    observed=f"{age:.1f} h",
                    threshold=threshold,
                )
            )
    return out


# --- completeness ---------------------------------------------------------------------------


def completeness(ctx: Context) -> list[CheckResult]:
    out: list[CheckResult] = []
    since = ctx.window_start.timestamp()
    for dataset, (table, where, series) in SERIES.items():
        severity: Severity = ctx.rules["completeness"]["severity"][dataset]
        part = f"partition by {', '.join(series)}" if series else ""
        rows = ctx.q(
            f"""
            with s as (
              select interval_start_utc t, resolution_minutes r,
                     lead(interval_start_utc) over ({part} order by interval_start_utc) nxt
              from {table} where {where} and epoch(interval_start_utc) >= ?
            )
            select cast(t + r * interval '1 minute' as date) first_missing_day,
                   sum(epoch(nxt - t) / 60 / r - 1) missing
            from s where nxt is not null and epoch(nxt - t) / 60 > r group by 1 order by 1
            """,
            [since],
        )
        ((total,),) = ctx.q(
            f"select count(*) from {table} where {where} and epoch(interval_start_utc) >= ?",
            [since],
        )
        if total == 0:
            out.append(failed("completeness", dataset, severity, "no data in the check window"))
        elif rows:
            n = int(sum(r[1] for r in rows))
            days = _days([r[0] for r in rows])
            out.append(
                failed(
                    "completeness",
                    dataset,
                    severity,
                    f"{n} missing intervals on {', '.join(days)}",
                    observed=f"{n} missing",
                    threshold="0 missing",
                    affected=days,
                )
            )
        else:
            out.append(
                passed(
                    "completeness",
                    dataset,
                    severity,
                    "no missing intervals",
                    observed="0 missing",
                    threshold="0 missing",
                )
            )

    # a production type that disappears is as bad as a gap, but never blocks by itself
    ratio = float(ctx.rules["completeness"]["series_count_min_ratio"])
    counts = ctx.q(
        """select cast(interval_start_utc as date) d, count(distinct psr_type || direction)
           from model.generation_by_source
           where epoch(interval_start_utc) >= ? group by 1 order by 1""",
        [since],
    )
    if counts:
        median = statistics.median(c for _, c in counts)
        low = [str(d) for d, c in counts if c < ratio * median]
        name = "series_count"
        ds = "entsoe_actual_generation"
        if low:
            out.append(
                failed(
                    name,
                    ds,
                    "warning",
                    f"fewer series than usual (median {median:g}) on {', '.join(low)}",
                    observed=f"min {min(c for _, c in counts)}",
                    threshold=f">= {ratio * median:g}",
                    affected=low,
                )
            )
        else:
            out.append(
                passed(
                    name,
                    ds,
                    "warning",
                    f"{median:g} series per day, none missing",
                    observed=f"median {median:g}",
                    threshold=f">= {ratio:g} x median",
                )
            )
    return out


# --- cross-source ---------------------------------------------------------------------------


def price_cross_check(ctx: Context) -> list[CheckResult]:
    tol = float(ctx.rules["price_cross_check"]["tolerance_eur_mwh"])
    since = ctx.window_start.timestamp()
    rows = ctx.q(
        """
        select cast(o.interval_start_utc as date) d, count(*) n,
               max(abs(o.price_eur_mwh - e.price_eur_mwh)) worst,
               sum((abs(o.price_eur_mwh - e.price_eur_mwh) > ?)::int) bad
        from model.day_ahead_price o join model.day_ahead_price e
          on o.interval_start_utc = e.interval_start_utc
         and o.resolution_minutes = e.resolution_minutes
        where o.source = 'omie' and e.source = 'entsoe' and epoch(o.interval_start_utc) >= ?
        group by 1 order by 1
        """,
        [tol, since],
    )
    name, ds, thr = "price_cross_check", "day_ahead_price", f"|OMIE - ENTSO-E| <= {tol:g} EUR/MWh"
    out: list[CheckResult] = []
    if not rows:
        out.append(
            failed(
                name,
                ds,
                "blocking",
                "no shared intervals to compare: one source is missing",
                threshold=thr,
            )
        )
    else:
        compared = sum(r[1] for r in rows)
        bad_days = [str(r[0]) for r in rows if r[3]]
        worst = max(r[2] for r in rows)
        if bad_days:
            n_bad = sum(r[3] for r in rows)
            out.append(
                failed(
                    name,
                    ds,
                    "blocking",
                    f"{n_bad} of {compared} intervals differ, worst by {worst:.2f} EUR/MWh",
                    observed=f"worst {worst:.2f}",
                    threshold=thr,
                    affected=bad_days,
                )
            )
        else:
            out.append(
                passed(
                    name,
                    ds,
                    "blocking",
                    f"{compared} intervals compared, worst difference {worst:.4f}",
                    observed=f"worst {worst:.4f}",
                    threshold=thr,
                )
            )

    # Days that one source has and the other lacks. The last day of the shorter feed is left out:
    # one source is always ahead (ENTSO-E covers Central European days, OMIE's last file ends
    # earlier), so its final day legitimately differs.
    ends = ctx.q(
        "select source, max(interval_start_utc)::date from model.day_ahead_price group by 1"
    )
    edge = min((d for _, d in ends), default=None) if len(ends) == 2 else None
    per_day = ctx.q(
        """select cast(interval_start_utc as date) d, source, count(*) from model.day_ahead_price
           where epoch(interval_start_utc) >= ? group by 1, 2""",
        [since],
    )
    by_day: dict[str, dict[str, int]] = {}
    for d, src, n in per_day:
        if edge is not None and d < edge:
            by_day.setdefault(str(d), {})[src] = n
    odd = sorted(d for d, s in by_day.items() if s.get("omie", 0) != s.get("entsoe", 0))
    if odd:
        out.append(
            failed(
                "price_coverage",
                ds,
                "warning",
                f"OMIE and ENTSO-E have different numbers of intervals on {', '.join(odd)}",
                observed=f"{len(odd)} days",
                threshold="same count",
                affected=odd,
            )
        )
    else:
        out.append(
            passed(
                "price_coverage",
                ds,
                "warning",
                "both sources cover the same past days",
                observed="0 days differ",
                threshold="same count",
            )
        )
    return out


def demand_cross_check(ctx: Context) -> list[CheckResult]:
    p = ctx.rules["demand_cross_check"]
    since = ctx.window_start.timestamp()
    hourly = ctx.q(
        """
        select epoch(e.interval_start_utc), e.demand_mw, r.demand_mw
        from model.demand e join model.demand r using (interval_start_utc)
        where e.source = 'entsoe' and r.source = 'eredes' and epoch(e.interval_start_utc) >= ?
          and cast(e.interval_start_utc as date) < ?
        order by 1
        """,
        [since, ctx.today],
    )
    ds = "demand"
    if not hourly:
        return [
            failed(
                "demand_cross_check",
                ds,
                "warning",
                "no overlapping hours between ENTSO-E and E-REDES",
            )
        ]
    by_day: dict[str, list[tuple[float, float]]] = {}
    for t, a, b in hourly:
        by_day.setdefault(str(_utc(t).date()), []).append((a, b))
    daily: dict[str, float] = {
        d: (sum(a for a, _ in v) - sum(b for _, b in v)) / sum(a for a, _ in v) * 100
        for d, v in by_day.items()
        if len(v) == 24  # only days where both sources have every hour
    }
    out: list[CheckResult] = []
    block, warn = float(p["daily_block_pct"]), float(p["daily_warn_pct"])
    thr = f"warn > {warn:g}%, block > {block:g}%"
    blocking = sorted(d for d, x in daily.items() if abs(x) > block)
    warning = sorted(d for d, x in daily.items() if warn < abs(x) <= block)
    worst = max((abs(x) for x in daily.values()), default=0.0)
    if blocking:
        out.append(
            failed(
                "demand_daily_energy",
                ds,
                "blocking",
                f"daily energy differs by more than {block:g}% on {', '.join(blocking)}",
                observed=f"worst {worst:.1f}%",
                threshold=thr,
                affected=blocking,
            )
        )
    if warning:
        out.append(
            failed(
                "demand_daily_energy",
                ds,
                "warning",
                f"daily energy differs by more than {warn:g}% on {', '.join(warning)}",
                observed=f"worst {worst:.1f}%",
                threshold=thr,
                affected=warning,
            )
        )
    if not daily:
        out.append(
            failed(
                "demand_daily_energy",
                ds,
                "warning",
                "no day has all 24 hours in both sources",
                threshold=thr,
            )
        )
    elif not blocking and not warning:
        out.append(
            passed(
                "demand_daily_energy",
                ds,
                "blocking",
                f"{len(daily)} days compared, worst {worst:.2f}%",
                observed=f"worst {worst:.2f}%",
                threshold=thr,
            )
        )
    hw = float(p["hourly_warn_pct"])
    odd = sorted({str(_utc(t).date()) for t, a, b in hourly if abs((a - b) / a * 100) > hw})
    worst_h = max(abs((a - b) / a * 100) for _, a, b in hourly)
    if odd:
        out.append(
            failed(
                "demand_hourly",
                ds,
                "warning",
                f"single hours differ by more than {hw:g}% on {', '.join(odd)}",
                observed=f"worst {worst_h:.1f}%",
                threshold=f"<= {hw:g}%",
                affected=odd,
            )
        )
    else:
        out.append(
            passed(
                "demand_hourly",
                ds,
                "warning",
                f"{len(hourly)} hours compared, worst {worst_h:.1f}%",
                observed=f"worst {worst_h:.1f}%",
                threshold=f"<= {hw:g}%",
            )
        )
    return out


# --- drift against recent history -----------------------------------------------------------

DRIFT_SQL = {
    "load": (
        "entsoe_actual_load",
        """select cast(interval_start_utc as date), avg(demand_mw)
        from model.demand where source = 'entsoe' group by 1 having count(*) = 24 order by 1""",
    ),
    "generation": (
        "entsoe_actual_generation",
        """
        with h as (select interval_start_utc t, sum(generation_mw) s from model.generation_by_source
                   where direction = 'generation' group by 1)
        select cast(t as date), avg(s) from h group by 1 having count(*) = 24 order by 1""",
    ),
    "price": (
        "omie_day_ahead_price",
        """select cast(interval_start_utc as date), avg(price_eur_mwh)
        from model.day_ahead_price where source = 'omie' group by 1
        having sum(resolution_minutes) = 1440 order by 1""",
    ),
}


def drift(ctx: Context) -> list[CheckResult]:
    p = ctx.rules["drift"]
    base_n, min_n = int(p["baseline_days"]), int(p["min_baseline_days"])
    first_day = ctx.window_start.date()
    out: list[CheckResult] = []
    for metric, (dataset, sql) in DRIFT_SQL.items():
        cfg = p[metric]
        series = [(d, float(v)) for d, v in ctx.q(sql)]
        block_days: list[str] = []
        warn_days: list[str] = []
        worst_ratio, worst_z, judged = 1.0, 0.0, 0
        for i, (day, value) in enumerate(series):
            if day < first_day:
                continue
            base = [v for _, v in series[max(0, i - base_n) : i]]
            if len(base) < min_n:
                continue
            judged += 1
            med = statistics.median(base)
            mad = statistics.median(abs(v - med) for v in base) * 1.4826
            ratio = value / med if med else float("inf")
            z = abs(value - med) / mad if mad > 0 else 0.0
            if abs(ratio - 1) > abs(worst_ratio - 1):
                worst_ratio = ratio
            worst_z = max(worst_z, z)
            lo, hi = cfg.get("block_ratio", (None, None))
            if lo is not None and not lo <= ratio <= hi:
                block_days.append(str(day))
            elif z > float(cfg["warn_z"]):
                warn_days.append(str(day))
        name = f"drift_{metric}"
        sev: Severity = "warning" if metric == "price" else "blocking"
        observed = f"ratio {worst_ratio:.2f}, z {worst_z:.1f}"
        limit = f"z <= {cfg['warn_z']}" + (
            f", ratio {cfg['block_ratio'][0]}-{cfg['block_ratio'][1]}"
            if "block_ratio" in cfg
            else ""
        )
        if block_days:
            out.append(
                failed(
                    name,
                    dataset,
                    "blocking",
                    f"daily value far outside the last {base_n} days on {', '.join(block_days)}",
                    observed=observed,
                    threshold=limit,
                    affected=block_days,
                )
            )
        if warn_days:
            out.append(
                failed(
                    name,
                    dataset,
                    "warning",
                    f"unusual daily value (robust z > {cfg['warn_z']}) on {', '.join(warn_days)}",
                    observed=observed,
                    threshold=limit,
                    affected=warn_days,
                )
            )
        if not block_days and not warn_days:
            note = f"{judged} days judged" if judged else f"not enough history (needs {min_n} days)"
            out.append(
                passed(
                    name, dataset, sev, note, observed=observed if judged else "", threshold=limit
                )
            )
    return out


# --- frozen values and internal consistency --------------------------------------------------


def _longest_runs(rows: list[tuple[Any, Any]], step_s: int, minimum: int) -> tuple[int, list[str]]:
    """Longest run of identical values at a fixed step, and the days of runs >= ``minimum``."""
    longest, days = 0, []
    run, prev_t, prev_v, start = 0, None, None, None
    for t, v in rows:
        if prev_t is not None and t - prev_t == step_s and v == prev_v:
            run += 1
        else:
            run, start = 1, t
        if run == minimum and start is not None:
            days.append(str(_utc(start).date()))
        longest = max(longest, run)
        prev_t, prev_v = t, v
    return longest, sorted(set(days))


def frozen(ctx: Context) -> list[CheckResult]:
    since = ctx.window_start.timestamp()
    out: list[CheckResult] = []
    cfg = ctx.rules["frozen"]
    specs: tuple[tuple[str, str, Severity, int, int, str, str], ...] = (
        ("frozen_values", "entsoe_actual_load", "blocking", int(cfg["load_hours"]), 3600,
         "select epoch(interval_start_utc), demand_mw from model.demand where source = 'entsoe' "
         "and epoch(interval_start_utc) >= ? order by 1", "hours"),
        ("frozen_values", "eredes_national_consumption", "warning",
         int(cfg["eredes_quarters"]), 900,
         "select epoch(interval_start_utc), total_kwh from staging.eredes_national_consumption "
         "where epoch(interval_start_utc) >= ? order by 1", "quarters"),
    )  # fmt: skip
    for name, dataset, severity, minimum, step, sql, unit in specs:
        rows = ctx.q(sql, [since])
        longest, days = _longest_runs(rows, step, minimum)
        thr = f"< {minimum} identical {unit} in a row"
        if days:
            out.append(
                failed(
                    name,
                    dataset,
                    severity,
                    f"value frozen for {minimum}+ {unit} on {', '.join(days)}",
                    observed=f"longest {longest} {unit}",
                    threshold=thr,
                    affected=days,
                )
            )
        else:
            out.append(
                passed(
                    name,
                    dataset,
                    severity,
                    f"longest identical run {longest} {unit}",
                    observed=f"longest {longest} {unit}",
                    threshold=thr,
                )
            )
    return out


def eredes_total_sum(ctx: Context) -> list[CheckResult]:
    tol = float(ctx.rules["eredes_total_tolerance_kwh"])
    rows = ctx.q(
        """select raw_window, max(abs(total_kwh - (low_voltage_kwh + medium_voltage_kwh
                  + high_voltage_kwh + very_high_voltage_kwh))) worst
           from staging.eredes_national_consumption where epoch(interval_start_utc) >= ?
           group by 1 order by 1""",
        [ctx.window_start.timestamp()],
    )
    ds, thr = "eredes_national_consumption", f"|total - sum of levels| <= {tol:g} kWh"
    bad = [str(w) for w, worst in rows if worst > tol]
    worst_all = max((w for _, w in rows), default=0.0)
    if bad:
        return [
            failed(
                "eredes_total_sum",
                ds,
                "warning",
                f"total differs from the sum of the voltage levels on {', '.join(bad)}",
                observed=f"worst {worst_all:.1f} kWh",
                threshold=thr,
                affected=bad,
            )
        ]
    return [
        passed(
            "eredes_total_sum",
            ds,
            "warning",
            "total equals the sum of the voltage levels",
            observed=f"worst {worst_all:.3f} kWh",
            threshold=thr,
        )
    ]


# --- what the pipeline itself rejected or revised -------------------------------------------


def parse_errors(ctx: Context) -> list[CheckResult]:
    rows = ctx.q("select dataset, raw_window, message from meta.errors order by 1, 2")
    by_ds: dict[str, list[tuple[str, str]]] = {}
    for d, w, m in rows:
        by_ds.setdefault(d, []).append((w, m))
    out: list[CheckResult] = []
    for ds in DATASETS:
        if ds in by_ds:
            items = by_ds[ds]
            out.append(
                failed(
                    "parse_errors",
                    ds,
                    "blocking",
                    f"{len(items)} raw windows cannot be parsed, e.g. {items[0][0]}: {items[0][1]}",
                    observed=f"{len(items)} windows",
                    threshold="0",
                    affected=[w for w, _ in items],
                )
            )
        else:
            out.append(
                passed(
                    "parse_errors",
                    ds,
                    "blocking",
                    "every raw window parsed",
                    observed="0",
                    threshold="0",
                )
            )
    return out


def rejected_rows(ctx: Context) -> list[CheckResult]:
    grace = (
        ctx.now - timedelta(days=int(ctx.rules["rejected"]["incomplete_hour_grace_days"]))
    ).date()
    since = ctx.window_start.date()
    rows = ctx.q(
        """select dataset, raw_window, reason from meta.rejected
           where raw_window >= ? and not (reason like 'incomplete_hour%' and raw_window >= ?)""",
        [str(since), str(grace)],
    )
    ds, thr = "eredes_national_consumption", "0 rejected rows (recent partial hours excepted)"
    if rows:
        reasons: dict[str, int] = {}
        for _, _, r in rows:
            key = r.split(" (")[0]
            reasons[key] = reasons.get(key, 0) + 1
        text = ", ".join(f"{n} {k}" for k, n in sorted(reasons.items()))
        return [
            failed(
                "rejected_rows",
                ds,
                "warning",
                f"rows rejected during staging: {text}",
                observed=f"{len(rows)} rows",
                threshold=thr,
                affected=_days([w for _, w, _ in rows]),
            )
        ]
    return [passed("rejected_rows", ds, "warning", "no rows rejected", observed="0", threshold=thr)]


def revisions(ctx: Context) -> list[CheckResult]:
    cfg = ctx.rules["revisions"]
    since = (ctx.now - timedelta(days=int(cfg["lookback_days"]))).timestamp()
    rows = ctx.q(
        """select dataset, raw_window, old_value, new_value from meta.revisions
           where epoch(detected_at) >= ? order by 1, 2""",
        [since],
    )
    out: list[CheckResult] = []
    warn = float(cfg["warn_pct"])
    for ds in DATASETS:
        mine = [(w, o, n) for d, w, o, n in rows if d == ds]
        is_price = ds in PRICE_DATASETS
        thr = "no revisions" if is_price else f"revisions <= {warn:g}%"

        def moved(o: float | None, n: float | None) -> float:
            if o is None or n is None:
                return float("inf")
            return abs(n - o) / max(abs(o), 1e-9) * 100

        flagged = [(w, o, n) for w, o, n in mine if is_price or moved(o, n) > warn]
        if flagged:
            worst = max(moved(o, n) for _, o, n in flagged)
            shown = (
                "added or removed rows" if worst == float("inf") else f"largest change {worst:.1f}%"
            )
            out.append(
                failed(
                    "revisions",
                    ds,
                    "warning",
                    f"{len(flagged)} revised values ({shown})",
                    observed=f"{len(flagged)} revised",
                    threshold=thr,
                    affected=_days([w for w, _, _ in flagged]),
                    opens_issue=is_price,
                )
            )
        else:
            out.append(
                passed(
                    "revisions",
                    ds,
                    "warning",
                    f"{len(mine)} small revisions" if mine else "no revisions",
                    observed=str(len(mine)),
                    threshold=thr,
                )
            )
    return out


ALL_CHECKS = (
    parse_errors,
    freshness,
    completeness,
    price_cross_check,
    demand_cross_check,
    drift,
    frozen,
    eredes_total_sum,
    rejected_rows,
    revisions,
)
