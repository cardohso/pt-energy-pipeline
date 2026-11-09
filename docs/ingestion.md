# Ingestion and the raw layer

```bash
pt-energy daily                      # last 7 days (plus tomorrow's prices), all datasets
pt-energy backfill --start 2026-10-07 --end 2026-11-05            # inclusive, all datasets
pt-energy backfill --start 2026-10-07 --end 2026-11-05 -d omie_day_ahead_price
```

Both commands run the same code. `ENTSOE_API_TOKEN` is read from `.env`; the data folder is
`--data-dir` or `PT_ENERGY_DATA_DIR` (default `./data`). Exit code 1 means at least one window
failed; the windows that worked are stored regardless.

## Layout

```
raw/<source>/<dataset>/<YYYY-MM-DD>/<content-sha256>.<ext>        the payload, byte for byte
raw/<source>/<dataset>/<YYYY-MM-DD>/<content-sha256>.meta.json    provenance
raw/_log/ingest.jsonl                                              every attempt, append-only
raw/_runs/<run_id>.json                                            one report per run
```

Files are never modified or deleted. A window is one calendar day (UTC day for ENTSO-E requests,
trading day for OMIE, local day for E-REDES); transformation turns them into UTC timestamps.

## Provenance (in every `.meta.json`)

`source_url` and `request_params` (token replaced by `REDACTED`), `fetched_at`, `http_status`,
`response_sha256` (exact bytes), `content_sha256` (identity of the version, see below),
`pipeline_version` (git SHA), `contract_version`, `attempts`, `run_id`, and `supersedes` (the
version this one replaced, if any). The token is never written to disk; a test checks every file.

## Idempotency

A version is named by the hash of its **content**. Fetching a window again with the same content
writes nothing and reports `unchanged`.

ENTSO-E stamps every response with a new document `mRID` and `createdDateTime`, so identical data
differs byte for byte. The first real rerun showed this (three false "revisions"). Those two
document-level fields are ignored when computing `content_sha256`; the stored file is still the
first response, unchanged. Series ids and all data stay part of the hash.

## Retries, rate limits, partial failures

- Network errors, HTTP 429 and 5xx: up to 4 attempts, waits of 2, 4 and 8 seconds, honouring
  `Retry-After` (capped at 60 s). Other statuses are not retried.
- Requests are spaced 0.25 s apart for ENTSO-E (limit: 400 per minute per token) and 0.5 s for
  the others.
- A bad token (401) or unexpected content fails that window at once.
- After 3 failed windows in a row for one dataset, its remaining windows are `skipped`, so a
  banned token or a down service is not hammered.
- Outcomes: `new`, `unchanged`, `revised`, `no_data` (OMIE 404, empty E-REDES day, ENTSO-E
  code 999), `failed`, `skipped`.

## Revision policy

Publishers revise past values (observed: ENTSO-E data is replaced after the fact; E-REDES says its
data is dynamic). Decision: **version, then flag. Never overwrite, never ignore.**

1. Raw files are immutable, so every version ever seen stays on disk.
2. If a window's content changes, the new version is stored next to the old one, the run reports
   `revised` with both hashes, and `supersedes` links them. A change back to an older version also
   counts as `revised`; the newest log entry is the current version.
3. Every daily run refetches the last 7 days, so late corrections are caught within a week. Older
   changes are only caught by a manual backfill.
4. Staging (step 5) loads the current version and keeps earlier values in a `revisions` table.
   Quality gates (step 6) warn on large revisions, and any revision of a day-ahead price opens an
   issue, because those prices should be final once published.

Why not overwrite: it loses the audit trail and makes it impossible to say what yesterday's
dashboard showed. Why not only flag: a flag without the old values cannot be investigated.

## Known limits

- A revision older than the 7-day lookback is not noticed by the daily run.
- ENTSO-E price documents cover Central European days, so one UTC-day request returns two
  overlapping periods. Both are stored as published; staging resolves the overlap.
- Dates are for the day the pipeline asked about, not proof the publisher had finished it. Gaps
  and stale data are caught by quality gates, not by ingestion.
