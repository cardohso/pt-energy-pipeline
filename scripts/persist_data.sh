#!/usr/bin/env bash
# Commit and push the data repository after a pipeline run.
#
#   scripts/persist_data.sh <data-dir> ["commit message"]
#
# What is persisted (everything else is rebuilt or ignored):
#   raw/            append-only payloads, provenance, ingest log and run reports
#   reports/        quality reports (latest.json and one per batch)
#   lineage/        OpenLineage events
#   serving/model/  the published tables (Parquet)
#   serving/meta/   check results, lineage table, revisions, rejected rows, batches (Parquet)
# Not persisted: serving/staging (rebuildable), *.duckdb (rebuildable), serving.build and
# rejected (a refused build is uploaded as a workflow artifact instead).
#
# A run that failed the quality gate leaves serving/ untouched, so the previous good tables stay
# in the repository: that is how "keep the last good dataset published" survives between runs.
set -euo pipefail

dir="${1:?usage: persist_data.sh <data-dir> [message]}"
message="${2:-data: pipeline run}"
cd "$dir"

ignore=$'serving/staging/\nserving.build/\nrejected/\n*.duckdb\n*.duckdb.wal\n'
if [ "$(cat .gitignore 2>/dev/null || true)" != "${ignore%$'\n'}" ]; then
  printf '%s' "$ignore" > .gitignore
fi
if [ ! -f README.md ]; then
  cat > README.md <<'README'
# pt-energy-data

Data for [pt-energy-pipeline](https://github.com/cardohso/pt-energy-pipeline). **Keep this
repository private**: `raw/` holds ENTSO-E payloads, which are not on ENTSO-E's open-data list.

- `raw/`: immutable payloads and provenance, ingest log
- `reports/`, `lineage/`: quality reports, OpenLineage events
- `serving/model`, `serving/meta`: the last published tables (Parquet)

Written only by the pipeline workflow. Do not edit by hand.
README
fi

paths=(.gitignore README.md)
for p in raw reports lineage serving/model serving/meta; do
  [ -e "$p" ] && paths+=("$p")
done
git add --all -- "${paths[@]}"

if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi

git -c user.name="${GIT_AUTHOR_NAME:-pt-energy-pipeline[bot]}" \
    -c user.email="${GIT_AUTHOR_EMAIL:-pt-energy-pipeline@users.noreply.github.com}" \
    commit --quiet -m "$message"

for attempt in 1 2 3 4 5; do
  if git push --quiet; then
    echo "pushed on attempt $attempt"
    exit 0
  fi
  echo "push failed (attempt $attempt), rebasing" >&2
  git pull --rebase --quiet || { echo "rebase conflict: manual fix needed" >&2; git rebase --abort || true; exit 1; }
  sleep "${PERSIST_RETRY_SLEEP:-3}"
done
echo "could not push after 5 attempts" >&2
exit 1
