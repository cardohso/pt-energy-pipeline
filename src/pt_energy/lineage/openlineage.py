"""OpenLineage run events written to local files. No server, no extra dependency.

One file per batch, ``<data-dir>/lineage/openlineage/<batch_id>.jsonl``, with START and
COMPLETE or FAIL events for two jobs:

- ``transform``: inputs are the raw datasets (versioned by the hash of their current files),
  outputs are the staging and model tables.
- ``quality_gate``: inputs are the model tables, each with a standard ``dataQualityAssertions``
  facet listing the checks that touched it; the output is ``serving`` when the build was
  published. A rejected build has a FAIL event and no output.

Events follow the OpenLineage 2-0-2 RunEvent shape, so they can be sent to any OpenLineage
backend later. Row-level lineage lives in ``meta.lineage`` (see docs/lineage.md).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from pt_energy.quality.results import GateReport
from pt_energy.transform.stage import SPECS, Staged

PRODUCER = "https://github.com/cardohso/pt-energy-pipeline"
SCHEMA = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
NAMESPACE = "pt-energy-pipeline"
_NS_UUID = uuid.UUID("6f0c2a58-6b5f-4c0e-9d52-0a9c6f3f7a11")
MODEL_TABLES = ("generation_by_source", "demand", "day_ahead_price")
# which model table each dataset's checks speak about
_CHECK_TABLE = {
    "omie_day_ahead_price": "day_ahead_price",
    "entsoe_day_ahead_price": "day_ahead_price",
    "day_ahead_price": "day_ahead_price",
    "entsoe_actual_load": "demand",
    "eredes_national_consumption": "demand",
    "demand": "demand",
    "entsoe_actual_generation": "generation_by_source",
}


def _run_id(batch_id: str, job: str) -> str:
    return str(uuid.uuid5(_NS_UUID, f"{batch_id}/{job}"))


def _facet(schema: str, **fields: Any) -> dict[str, Any]:
    return {"_producer": PRODUCER, "_schemaURL": schema, **fields}


def _event(
    kind: str, when: str, batch_id: str, job: str, inputs: list[Any], outputs: list[Any]
) -> dict[str, Any]:
    return {
        "eventType": kind,
        "eventTime": when,
        "run": {"runId": _run_id(batch_id, job), "facets": {}},
        "job": {"namespace": NAMESPACE, "name": job},
        "inputs": inputs,
        "outputs": outputs,
        "producer": PRODUCER,
        "schemaURL": SCHEMA,
    }


def _raw_inputs(staged: Staged) -> list[dict[str, Any]]:
    inputs = []
    current = staged.lineage[staged.lineage["is_current"]]
    for spec in SPECS.values():
        mine = current[current["dataset"] == spec.dataset]
        version = hashlib.sha256("".join(sorted(mine["content_sha256"])).encode()).hexdigest()
        inputs.append(
            {
                "namespace": NAMESPACE,
                "name": f"raw/{spec.source}/{spec.dataset}",
                "facets": {
                    "version": _facet(
                        "https://openlineage.io/spec/facets/1-0-1/DatasetVersionDatasetFacet.json",
                        datasetVersion=version,
                    )
                },
            }
        )
    return inputs


def _assertions(report: GateReport) -> dict[str, list[dict[str, Any]]]:
    per_table: dict[str, list[dict[str, Any]]] = {t: [] for t in MODEL_TABLES}
    for r in report.results:
        table = _CHECK_TABLE.get(r.dataset)
        if table:
            per_table[table].append(
                {"assertion": f"{r.dataset}.{r.name}", "success": r.status != "fail"}
            )
    return per_table


def emit_openlineage(
    root: Path, report: GateReport, staged: Staged, counts: dict[str, int]
) -> Path:
    when = report.checked_at
    batch = report.batch_id
    raw = _raw_inputs(staged)
    transform_out = [
        {"namespace": NAMESPACE, "name": f"{layer}/{name}"}
        for layer, name in ([("staging", d) for d in SPECS] + [("model", t) for t in MODEL_TABLES])
    ]
    asserts = _assertions(report)
    gate_in = [
        {
            "namespace": NAMESPACE,
            "name": f"model/{t}",
            "inputFacets": {
                "dataQualityAssertions": _facet(
                    "https://openlineage.io/spec/facets/1-0-1/DataQualityAssertionsDatasetFacet.json",
                    assertions=asserts[t],
                )
            },
        }
        for t in MODEL_TABLES
    ]
    gate_out = [{"namespace": NAMESPACE, "name": "serving"}] if report.published else []
    end = "COMPLETE" if report.published else "FAIL"
    events = [
        _event("START", when, batch, "transform", raw, []),
        _event("COMPLETE", when, batch, "transform", raw, transform_out),
        _event("START", when, batch, "quality_gate", gate_in, []),
        _event(end, when, batch, "quality_gate", gate_in, gate_out),
    ]
    path = root / "lineage" / "openlineage" / f"{batch}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in events), encoding="utf-8")
    return path
