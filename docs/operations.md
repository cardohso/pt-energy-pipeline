# Operations

## One-time setup

You need `gh` logged in as the owner. Nothing below puts a secret in a command line, a file in the
repository, or a chat.

1. Create the private data repository:
   ```bash
   gh repo create cardohso/pt-energy-data --private --description "Data for pt-energy-pipeline"
   ```
2. Create a deploy key and give the data repository write access with its public half:
   ```bash
   ssh-keygen -t ed25519 -N "" -C "pt-energy-pipeline deploy" -f ./deploy_key
   gh repo deploy-key add ./deploy_key.pub --repo cardohso/pt-energy-data --allow-write --title pt-energy-pipeline
   ```
3. Store the secrets in the code repository, then delete the key files:
   ```bash
   gh secret set DATA_DEPLOY_KEY --repo cardohso/pt-energy-pipeline < ./deploy_key
   gh secret set ENTSOE_API_TOKEN --repo cardohso/pt-energy-pipeline     # paste the token at the prompt
   rm ./deploy_key ./deploy_key.pub
   ```
4. Seed the history with a 30-day backfill and run once (about 15 minutes for the backfill):
   ```bash
   gh workflow run pipeline.yml --repo cardohso/pt-energy-pipeline -f mode=backfill -f start=2026-10-10 -f end=2026-11-08
   gh workflow run pipeline.yml --repo cardohso/pt-energy-pipeline          # a normal daily run
   ```

## Running by hand

```bash
pt-energy daily --lookback 7                       # refetch the last 7 days
pt-energy backfill --start 2026-10-01 --end 2026-10-31 -d omie_day_ahead_price
pt-energy transform                                # build, gate, publish if it passes
pt-energy alert --dry-run                          # what issues the last run would raise
```

## When an issue opens

| Issue | First look | Usual fix |
|---|---|---|
| `alert:blocking_check` | the issue lists the failing checks and days; the run has an artifact `rejected-build` | nothing, if the publisher corrects its data: the next run refetches 7 days and publishes. If the defect is permanent, review it and add the days to `contracts/quality/known_issues.yaml` (pull request) |
| `alert:ingest_failure` | the windows and messages in the issue | usually a publisher outage; retried automatically. A `401` means the ENTSO-E token expired |
| `alert:price_revision` | which days; `meta.revisions` has old and new values | check against OMIE. A blocking cross-check usually accompanies it |
| `alert:workflow_failure` | the run log | an environment problem; fix and rerun |

The published tables never change while a blocking check is failing, so the dashboard shows the
last good data.

## Rotating the ENTSO-E token

Generate a new token on the Transparency Platform (My Account), run
`gh secret set ENTSOE_API_TOKEN --repo cardohso/pt-energy-pipeline`, update your local `.env`.

## Stopping

Disable the workflow in the Actions tab. Data in the data repository is untouched.
