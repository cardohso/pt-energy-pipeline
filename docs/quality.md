# Data-quality gate

`pt-energy transform` builds the tables into `serving.build/`, runs every check below on that
candidate, and publishes it (`serving/`) only if no **blocking** check failed. Otherwise the
previous `serving/` stays exactly as it was, the rejected build is kept in `rejected/`, a report
is written to `reports/` (`latest.json` and `<batch_id>.json`), and the command exits 1.
Warnings publish and are shown on the health panel. All results are also stored in the build as
`meta.check_results`.

Thresholds live in [`contracts/quality/rules.yaml`](../contracts/quality/rules.yaml) and were set
from the real 2026-10-10 to 2026-11-08 backfill. Ranges, types, nullability, allowed values and the
primary key come from the contracts, not from code. 57 checks run on every build.

## Why Pandera

The contract checks use [Pandera](https://pandera.readthedocs.io). A schema is generated from each
contract and run on the staging table, reporting every failing value at once. Great Expectations
was rejected as heavy for five tables (it wants a project scaffold and a datasource layer), and
Soda Core because its anomaly detection needs the paid Soda Cloud. The checks that need history or
two sources (freshness, completeness, cross-source, drift) are plain DuckDB SQL, which is clearer
than forcing them into a DataFrame framework.

## The checks

| Check | Severity | Tolerance | Why it exists | Real data |
|---|---|---|---|---|
| `parse_errors` | blocking | 0 unparseable days | a changed format, unit or column count | 0 |
| `contract_schema` | blocking | exact types, UTC timestamps | publisher changed a type | pass |
| `contract_nulls` | blocking | none where non-nullable | broken rows | pass |
| `contract_ranges` | blocking | load 0-12,000 MW; generation 0-12,000 MW per type; E-REDES 0-3 M kWh per 15 min; price -1,000 to 6,000 EUR/MWh | corrupt values, unit errors | max load 8,134 MW, max type 4,714 MW, E-REDES 1.05-1.98 M kWh, price -1.46 to 380 |
| `contract_allowed_values` | blocking | contract list (production types B01-B20) | unknown category | pass |
| `contract_primary_key` | blocking | unique per raw window | double loads | pass |
| `freshness` | blocking (E-REDES: warning) | newest data younger than the contract's `max_age_hours` (6 h load and generation, 36 h prices, 168 h E-REDES); from 13:00 UTC prices for tomorrow must exist | a stopped feed | 1 h; E-REDES 133 h |
| `completeness` | blocking (E-REDES: warning) | no missing interval in the last 14 days | gaps | 0 missing; E-REDES 3 days |
| `series_count` | warning | at least 80% of the median number of generation series per day | a production type vanishing at the end of the data (a gap in the middle is caught by `completeness`) | 15 per day |
| `price_cross_check` | blocking | OMIE and ENTSO-E within 0.01 EUR/MWh on every shared interval | two publishers of one price must agree | 2,884 of 2,884 equal |
| `price_coverage` | warning | both sources have the same number of intervals on past days | one source missing a day | same |
| `demand_daily_energy` | warning above 4%, blocking above 10% | ENTSO-E load vs E-REDES, complete days only | unit or scope errors | -0.6% to +2.4% |
| `demand_hourly` | warning | 15% | a bad hour | worst 13.3% |
| `drift_load` | blocking outside 0.5-1.5x, warning above robust z 5 | daily mean vs the median of the previous 14 days | silent scale changes | ratio 0.84-1.07, z 4.1 |
| `drift_generation` | blocking outside 0.3-2.5x, warning above z 5 | same, total generation | same | ratio 0.70-1.49, z 4.8 |
| `drift_price` | warning above z 8, never blocks | same, OMIE daily mean | real markets move; OMIE vs ENTSO-E already catches wrong prices | ratio 0.43-1.46, z 6.2 (a windy weekend) |
| `frozen_values` | blocking for load (6 identical hours), warning for E-REDES (12 quarters) | | a stuck sensor or feed | longest run 1 |
| `eredes_total_sum` | warning | total equals the sum of voltage levels within 1 kWh | internal consistency | 0 |
| `rejected_rows` | warning | no rows rejected in staging (recent partial hours excepted for 5 days) | publisher defects | 15 over 30 days |
| `revisions` | warning; any price revision also asks for an issue | other datasets: a revised value moving more than 5% | late data | none |

E-REDES never blocks on completeness, freshness or its own quality: it is a cross-check, it is
published 4 or more days late, and its data has real defects. It can still block through the
contract checks (an impossible value) and the daily-energy cross-check above 10%.

## Known issues

`contracts/quality/known_issues.yaml` lists defects that were looked at, cannot be fixed and are
accepted. Each entry names the dataset, the checks, the **days** and the reason. A finding whose
affected days are all listed is reported as `known`; if it also fails on a day nobody has looked
at, it keeps failing for that day. Nothing is excused by pattern, only by day. Reviewed like code.

## What the tests prove

`tests/test_quality_gate.py` runs the whole pipeline on a deterministic synthetic world (five
consistent feeds, 40 days, weekday pattern and +-4% daily noise, the publishers' real time quirks):

- a healthy month passes all checks with no warnings, at seven different times of day (00:00 to 23:00,
  including either side of the price publication hour: truncated current day, tomorrow's prices
  not yet out, publication lag);
- each planted bad batch is caught by the right check: a truncated day, a missing day of prices,
  a missing generation day, a production type disappearing, a conflicting duplicate series,
  repeated primary keys, a price spike, a negative load, a value in the wrong unit, a changed
  ENTSO-E unit, a renamed E-REDES field, an extra OMIE column, an unknown production type, stale
  feeds, a feed that stops, missing prices for tomorrow, a stuck load, load at 40% of normal,
  generation at 3.2x, one wrong OMIE price, E-REDES 20% above ENTSO-E;
- things that look odd but are normal do not alarm: a 2% gap between load sources, a day at
  5x normal price (warning only), publication lag, E-REDES being days late;
- the main checks (price cross-check, ranges, freshness, completeness, drift, frozen values) were
  mutation-tested: switching one off makes its planted-batch test fail. The others were not.

A real dry run on the real 30 days replayed as of 2026-11-09 05:00 UTC published with 55 of 57
checks passing, one warning (E-REDES 2026-11-02, unexplained) and one known issue. Changing one
price in a real raw ENTSO-E file to 99999 blocked the build, kept the previous `serving/`
byte-identical, and listed the failing check and the days in `reports/latest.json`.

## Limits

- The thresholds come from 30 autumn days. Summer solar, spring negative prices and holidays are
  not in the data, so expect some warnings to need tuning.
- A stale feed blocks everything, including feeds that are fresh. It is simple and loud; the cost
  is that one ENTSO-E outage pauses publishing.
- The ENTSO-E and E-REDES load figures are not independent (both republish REN's), so the demand
  check finds publishing, unit and time-zone errors, not measurement errors.
- The price cross-check proves the two publishers agree, not that the price is right.
- E-REDES 2026-10-25: 4 rows have no timestamp and are rejected; whether they belong in the gap
  is unknown. See [data-model.md](data-model.md).
