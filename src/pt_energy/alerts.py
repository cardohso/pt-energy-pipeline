"""Alerting: turn a run's reports into GitHub issues, without spamming.

One open issue per kind of problem (label ``alert:<kind>``):

- ``blocking_check``: the quality gate refused to publish; the previous build is still live;
- ``ingest_failure``: some windows could not be fetched;
- ``price_revision``: a day-ahead price was revised after publication (should not happen);
- ``workflow_failure``: the run failed for a reason none of the above explains.

A failing run opens the issue if none is open, otherwise it refreshes the issue body and comments
only when the set of failing checks changed. A healthy run comments and closes any open issue.
The decision logic is pure; GitHub is reached through a small interface (``Issues``) so tests use
a fake and the workflow uses the ``gh`` command line.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

KINDS = ("blocking_check", "ingest_failure", "price_revision", "workflow_failure")
TITLES = {
    "blocking_check": "Data pipeline: blocking check failed, previous build still published",
    "ingest_failure": "Data pipeline: ingestion failed for some windows",
    "price_revision": "Data pipeline: a day-ahead price was revised",
    "workflow_failure": "Data pipeline: the run failed",
}
_FINGERPRINT = re.compile(r"<!-- fingerprint: ([0-9a-f]+) -->")


@dataclass(frozen=True)
class Alert:
    kind: str
    title: str
    body: str
    fingerprint: str

    @property
    def label(self) -> str:
        return f"alert:{self.kind}"


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str


class Issues(Protocol):
    def ensure_label(self, name: str) -> None: ...
    def find_open(self, label: str) -> list[Issue]: ...
    def create(self, title: str, body: str, labels: list[str]) -> int: ...
    def edit_body(self, number: int, body: str) -> None: ...
    def comment(self, number: int, text: str) -> None: ...
    def close(self, number: int, text: str) -> None: ...


def _fingerprint(parts: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()[:16]


def _table(rows: Sequence[tuple[str, ...]], header: tuple[str, ...]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += [
        "| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in r) + " |" for r in rows
    ]
    return "\n".join(out)


def _footer(run_url: str | None, fingerprint: str) -> str:
    link = f"\n\nRun: {run_url}" if run_url else ""
    return f"{link}\n\n<!-- fingerprint: {fingerprint} -->\n"


def build_alerts(
    gate: dict[str, Any] | None,
    ingest: dict[str, Any] | None,
    *,
    run_url: str | None = None,
    workflow_failed: bool = False,
) -> list[Alert]:
    """The alerts this run should raise. Kinds not returned are healthy."""
    alerts: list[Alert] = []

    if gate and gate.get("blocking"):
        blocking = gate["blocking"]
        fp = _fingerprint(
            [f"{b['dataset']}.{b['name']}:{','.join(b['affected'])}" for b in blocking]
        )
        block_rows = [
            (b["dataset"], b["name"], b["message"], ", ".join(b["affected"]) or "-")
            for b in blocking
        ]
        body = (
            f"The quality gate blocked batch `{gate['batch_id']}`. **The previous build is still "
            "published**; nothing was replaced.\n\n"
            f"- Affected days (UTC): {', '.join(gate['affected_days']) or 'none listed'}\n"
            f"- Raw state: `{gate['raw_state_sha256']}`\n"
            f"- Checks: {gate['counts']['passed']} of {gate['counts']['checks']} passed, "
            f"{gate['counts']['blocking_failures']} blocking, "
            f"{gate['counts']['warnings']} warnings\n\n"
            + _table(block_rows, ("Dataset", "Check", "What failed", "Days"))
            + "\n\n**What to do:** open the `rejected-build` artifact of the run (or `reports/` in "
            "the data repository) and compare with the publisher. If the publisher fixes its data, "
            "the next daily run (it refetches 7 days) publishes by itself and this issue "
            "closes. If "
            "the defect is permanent and understood, list the days in "
            "`contracts/quality/known_issues.yaml` in a reviewed pull request."
            + _footer(run_url, fp)
        )
        alerts.append(Alert("blocking_check", TITLES["blocking_check"], body, fp))

    if ingest and (
        ingest.get("counts", {}).get("failed") or ingest.get("counts", {}).get("skipped")
    ):
        bad = [r for r in ingest["results"] if r["status"] in ("failed", "skipped")]
        fp = _fingerprint([f"{r['dataset']}:{r['window']}:{r['status']}" for r in bad])
        ingest_rows = [
            (r["dataset"], r["window"], r["status"], r.get("message") or "") for r in bad[:50]
        ]
        more = f"\n\n…and {len(bad) - 50} more." if len(bad) > 50 else ""
        body = (
            f"Run `{ingest['run_id']}` could not fetch {len(bad)} windows. The windows that worked "
            "are stored; the failed ones are retried by the next run.\n\n"
            + _table(ingest_rows, ("Dataset", "Day", "Outcome", "Message"))
            + more
            + _footer(run_url, fp)
        )
        alerts.append(Alert("ingest_failure", TITLES["ingest_failure"], body, fp))

    if gate and gate.get("issue_worthy"):
        revisions = [i for i in gate["issue_worthy"] if i["name"] == "revisions"]
        if revisions:
            fp = _fingerprint([f"{r['dataset']}:{','.join(r['affected'])}" for r in revisions])
            revision_rows = [
                (r["dataset"], ", ".join(r["affected"]), r["message"]) for r in revisions
            ]
            body = (
                "Day-ahead prices should be final once published, but these were revised after "
                f"publication (batch `{gate['batch_id']}`). Both versions are kept in the raw "
                "layer "
                "and listed in `meta.revisions`.\n\n"
                + _table(revision_rows, ("Dataset", "Days", "Detail"))
                + _footer(run_url, fp)
            )
            alerts.append(Alert("price_revision", TITLES["price_revision"], body, fp))

    if workflow_failed and not alerts:
        fp = _fingerprint(["workflow", run_url or ""])
        body = (
            "The pipeline run failed before it produced a quality report, so no check explains it. "
            "Open the run log." + _footer(run_url, fp)
        )
        alerts.append(Alert("workflow_failure", TITLES["workflow_failure"], body, fp))
    return alerts


def apply_alerts(
    client: Issues, alerts: list[Alert], *, run_url: str | None = None, batch: str = ""
) -> list[str]:
    """Create, refresh or close issues. Returns what was done, for the run log."""
    done: list[str] = []
    raised = {a.kind for a in alerts}
    for alert in alerts:
        client.ensure_label(alert.label)
        existing = client.find_open(alert.label)
        if not existing:
            number = client.create(alert.title, alert.body, [alert.label, "pipeline-alert"])
            done.append(f"opened #{number} ({alert.kind})")
            continue
        issue = existing[0]
        old = _FINGERPRINT.search(issue.body)
        client.edit_body(issue.number, alert.body)
        if old is None or old.group(1) != alert.fingerprint:
            client.comment(
                issue.number, f"The failing set changed.{' Run: ' + run_url if run_url else ''}"
            )
            done.append(
                f"updated #{issue.number} and commented ({alert.kind}: failing set changed)"
            )
        else:
            done.append(f"refreshed #{issue.number} ({alert.kind}: same failure)")
    for kind in KINDS:
        if kind in raised:
            continue
        for issue in client.find_open(f"alert:{kind}"):
            where = f" in batch `{batch}`" if batch else ""
            client.close(issue.number, f"Recovered{where}: no longer failing.")
            done.append(f"closed #{issue.number} ({kind}: recovered)")
    return done


def latest_json(path: Path, since: str | None, key: str) -> dict[str, Any] | None:
    """Load a report only if it belongs to this run (``key`` timestamp >= ``since``)."""
    if not path.exists():
        return None
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if since and str(doc.get(key, "")) < since:
        return None  # left over from an earlier run: this run never produced one
    return doc


def latest_ingest_report(raw_dir: Path, since: str | None) -> dict[str, Any] | None:
    runs = sorted((raw_dir / "_runs").glob("*.json")) if (raw_dir / "_runs").exists() else []
    return latest_json(runs[-1], since, "started_at") if runs else None


class GhCli:
    """``Issues`` through the GitHub command line (``gh``), authenticated by ``GH_TOKEN``."""

    def __init__(self, repo: str) -> None:
        self.repo = repo

    def _gh(self, *args: str) -> str:
        done = subprocess.run(
            ["gh", *args, "--repo", self.repo],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return done.stdout

    def ensure_label(self, name: str) -> None:
        self._gh("label", "create", name, "--force", "--color", "B60205")
        self._gh("label", "create", "pipeline-alert", "--force", "--color", "D93F0B")

    def find_open(self, label: str) -> list[Issue]:
        out = self._gh(
            "issue", "list", "--label", label, "--state", "open", "--json", "number,title,body"
        )
        return [Issue(i["number"], i["title"], i["body"]) for i in json.loads(out)]

    def create(self, title: str, body: str, labels: list[str]) -> int:
        args = ["issue", "create", "--title", title, "--body", body]
        for label in labels:
            args += ["--label", label]
        url = self._gh(*args).strip()
        return int(url.rsplit("/", 1)[-1])

    def edit_body(self, number: int, body: str) -> None:
        self._gh("issue", "edit", str(number), "--body", body)

    def comment(self, number: int, text: str) -> None:
        self._gh("issue", "comment", str(number), "--body", text)

    def close(self, number: int, text: str) -> None:
        self._gh("issue", "close", str(number), "--comment", text)
