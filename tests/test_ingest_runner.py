"""Raw layer end to end: provenance, idempotency, backfill, revisions, failures. No network."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from conftest import TOKEN, FakeClock, FakeWeb, fixture_bytes, tree
from pt_energy.ingest.runner import RunReport, days_between, lookback_days, run_ingest
from pt_energy.ingest.sources import (
    OmieDayAheadPrice,
    Source,
    build_sources,
)
from pt_energy.ingest.store import RawStore, sha256_hex
from pt_energy.settings import contract_versions

CONTRACTS = Path(__file__).parent.parent / "contracts"
D3, D5 = date(2026, 11, 3), date(2026, 11, 5)
OMIE = "omie_day_ahead_price"


def run(
    web: FakeWeb,
    root: Path,
    clock: FakeClock,
    sleeps: list[float],
    days: list[date] | None = None,
    datasets: list[str] | None = None,
    **kwargs: object,
) -> RunReport:
    sources = build_sources(datasets or [], TOKEN)
    plan: list[tuple[Source, list[date]]] = [(s, days or [D3, D5]) for s in sources]
    return run_ingest(
        plan,
        client=web.client(),
        store=RawStore(root),
        pipeline_version="abc123def",
        contract_versions=contract_versions(CONTRACTS),
        now=clock,
        sleep=sleeps.append,
        **kwargs,  # type: ignore[arg-type]
    )


def status_of(report: RunReport, dataset: str, day: date) -> str:
    return next(r.status for r in report.results if r.dataset == dataset and r.window == str(day))


# --- first run ------------------------------------------------------------------------------


def test_first_run_stores_new_windows_and_reports_missing_ones_as_no_data(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    report = run(web, tmp_path, clock, sleeps)
    assert report.ok
    assert report.counts == {"new": 8, "no_data": 2}
    assert status_of(report, OMIE, D5) == "new" and status_of(report, OMIE, D3) == "no_data"
    assert status_of(report, "eredes_national_consumption", D3) == "new"
    assert status_of(report, "entsoe_actual_load", D3) == "new"


def test_payload_is_stored_byte_for_byte_under_its_hash(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    run(web, tmp_path, clock, sleeps, days=[D5], datasets=[OMIE])
    body = fixture_bytes("omie", "marginalpdbcpt_20261105.1")
    stored = tmp_path / "raw" / "omie" / OMIE / "2026-11-05" / f"{sha256_hex(body)}.txt"
    assert stored.read_bytes() == body


@pytest.mark.parametrize("day", ["2026-03-29", "2026-10-25"])
def test_daylight_saving_days_are_stored_untouched(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float], day: str
) -> None:
    """The odd rows on clock-change days are kept as published; staging deals with them."""
    run(
        web,
        tmp_path,
        clock,
        sleeps,
        days=[date.fromisoformat(day)],
        datasets=["eredes_national_consumption"],
    )
    body = fixture_bytes("eredes", f"consumo-total-nacional_{day}.json")
    folder = tmp_path / "raw" / "eredes" / "eredes_national_consumption" / day
    assert (folder / f"{sha256_hex(body)}.json").read_bytes() == body


def test_provenance_sidecar_has_every_required_field(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    run(web, tmp_path, clock, sleeps, days=[D5], datasets=["entsoe_day_ahead_price"])
    (meta_path,) = (tmp_path / "raw/entsoe/entsoe_day_ahead_price/2026-11-05").glob("*.meta.json")
    meta = json.loads(meta_path.read_text())
    body = fixture_bytes("entsoe", "price_2026-11-05.xml")
    assert meta["response_sha256"] == sha256_hex(body) and meta["bytes"] == len(body)
    assert meta["pipeline_version"] == "abc123def"
    assert meta["contract_version"] == contract_versions(CONTRACTS)["entsoe_day_ahead_price"]
    assert meta["source_url"].startswith("https://web-api.tp.entsoe.eu/api?")
    assert meta["request_params"]["documentType"] == "A44"
    assert meta["fetched_at"].endswith("Z") and meta["http_status"] == 200
    assert meta["supersedes"] is None and meta["attempts"] == 1


def test_the_api_token_never_reaches_disk(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    run(web, tmp_path, clock, sleeps)  # includes ENTSO-E windows
    web.set("entsoe_actual_load", "2026-11-03", status=401)
    run(web, tmp_path, clock, sleeps)  # and a failure message
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path


# --- idempotency ----------------------------------------------------------------------------


def test_rerunning_a_window_changes_nothing(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    first = run(web, tmp_path, clock, sleeps)
    before = tree(tmp_path)
    second = run(web, tmp_path, clock, sleeps)
    assert tree(tmp_path) == before  # identical files, identical bytes, nothing new
    assert first.counts == {"new": 8, "no_data": 2}
    assert second.counts == {"unchanged": 8, "no_data": 2}
    assert sum(1 for p in before if not p.endswith(".meta.json")) == 8  # one payload per window


def test_overlapping_backfills_never_duplicate(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    run(web, tmp_path, clock, sleeps, days=days_between(date(2026, 11, 1), date(2026, 11, 5)))
    before = tree(tmp_path)
    second = run(
        web, tmp_path, clock, sleeps, days=days_between(date(2026, 11, 3), date(2026, 11, 8))
    )
    after = tree(tmp_path)
    assert all(after[path] == content for path, content in before.items())  # nothing rewritten
    added = set(after) - set(before)
    assert added and all(any(f"/2026-11-0{d}/" in p for d in (6, 7, 8)) for p in added)
    overlap = [r for r in second.results if r.window <= "2026-11-05"]
    assert overlap and all(r.status in ("unchanged", "no_data") for r in overlap)


# --- backfill windows -----------------------------------------------------------------------


def test_days_between_is_inclusive_and_rejects_reversed_ranges() -> None:
    days = days_between(date(2026, 2, 27), date(2026, 3, 2))
    assert [d.isoformat() for d in days] == ["2026-02-27", "2026-02-28", "2026-03-01", "2026-03-02"]
    assert days_between(D5, D5) == [D5]
    with pytest.raises(ValueError, match="before start"):
        days_between(D5, D3)


def test_a_30_day_backfill_makes_one_request_per_day(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    days = days_between(date(2026, 10, 7), date(2026, 11, 5))
    assert len(days) == 30
    report = run(web, tmp_path, clock, sleeps, days=days, datasets=["eredes_national_consumption"])
    assert len(report.results) == 30 and web.count("eredes_national_consumption") == 30
    assert report.counts["new"] == 2  # only 2026-10-25 and 2026-11-03 have recorded fixtures


def test_daily_window_covers_lookback_and_tomorrow_for_day_ahead_sources() -> None:
    today = date(2026, 11, 9)
    omie_days = lookback_days(OmieDayAheadPrice(), today, 7)
    assert omie_days[0] == date(2026, 11, 2) and omie_days[-1] == date(2026, 11, 10)
    eredes_days = lookback_days(build_sources(["eredes_national_consumption"], None)[0], today, 7)
    assert eredes_days[-1] == today and len(eredes_days) == 8


# --- revisions (late data) ------------------------------------------------------------------


def test_a_changed_response_is_kept_as_a_new_version_and_flagged_revised(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    first = run(web, tmp_path, clock, sleeps, days=[D5], datasets=[OMIE])
    original = fixture_bytes("omie", "marginalpdbcpt_20261105.1")
    revised = original.replace(b"2026;11;05;1;262.24;262.24;", b"2026;11;05;1;270.00;262.24;")
    assert revised != original
    web.set(OMIE, "2026-11-05", body=revised)
    second = run(web, tmp_path, clock, sleeps, days=[D5], datasets=[OMIE])

    assert second.results[0].status == "revised"
    assert second.results[0].supersedes == first.results[0].sha256
    folder = tmp_path / "raw/omie" / OMIE / "2026-11-05"
    assert len(list(folder.glob("*.txt"))) == 2  # old version kept, never overwritten
    assert (folder / f"{sha256_hex(original)}.txt").read_bytes() == original
    newer = next(
        m for m in RawStore(tmp_path).versions("omie", OMIE, "2026-11-05") if m["supersedes"]
    )
    assert newer["response_sha256"] == sha256_hex(revised)


def test_a_revert_to_an_older_version_counts_as_a_revision(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    original = fixture_bytes("omie", "marginalpdbcpt_20261105.1")
    other = original.replace(b"262.24;262.24", b"270.00;262.24")
    statuses = []
    for body in (original, other, original, original):
        web.set(OMIE, "2026-11-05", body=body)
        statuses.append(
            run(web, tmp_path, clock, sleeps, days=[D5], datasets=[OMIE]).results[0].status
        )
    assert statuses == ["new", "revised", "revised", "unchanged"]
    assert RawStore(tmp_path).current_sha("omie", OMIE, "2026-11-05") == sha256_hex(original)


# --- failures -------------------------------------------------------------------------------


def test_one_failing_window_does_not_stop_the_others(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    web.set("eredes_national_consumption", "2026-11-03", status=500)
    report = run(web, tmp_path, clock, sleeps, datasets=["eredes_national_consumption", OMIE])
    assert not report.ok
    assert status_of(report, "eredes_national_consumption", D3) == "failed"
    assert status_of(report, OMIE, D5) == "new"  # the other dataset still got stored
    failed = next(r for r in report.results if r.status == "failed")
    assert "HTTP 500 after 4 attempts" in (failed.message or "")
    assert sleeps.count(2.0) >= 1  # it did retry with backoff before giving up


def test_a_failed_window_is_retried_by_the_next_run(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    web.set("eredes_national_consumption", "2026-11-03", status=500)
    run(web, tmp_path, clock, sleeps, days=[D3], datasets=["eredes_national_consumption"])
    web._overrides.clear()  # the service recovers
    again = run(web, tmp_path, clock, sleeps, days=[D3], datasets=["eredes_national_consumption"])
    assert again.ok and again.counts == {"new": 1}


def test_repeated_failures_trip_the_breaker_and_skip_the_rest(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    web.set(OMIE, status=503)
    days = days_between(date(2026, 11, 1), date(2026, 11, 8))
    report = run(web, tmp_path, clock, sleeps, days=days, datasets=[OMIE])
    assert report.counts == {"failed": 3, "skipped": 5}
    assert web.count(OMIE) == 3 * 4  # 3 windows x 4 attempts, then it stops asking
    assert not report.ok


def test_a_bad_token_fails_fast_without_retries(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    web.set("entsoe_actual_load", status=401)
    report = run(web, tmp_path, clock, sleeps, days=[D3, D5], datasets=["entsoe_actual_load"])
    assert [r.status for r in report.results] == ["failed", "failed"]
    assert "rejected the security token" in (report.results[0].message or "")
    assert web.count("entsoe_actual_load") == 2 and sleeps.count(2.0) == 0


def test_unexpected_content_is_a_failure_not_stored_data(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    web.set(OMIE, "2026-11-05", body=b"<html>maintenance</html>")
    report = run(web, tmp_path, clock, sleeps, days=[D5], datasets=[OMIE])
    assert report.counts == {"failed": 1}
    assert tree(tmp_path) == {}


def test_a_crash_between_payload_and_provenance_heals_on_the_next_run(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    run(web, tmp_path, clock, sleeps, days=[D5], datasets=[OMIE])
    folder = tmp_path / "raw/omie" / OMIE / "2026-11-05"
    for meta in folder.glob("*.meta.json"):
        meta.unlink()  # simulate dying before the provenance file was written
    (tmp_path / "raw/_log/ingest.jsonl").unlink()
    again = run(web, tmp_path, clock, sleeps, days=[D5], datasets=[OMIE])
    assert again.counts == {"new": 1} and len(list(folder.glob("*.meta.json"))) == 1


# --- log and run reports --------------------------------------------------------------------


def test_every_outcome_is_logged_and_each_run_writes_a_report(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    web.set(OMIE, "2026-11-03", status=500)
    report = run(web, tmp_path, clock, sleeps, datasets=[OMIE])
    lines = [json.loads(x) for x in (tmp_path / "raw/_log/ingest.jsonl").read_text().splitlines()]
    assert sorted(entry["status"] for entry in lines) == ["failed", "new"]
    assert all(entry["run_id"] == report.run_id for entry in lines)
    saved = json.loads((tmp_path / "raw/_runs" / f"{report.run_id}.json").read_text())
    assert saved["ok"] is False and saved["counts"] == {"failed": 1, "new": 1}
    assert saved["pipeline_version"] == "abc123def"


# --- volatile envelope fields (found in the first real rerun) -------------------------------

ENTSOE_LOAD = "entsoe_actual_load"


def _fresh_envelope_each_call(web: FakeWeb, kind: str, counter: list[int]) -> None:
    """Same data every time, but a new document id and creation time, like the real API."""
    import httpx

    from conftest import ENTSOE_FILES

    base = fixture_bytes("entsoe", ENTSOE_FILES[kind])

    def respond() -> httpx.Response:
        counter.append(1)
        body = base.replace(b"<mRID>synthetic</mRID>", f"<mRID>doc-{len(counter)}</mRID>".encode())
        body = body.replace(
            b"2026-11-05T12:00:00Z", f"2026-11-09T13:00:{len(counter):02d}Z".encode()
        )
        return httpx.Response(200, content=body)

    web.set_fn(kind, respond)


def test_a_new_document_id_and_timestamp_alone_is_not_a_revision(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    calls: list[int] = []
    _fresh_envelope_each_call(web, ENTSOE_LOAD, calls)
    first = run(web, tmp_path, clock, sleeps, days=[D5], datasets=[ENTSOE_LOAD])
    before = tree(tmp_path)
    second = run(web, tmp_path, clock, sleeps, days=[D5], datasets=[ENTSOE_LOAD])
    third = run(web, tmp_path, clock, sleeps, days=[D5], datasets=[ENTSOE_LOAD])
    assert len(calls) == 3  # it really fetched three times, with three different envelopes
    assert [first.counts, second.counts, third.counts] == [
        {"new": 1},
        {"unchanged": 1},
        {"unchanged": 1},
    ]
    assert tree(tmp_path) == before  # still exactly one payload and one provenance file


def test_a_changed_value_inside_an_entsoe_document_is_a_revision(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    run(web, tmp_path, clock, sleeps, days=[D5], datasets=[ENTSOE_LOAD])
    original = fixture_bytes("entsoe", "load_2026-11-05.xml")
    changed = original.replace(
        b"<position>1</position><quantity>", b"<position>1</position><quantity>9"
    )
    assert changed != original
    web.set(ENTSOE_LOAD, "2026-11-05", body=changed)
    second = run(web, tmp_path, clock, sleeps, days=[D5], datasets=[ENTSOE_LOAD])
    assert second.counts == {"revised": 1}


def test_provenance_keeps_both_the_exact_response_hash_and_the_content_hash(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    from pt_energy.ingest.sources import EntsoeActualLoad

    run(web, tmp_path, clock, sleeps, days=[D5], datasets=[ENTSOE_LOAD])
    body = fixture_bytes("entsoe", "load_2026-11-05.xml")
    (meta,) = RawStore(tmp_path).versions("entsoe", ENTSOE_LOAD, "2026-11-05")
    assert meta["response_sha256"] == sha256_hex(body)
    assert meta["content_sha256"] == sha256_hex(EntsoeActualLoad("t").canonical(body))
    assert meta["response_sha256"] != meta["content_sha256"]


def test_canonical_form_ignores_only_the_document_level_fields() -> None:
    from pt_energy.ingest.sources import EntsoeActualLoad

    src = EntsoeActualLoad("t")
    a = b"<D><mRID>aaa</mRID><createdDateTime>t1</createdDateTime><TimeSeries><mRID>1</mRID></TimeSeries></D>"
    b = b"<D><mRID>bbb</mRID><createdDateTime>t2</createdDateTime><TimeSeries><mRID>1</mRID></TimeSeries></D>"
    c = b"<D><mRID>aaa</mRID><createdDateTime>t1</createdDateTime><TimeSeries><mRID>2</mRID></TimeSeries></D>"
    assert src.canonical(a) == src.canonical(b)
    assert src.canonical(a) != src.canonical(c)  # a series id is data, not envelope
