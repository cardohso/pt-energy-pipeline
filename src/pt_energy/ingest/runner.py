"""Fetch windows, store raw payloads with provenance, keep going when one window fails."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from pt_energy.ingest.http import RateLimiter, Sleep, fetch
from pt_energy.ingest.models import IngestError, WindowResult
from pt_energy.ingest.sources import Source
from pt_energy.ingest.store import RawStore, utc_iso

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def days_between(start: date, end: date) -> list[date]:
    """Every day from ``start`` to ``end``, both included."""
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def lookback_days(source: Source, today: date, lookback: int) -> list[date]:
    """Daily-run windows: the last ``lookback`` days, plus tomorrow for day-ahead sources."""
    last = today + timedelta(days=1 if source.publishes_ahead else 0)
    return days_between(today - timedelta(days=lookback), last)


@dataclass
class RunReport:
    run_id: str
    started_at: str
    finished_at: str = ""
    pipeline_version: str = ""
    results: list[WindowResult] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return dict(Counter(r.status for r in self.results))

    @property
    def ok(self) -> bool:
        return not any(r.status in ("failed", "skipped") for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pipeline_version": self.pipeline_version,
            "ok": self.ok,
            "counts": self.counts,
            "results": [asdict(r) for r in self.results],
        }


def ingest_window(
    source: Source,
    day: date,
    *,
    client: httpx.Client,
    store: RawStore,
    limiter: RateLimiter,
    sleep: Sleep,
    now: Clock,
    run_id: str,
    pipeline_version: str,
    contract_version: str,
) -> WindowResult:
    window = f"{day:%Y-%m-%d}"
    spec = source.request(day)
    response, attempts = fetch(client, spec, limiter=limiter, sleep=sleep)
    verdict = source.interpret(response.status_code, response.content)
    if verdict == "no_data":
        return WindowResult(source.name, source.dataset, window, "no_data", attempts=attempts)

    fetched_at = utc_iso(now())
    provenance = {
        "source": source.name,
        "dataset": source.dataset,
        "window": window,
        "source_url": str(httpx.URL(spec.url, params=spec.redacted_params())),
        "request_params": spec.redacted_params(),
        "fetched_at": fetched_at,
        "http_status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "attempts": attempts,
        "pipeline_version": pipeline_version,
        "contract_version": contract_version,
        "run_id": run_id,
    }
    return store.save(
        source=source.name,
        dataset=source.dataset,
        window=window,
        body=response.content,
        extension=source.extension,
        provenance=provenance,
        content_key=source.canonical(response.content),
    )


def run_ingest(
    plan: list[tuple[Source, list[date]]],
    *,
    client: httpx.Client,
    store: RawStore,
    pipeline_version: str,
    contract_versions: dict[str, str],
    now: Clock = utc_now,
    sleep: Sleep | None = None,
    max_consecutive_failures: int = 3,
) -> RunReport:
    """Run every (source, days) pair. One bad window never stops the others.

    After ``max_consecutive_failures`` failures in a row for a source, its remaining windows are
    skipped (protects against a banned token or a down service) and reported as such.
    """
    sleep = sleep or time.sleep
    started = now()
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    report = RunReport(run_id, utc_iso(started), pipeline_version=pipeline_version)

    for source, days in plan:
        limiter = RateLimiter(source.min_interval_s, sleep=sleep)
        streak = 0
        for day in days:
            window = f"{day:%Y-%m-%d}"
            if streak >= max_consecutive_failures:
                result = WindowResult(
                    source.name, source.dataset, window, "skipped",
                    message=f"skipped after {streak} consecutive failures",
                )  # fmt: skip
            else:
                try:
                    result = ingest_window(
                        source, day, client=client, store=store, limiter=limiter, sleep=sleep,
                        now=now, run_id=run_id, pipeline_version=pipeline_version,
                        contract_version=contract_versions[source.dataset],
                    )  # fmt: skip
                    streak = 0
                except IngestError as exc:
                    streak += 1
                    result = WindowResult(
                        source.name, source.dataset, window, "failed", message=str(exc)
                    )
            if result.status in ("failed", "skipped", "no_data"):
                store.log(result, run_id=run_id, ts=utc_iso(now()))
            report.results.append(result)

    report.finished_at = utc_iso(now())
    store.write_run_report(run_id, report.to_dict())
    return report
