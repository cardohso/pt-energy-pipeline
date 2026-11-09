# Data model

```
raw/        immutable payloads + provenance       (step 4, see ingestion.md)
staging/    one parsed table per dataset          current raw version of every window
model/      what the dashboard reads               one row per interval
meta/       lineage, revisions, rejects, errors, batches
```

`pt-energy transform --data-dir D` rebuilds everything from `D/raw` into `D/serving/`:
`pt_energy.duckdb` plus one Parquet file per table in `staging/`, `model/` and `meta/`.
It is a pure function of the raw files: delete `serving/` and run it again to get the same tables.
The build goes to `serving.build/` first and replaces `serving/` only if every raw window parsed;
otherwise the previous build stays in place and the command exits 1.

## Time

- Every `*_utc` column is an absolute UTC instant (`TIMESTAMP WITH TIME ZONE`, Parquet
  `timestamp[us, tz=UTC]`). Publisher time conventions are resolved once, in
  `transform/parse.py`.
- OMIE trading days follow Spanish time. Periods count elapsed time from local midnight, so a
  clock-change day has 23 or 25 hours (92 or 100 quarter-hours).
- E-REDES gives UTC timestamps plus Lisbon wall-clock labels. The label must agree with the
  timestamp or the row is rejected.
- ENTSO-E periods are UTC. Prices cover Central European days (23, 24 or 25 hours); load and
  generation cover UTC days.
- Tests cover both 2026 clock changes (Madrid and Lisbon switch on the same instants) for every
  source, and the hourly OMIE days 2024-10-27 and 2025-03-30.

## Staging tables (`staging.<dataset>`)

The parsed contract fields plus `raw_window`, `raw_sha256` (hash of the raw file the row came
from) and `fetched_at`. Only the current raw version of each window is staged. ENTSO-E series that
omit repeated values are expanded to every slot up to the period's declared end.

## Modelled tables

| Table | Grain | Columns |
|---|---|---|
| `model.generation_by_source` | production type, direction, hour | `interval_start_utc`, `psr_type`, `direction` (generation or consumption), `generation_mw`, `resolution_minutes`, `raw_window`, `raw_sha256` |
| `model.demand` | source, hour | `interval_start_utc`, `source` (entsoe or eredes), `demand_mw`, `resolution_minutes` (60), `raw_window`, `raw_sha256` |
| `model.day_ahead_price` | source, market time unit | `interval_start_utc`, `source` (omie or entsoe), `price_eur_mwh`, `resolution_minutes` (15, or 60 before 2025-10-01), `raw_window`, `raw_sha256` |

- **Demand from E-REDES** is the sum of four 15-minute kWh values divided by 1000, which is the
  average MW over the hour (the same unit as ENTSO-E load). An hour with fewer than four quarters
  is left out and listed in `meta.rejected` as `incomplete_hour`.
- **Overlapping windows:** where two windows contain the same interval (ENTSO-E prices), the value
  from the newest fetch wins.
- Both sources sit in the same table on purpose: cross-source checks (step 6) are a self-join.

## Meta tables

| Table | Content |
|---|---|
| `meta.lineage` | every raw version ever fetched: dataset, window, `content_sha256`, `response_sha256`, `path`, `fetched_at`, `pipeline_version`, `contract_version`, `is_current` |
| `meta.revisions` | one row per changed value between successive versions of a window: `key`, `field`, `old_value`, `new_value`, both hashes, `detected_at` |
| `meta.rejected` | rows not loaded, with a reason (below) |
| `meta.errors` | raw windows that could not be parsed (they block publishing) |
| `meta.batches` | one row per build: `batch_id`, `built_at`, `pipeline_version`, `raw_state_sha256` (identifies the exact set of raw files used), `windows` |

### Lineage

Every modelled row carries `raw_sha256`. Join it to `meta.lineage.content_sha256` to get the raw
file path, fetch time, git SHA and contract version. A number on the dashboard can be followed
that way to the exact line of the raw file (there is a test that does it for one price).

### Rejected rows (never repaired, never guessed)

| Reason | Meaning |
|---|---|
| `nonexistent_local_time` | E-REDES row labelled with a Lisbon time that does not exist (spring-forward day), value 0 |
| `no_timestamp` | E-REDES row without `datahora` (fall-back day: labels 24:15 to 25:00); its slot is unknown, so the gap stays |
| `timestamp_mismatch` | E-REDES UTC stamp disagrees with the local label |
| `conflicting_duplicate` | same timestamp twice with different values; all copies rejected (seen 2026-11-01) |
| `incomplete_hour` | fewer than four E-REDES quarters in an hour |

Rejected rows leave gaps on purpose; the completeness checks in step 6 decide whether a gap blocks
publishing.

## What the real 30-day run showed (2026-10-10 to 2026-11-08)

146 raw windows produced 10 800 generation rows, 1 317 demand rows and 5 864 price rows.
OMIE and ENTSO-E prices agreed on all 2 884 shared intervals, including both clock-change days.
E-REDES lost 8 rows to `conflicting_duplicate`, 4 to `no_timestamp` and 3 hours to
`incomplete_hour`. Hourly demand from E-REDES differed from ENTSO-E load by +1.5% on average
(5th to 95th percentile: -1.9% to +5.1%; extremes -9.6% and +13.3%, 596 hours). Step 6 sets the
tolerance from this.
