"""The publish gate: run every check on a candidate build and decide.

Order: parse errors, contract checks (Pandera, one per staging table), table checks. Findings that
match ``known_issues.yaml`` are moved from ``fail`` to ``known``. The results are stored in the
candidate itself (``meta.check_results``) so the dashboard can show them, and a JSON report is
written for alerting. The caller publishes the candidate only if ``report.ok``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from pt_energy.contracts import load_yaml
from pt_energy.quality.checks import ALL_CHECKS, Context
from pt_energy.quality.contract_checks import validate_dataset
from pt_energy.quality.results import CheckResult, GateReport
from pt_energy.quality.rules import (
    DATASETS,
    apply_known_issues,
    load_known_issues,
    load_rules,
)

RESULT_COLUMNS = [
    "batch_id", "check_name", "dataset", "severity", "status", "message", "observed",
    "threshold", "affected", "known", "opens_issue",
]  # fmt: skip


def results_frame(batch_id: str, results: list[CheckResult]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "batch_id": batch_id,
                "check_name": r.name,
                "dataset": r.dataset,
                "severity": r.severity,
                "status": r.status,
                "message": r.message,
                "observed": r.observed,
                "threshold": r.threshold,
                "affected": ",".join(r.affected),
                "known": ",".join(r.known),
                "opens_issue": r.opens_issue,
            }
            for r in results
        ],
        columns=RESULT_COLUMNS,
    ).astype({"opens_issue": "bool"})


def run_gate(
    candidate: Path,
    *,
    contracts_dir: Path,
    now: datetime | None = None,
) -> GateReport:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    rules = load_rules(contracts_dir / "quality" / "rules.yaml")
    known = load_known_issues(contracts_dir / "quality" / "known_issues.yaml")
    contracts = {d: load_yaml(contracts_dir / f"{d}.yaml") for d in DATASETS}

    con = duckdb.connect(str(candidate / "pt_energy.duckdb"))
    try:
        con.execute("SET TimeZone = 'UTC'")
        ctx = Context(con, now, rules, contracts)
        results: list[CheckResult] = []
        results += ALL_CHECKS[0](ctx)  # parse errors first: they explain everything after
        for dataset in DATASETS:
            results += validate_dataset(
                contracts[dataset], con.execute(f"select * from staging.{dataset}").df()
            )
        for check in ALL_CHECKS[1:]:
            results += check(ctx)
        apply_known_issues(results, known)

        batch_id, raw_state = con.execute(
            "select batch_id, raw_state_sha256 from meta.batches"
        ).fetchall()[0]
        frame = results_frame(batch_id, results)
        con.register("check_results_df", frame)
        con.execute("DROP TABLE IF EXISTS meta.check_results")
        con.execute("CREATE TABLE meta.check_results AS SELECT * FROM check_results_df")
        target = candidate / "meta" / "check_results.parquet"
        con.execute(f"COPY meta.check_results TO '{target}' (FORMAT PARQUET)")
    finally:
        con.close()
    return GateReport(batch_id, raw_state, now.strftime("%Y-%m-%dT%H:%M:%SZ"), results)


def write_report(reports_dir: Path, report: GateReport) -> Path:
    """Write ``<batch_id>.json`` and ``latest.json``; return the batch file."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"
    path = reports_dir / f"{report.batch_id}.json"
    path.write_text(text, encoding="utf-8")
    (reports_dir / "latest.json").write_text(text, encoding="utf-8")
    return path
