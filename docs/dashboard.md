# Dashboard

A static page (plain HTML, CSS and JavaScript, no libraries, no external requests) that reads one
file, `data/dashboard.json`. Sources: [`dashboard/`](../dashboard/) (page) and
[`src/pt_energy/dashboard/export.py`](../src/pt_energy/dashboard/export.py) (the export).

```bash
pt-energy dashboard build --data-dir data --out site   # writes site/index.html and site/data/dashboard.json
python -m http.server -d site 8000                      # preview
```

The command refuses to run when nothing has been published yet (exit code 2).

## What it shows

| Block | Source table | Notes |
|---|---|---|
| Three headline numbers | `model.generation_by_source`, `model.day_ahead_price`, `model.demand` | renewable share (last 24 h), average OMIE price (same 24 h), peak ENTSO-E demand (7 days) |
| Generation by source | `model.generation_by_source` | six groups, stack order fixed (wind, hydro, solar, gas, biomass and other, pumped storage) |
| Demand, price | `model.demand`, `model.day_ahead_price` | hourly, UTC; price is the hourly mean of the market intervals |
| Data health | `reports/latest.json`, `reports/*.json`, `meta.revisions`, `meta.rejected`, `meta.lineage` | the quality gate's own report, not a second opinion |

Renewable share = biomass, hydro without pumped storage, solar and wind, divided by all generation.
Pumped storage counts in the total but not in the renewable part.

All times are UTC. Every chart has a legend with the latest values, a hover tooltip (arrow keys work
when the chart has focus) and a "View as table" fallback.

## Tracing a number to its raw file

Every headline number has a **Trace to raw files** button. The export computes the trace from the
same rows as the number: each model row carries `raw_sha256`, which is joined to `meta.lineage` to
give the raw file (path in the data repository, window, fetch time, contract version) and the plain
SHA-256 of its bytes. The tests recompute the renewable share independently, check that the trace
covers exactly the rows used, and check that each listed file exists and hashes to the value shown.

The file name carries a *content key*. For ENTSO-E it ignores the per-request document id and
timestamp, so an identical answer is not stored twice; that is why the key differs from the SHA-256
of the bytes there (for OMIE and E-REDES they are equal). The page shows both.

## Data-health panel

- a banner: published clean, published with warnings, or refused (then the page still shows the
  previous good build);
- counts of checks, passed, blocking, warnings, known issues;
- every check that is not a plain pass, with its message;
- freshness per dataset (age of the newest data against the limit in the contract), and when it was
  last fetched;
- the last 30 runs as boxes (published clean, with warnings, refused);
- revisions and rejected rows from the whole history.

## What leaves the private data repository

The export holds hourly aggregates for the last 7 days, three headline numbers, the quality report,
and paths and hashes of raw files. It does not hold raw payloads, and a test checks that. ENTSO-E
data is shown as aggregates with attribution, OMIE with attribution, E-REDES under CC BY 4.0; the
credits are on the page footer.

## Publishing (opt-in)

The `dashboard` job in [`pipeline.yml`](../.github/workflows/pipeline.yml) builds the site after the
pipeline job and deploys it to GitHub Pages. It only runs when the repository variable
`PAGES_ENABLED` is `true`, because publishing makes the page public. To turn it on:

1. Settings, Pages, Build and deployment, Source: **GitHub Actions**.
2. Settings, Secrets and variables, Actions, Variables: add `PAGES_ENABLED` = `true`.
3. Run the workflow once (Actions, Pipeline, Run workflow), or wait for the next scheduled run.

The job runs even when the pipeline step failed: a refused build leaves the previous good tables in
place, and the health panel says so.
