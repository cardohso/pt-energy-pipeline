# Decisions: orchestration, persistence, alerting

## Orchestration: GitHub Actions cron, no Dagster

The workflow [`pipeline.yml`](../.github/workflows/pipeline.yml) runs daily at 15:30 UTC, on
demand (`workflow_dispatch`), and for backfills (mode `backfill`, start, end, datasets). Daily and
backfill call the same commands you run locally (`pt-energy daily`, `pt-energy backfill`,
`pt-energy transform`).

15:30 UTC is after day-ahead prices (about 11:45 UTC) and after E-REDES refreshes (about 14:35
UTC), so a daily run sees tomorrow's prices.

**Dagster: skipped.** Its main value here would be asset lineage, which the pipeline already
generates from its own definition ([lineage.md](lineage.md)), and OpenLineage events are written to
files. Running Dagster for real needs an always-on process or Dagster Cloud (paid); a Dagster
definition that is only ever run by cron would be a second description of the same five assets to
keep in sync. Revisit if there are several pipelines or dependencies between them.

## Persistence: a private data repository

The pipeline needs state between runs: the raw layer (append-only, the source of truth), and the
last **published** tables, so a refused build leaves the previous good ones in place. Compared:

| Option | History and audit | Licence fit | Growth | Verdict |
|---|---|---|---|---|
| **Private data repo** (chosen) | full git history, every raw version kept | raw ENTSO-E stays private (it is not on ENTSO-E's CC BY 4.0 list) | about 110 MB a year (estimate below) | **Chosen.** Free, auditable, atomic pushes, easy to read back |
| Data branch in the public repo | same | **republishes raw ENTSO-E data publicly**, a grey area under its Terms of Use | same | Rejected on licence grounds alone |
| Release assets | each asset is replaced or added whole, no diff history | public if the repo is | a whole snapshot per release | Clumsy for daily appends; no atomic update of several files |
| Workflow artifacts | none beyond the retention window | private | deleted after at most 90 days | **Not durable**; used only for refused builds (14 days) |

Estimate of growth, from the real 30-day backfill: raw payloads 3.9 MB per 30 days (about 47 MB a
year), published tables (`serving/model` and `serving/meta`, Parquet) about 170 KB per day
committed as a binary (about 62 MB a year). The DuckDB file (3.3 MB) is rebuildable and never
committed, nor is `serving/staging`. If the repository grows too large, squash old history once a
year; raw files are content-named, so nothing is lost by that.

**What is committed** (`scripts/persist_data.sh`): `raw/`, `reports/`, `lineage/`,
`serving/model/`, `serving/meta/`. The raw layer is append-only: a test checks that a second run
only adds files. A run refused by the quality gate leaves `serving/` untouched, so the previous
good tables are exactly what the repository still holds.

**Access: a deploy key, not a personal access token.** The workflow checks the data repository out
and pushes with an SSH deploy key that has write access to that one repository. A fine-grained
token also works, but it expires (a silent failure months later) and belongs to a user account; a
deploy key is scoped to the repository and does not expire. The private key is the secret
`DATA_DEPLOY_KEY`; the ENTSO-E token is `ENTSOE_API_TOKEN`. Neither appears in a script, only in
the `ssh-key` input and an `env` entry; a test checks this.

## What happens when something fails

| Failure | What the run does | Visible as |
|---|---|---|
| One window cannot be fetched (HTTP 5xx, timeout) | retried 4 times; after 3 failed windows in a row for a dataset, the rest of that dataset is skipped; what worked is stored; the next run retries | run marked failed; issue `alert:ingest_failure` |
| A blocking quality check fails | the build is refused; `serving/` keeps the previous good tables; the refused build is uploaded as artifact `rejected-build` (14 days); raw data and the report are still pushed | run marked failed; issue `alert:blocking_check` with the failing checks, days and batch |
| A day-ahead price is revised after publication | published if nothing else fails; flagged | issue `alert:price_revision` |
| The run crashes before producing a report | whatever was fetched is still pushed; no tables change | issue `alert:workflow_failure` |
| The data push conflicts with a manual edit | fails loudly, nothing overwritten | issue `alert:workflow_failure` |
| Another run is still going | the new run waits (`concurrency`) | nothing |

## Alerting

One open issue per kind of problem (label `alert:<kind>`). A failing run opens it if none is open;
otherwise it refreshes the body (always the latest state) and comments **only when the set of
failing checks changed**, so a persistent problem does not flood the inbox. A healthy run comments
"recovered" and closes it. Reports from an earlier run are ignored (`--since`), so a crash cannot
be mistaken for a pass or a failure it did not have. The logic is pure and tested with a fake
GitHub (`tests/test_alerts.py`); the workflow uses the `gh` command line with the job's own token.

## Known limits

- **GitHub disables scheduled workflows in a public repository after 60 days without repository
  activity.** The data repository's commits do not count, since the workflow lives in the code
  repository. A commit, an issue or a manual run in this repository resets it. If the dashboard
  stops updating, check the Actions tab first.
- Scheduled runs can start several minutes late. Nothing depends on the exact minute.
- A blocking defect that the publisher never fixes keeps the build refused (and the dashboard on
  the last good tables) until someone lists the days in `contracts/quality/known_issues.yaml` in a
  reviewed pull request. That is deliberate: publishing known-bad data should be a decision.
- The workflow itself can only be run on GitHub. Its structure, security properties and
  commands are tested here (`tests/test_workflow.py`, plus actionlint), and the commit logic
  against real git repositories (`tests/test_persist_script.py`), but the first real run is the
  real test.
