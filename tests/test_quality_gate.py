"""The quality gate end to end on the synthetic world: healthy data passes, planted bad batches
are caught by the right check, and ordinary day-to-day variation is not mistaken for a fault."""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from conftest import TOKEN, FakeClock, FakeWeb
from pt_energy.ingest.runner import run_ingest
from pt_energy.ingest.sources import build_sources
from pt_energy.ingest.store import RawStore
from pt_energy.quality.gate import run_gate
from pt_energy.quality.results import CheckResult, GateReport
from pt_energy.settings import contract_versions
from pt_energy.transform.model import build
from pt_energy.transform.stage import Staged, stage
from synth import MADRID, World

CONTRACTS = Path(__file__).parent.parent / "contracts"
NOW = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
START = date(2026, 10, 1)
OMIE, EREDES = "omie_day_ahead_price", "eredes_national_consumption"
LOAD, GEN, PRICE = "entsoe_actual_load", "entsoe_actual_generation", "entsoe_day_ahead_price"
_CALLS = itertools.count()  # every ingest call starts later than the previous one, like real time
DAY = "2026-11-04"  # a normal past day inside the check window, away from the edges


@dataclass(frozen=True)
class Tweaked(World):
    """The world with one day altered consistently in every source that shares the quantity."""

    day: date = date(2026, 11, 3)
    load_scale: float = 1.0
    gen_scale: float = 1.0
    price_scale: float = 1.0
    frozen: bool = False

    def load_mw(self, hour: datetime) -> float:
        if hour.date() != self.day:
            return super().load_mw(hour)
        if self.frozen and 4 <= hour.hour <= 13:
            return 5000.0
        return super().load_mw(hour) * self.load_scale

    def gen_mw(self, psr: str, base: float, solar: bool, hour: datetime) -> float:
        scale = self.gen_scale if hour.date() == self.day else 1.0
        return round(super().gen_mw(psr, base, solar, hour) * scale, 1)

    def price(self, slot_start: datetime) -> float:
        scale = self.price_scale if slot_start.astimezone(MADRID).date() == self.day else 1.0
        return round(super().price(slot_start) * scale, 2)


@dataclass
class Out:
    report: GateReport
    root: Path
    out: Path
    staged: Staged
    web: FakeWeb


def ingest_days(
    web: FakeWeb,
    root: Path,
    last: date,
    datasets: list[str] | None = None,
    start: date = START,
    skip: set[tuple[str, str]] | None = None,
) -> None:
    plan = []
    for s in build_sources(datasets or [], TOKEN):
        end = last + timedelta(days=1 if s.publishes_ahead else 0)
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        plan.append((s, [d for d in days if (s.dataset, str(d)) not in (skip or set())]))
    run_ingest(
        plan, client=web.client(), store=RawStore(root), pipeline_version="test-sha",
        contract_versions=contract_versions(CONTRACTS),
        now=FakeClock(datetime(2026, 11, 9, 0, 0, tzinfo=UTC) + timedelta(minutes=10 * next(_CALLS))),
        sleep=lambda _s: None,
    )  # fmt: skip


def pipeline(
    tmp_path: Path,
    *,
    world: World | None = None,
    now: datetime = NOW,
    gate_now: datetime | None = None,
    plant: Callable[[FakeWeb], None] | None = None,
) -> Out:
    web = FakeWeb()
    web.world = world or World(as_of=now)
    if plant:
        plant(web)
    root = tmp_path / "data"
    ingest_days(web, root, now.date())
    staged = stage(RawStore(root))
    out = tmp_path / "out"
    build(staged, out, now=now)
    report = run_gate(out, contracts_dir=CONTRACTS, now=gate_now or now)
    return Out(report, root, out, staged, web)


def find(report: GateReport, name: str, dataset: str | None = None) -> list[CheckResult]:
    return [
        r for r in report.results if r.name == name and (dataset is None or r.dataset == dataset)
    ]


def blocking_names(report: GateReport) -> set[tuple[str, str]]:
    return {(r.dataset, r.name) for r in report.blocking}


def warning_names(report: GateReport) -> set[tuple[str, str]]:
    return {(r.dataset, r.name) for r in report.warnings}


def edit(body: bytes, old: str, new: str, count: int = 1) -> bytes:
    text = body.decode()
    assert old in text, f"{old!r} not found"
    return text.replace(old, new, count).encode()


# --- healthy data and normal variation ------------------------------------------------------


def test_a_healthy_month_passes_every_check(tmp_path: Path) -> None:
    out = pipeline(tmp_path)
    assert out.report.ok
    assert [r.name for r in out.report.warnings] == []
    assert all(r.status == "pass" for r in out.report.results)
    assert len(out.report.results) >= 50


@pytest.mark.parametrize("hour", [0, 3, 11, 12, 13, 18, 23])
def test_every_time_of_day_is_healthy_including_around_price_publication(
    tmp_path: Path, hour: int
) -> None:
    """Truncated current day, tomorrow's prices not out yet, publication lag: all normal."""
    now = datetime(2026, 11, 9, hour, 0, tzinfo=UTC)
    out = pipeline(tmp_path, now=now)
    assert out.report.ok, [(r.dataset, r.name, r.message) for r in out.report.blocking]
    assert out.report.warnings == []


def test_ordinary_daily_variation_does_not_trip_drift_checks(tmp_path: Path) -> None:
    out = pipeline(tmp_path)
    world = World(as_of=NOW)
    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(38)]
    loads = [
        sum(world.load_mw(datetime(d.year, d.month, d.day, h, tzinfo=UTC)) for h in range(24))
        for d in days
    ]
    assert max(loads) / min(loads) > 1.08  # the world really does vary (weekends, +-4% noise)
    for name in ("drift_load", "drift_generation", "drift_price"):
        (res,) = find(out.report, name)
        assert res.status == "pass" and "days judged" in res.message


def test_a_shorter_history_is_reported_as_not_enough_not_as_a_pass_of_nothing(
    tmp_path: Path,
) -> None:
    web = FakeWeb()
    web.world = World(as_of=NOW)
    ingest_days(web, tmp_path / "d", NOW.date(), start=date(2026, 11, 5))
    staged = stage(RawStore(tmp_path / "d"))
    build(staged, tmp_path / "o", now=NOW)
    report = run_gate(tmp_path / "o", contracts_dir=CONTRACTS, now=NOW)
    (res,) = find(report, "drift_load")
    assert "not enough history" in res.message


# --- planted bad batches: missing intervals -------------------------------------------------


def test_a_truncated_day_in_the_middle_of_the_history_is_caught(tmp_path: Path) -> None:
    early = World(as_of=datetime(2026, 11, 4, 12, 0, tzinfo=UTC))  # the feed as it was at noon
    out = pipeline(tmp_path, plant=lambda w: w.set(LOAD, DAY, body=early.payload(LOAD, DAY)))
    assert not out.report.ok
    (res,) = [r for r in find(out.report, "completeness", LOAD)]
    assert res.status == "fail" and res.severity == "blocking" and DAY in res.affected
    assert "12 missing" in res.message or "missing intervals" in res.message


def test_a_missing_day_of_prices_is_caught(tmp_path: Path) -> None:
    out = pipeline(tmp_path, plant=lambda w: w.set(OMIE, "2026-11-03", status=404, body=b""))
    assert (OMIE, "completeness") in blocking_names(out.report)
    # the trading day of 2026-11-03 starts at 23:00 UTC on 11-02, and days are reported in UTC
    assert "2026-11-02" in find(out.report, "completeness", OMIE)[0].affected
    assert find(out.report, "completeness", PRICE)[0].status == "pass"  # the other feed is fine


def test_a_whole_missing_generation_day_is_caught(tmp_path: Path) -> None:
    ack = World(as_of=NOW).payload(GEN, "2099-01-01")  # the "no data" acknowledgement
    out = pipeline(tmp_path, plant=lambda w: w.set(GEN, "2026-11-02", body=ack))
    assert (GEN, "completeness") in blocking_names(out.report)


def drop_series(types: tuple[str, ...], days: list[str]) -> Callable[[FakeWeb], None]:
    def plant(web: FakeWeb) -> None:
        for d in days:
            body = web.world.payload(GEN, d).decode()
            for block in re.findall(r"<TimeSeries>.*?</TimeSeries>", body, re.S):
                if any(f"<psrType>{t}</psrType>" in block for t in types):
                    body = body.replace(block, "")
            web.set(GEN, d, body=body.encode())

    return plant


def test_a_production_type_that_disappears_at_the_end_is_a_warning_not_a_block(
    tmp_path: Path,
) -> None:
    """Nothing is missing between two points, so completeness cannot see it; the count can."""
    out = pipeline(tmp_path, plant=drop_series(("B18", "B20", "B01"), ["2026-11-08", "2026-11-09"]))
    (res,) = find(out.report, "series_count")
    assert res.status == "fail" and res.severity == "warning"
    assert {"2026-11-08", "2026-11-09"} <= set(res.affected)
    assert out.report.ok  # not blocking by itself


def test_a_production_type_that_disappears_in_the_middle_is_a_gap_and_blocks(
    tmp_path: Path,
) -> None:
    out = pipeline(tmp_path, plant=drop_series(("B18",), ["2026-11-05", "2026-11-06"]))
    assert (GEN, "completeness") in blocking_names(out.report)


# --- planted bad batches: duplicates --------------------------------------------------------


def duplicate_series(body: bytes, psr: str, bump: float) -> bytes:
    text = body.decode()
    block = next(
        b
        for b in re.findall(r"<TimeSeries>.*?</TimeSeries>", text, re.S)
        if f"<psrType>{psr}</psrType>" in b
    )
    changed = re.sub(
        r"(<quantity>)([-\d.]+)(</quantity>)",
        lambda m: f"{m[1]}{float(m[2]) + bump}{m[3]}",
        block,
        count=1,
    )
    return text.replace(block, block + changed).encode()


def test_a_duplicated_series_with_different_values_blocks_the_build(tmp_path: Path) -> None:
    def plant(web: FakeWeb) -> None:
        web.set(GEN, DAY, body=duplicate_series(web.world.payload(GEN, DAY), "B19", 1.0))

    out = pipeline(tmp_path, plant=plant)
    assert (GEN, "parse_errors") in blocking_names(out.report)
    assert DAY in find(out.report, "parse_errors", GEN)[0].affected
    assert "conflicting values" in find(out.report, "parse_errors", GEN)[0].message


def test_a_duplicated_series_with_identical_values_is_harmless(tmp_path: Path) -> None:
    def plant(web: FakeWeb) -> None:
        web.set(GEN, DAY, body=duplicate_series(web.world.payload(GEN, DAY), "B19", 0.0))

    out = pipeline(tmp_path, plant=plant)
    assert out.report.ok and out.report.warnings == []


def test_repeated_primary_keys_inside_one_window_fail_the_contract_check(tmp_path: Path) -> None:
    import duckdb
    import pandas as pd

    from pt_energy.contracts import load_yaml
    from pt_energy.quality.contract_checks import validate_dataset

    out = pipeline(tmp_path)
    con = duckdb.connect(str(out.out / "pt_energy.duckdb"), read_only=True)
    df = con.execute(f"select * from staging.{LOAD}").df()
    dup = pd.concat([df, df.iloc[[5]]], ignore_index=True)
    results = validate_dataset(load_yaml(CONTRACTS / f"{LOAD}.yaml"), dup)
    (pk,) = [r for r in results if r.name == "contract_primary_key"]
    assert pk.status == "fail" and pk.severity == "blocking"


def test_conflicting_duplicates_in_e_redes_only_warn(tmp_path: Path) -> None:
    def plant(web: FakeWeb) -> None:
        doc = json.loads(web.world.payload(EREDES, DAY))
        twin = dict(doc["results"][40], total=1.0)
        doc["results"].insert(41, twin)
        doc["total_count"] += 1
        web.set(EREDES, DAY, body=json.dumps(doc).encode())

    out = pipeline(tmp_path, plant=plant)
    assert out.report.ok  # E-REDES quality problems never block on their own
    assert (EREDES, "rejected_rows") in warning_names(out.report)
    assert (EREDES, "completeness") in warning_names(out.report)


# --- planted bad batches: out-of-range values -----------------------------------------------


def test_a_price_spike_is_caught_by_range_and_by_the_cross_check(tmp_path: Path) -> None:
    def plant(web: FakeWeb) -> None:
        body = web.world.payload(PRICE, "2026-11-03")
        web.set(
            PRICE,
            "2026-11-03",
            body=re.sub(
                rb"<price.amount>[^<]*</price.amount>",
                b"<price.amount>99999</price.amount>",
                body,
                count=1,
            ),
        )

    out = pipeline(tmp_path, plant=plant)
    assert (PRICE, "contract_ranges") in blocking_names(out.report)
    assert ("day_ahead_price", "price_cross_check") in blocking_names(out.report)


def test_a_negative_load_is_out_of_range(tmp_path: Path) -> None:
    def plant(web: FakeWeb) -> None:
        body = web.world.payload(LOAD, DAY)
        web.set(
            LOAD,
            DAY,
            body=re.sub(rb"(<quantity>)[^<]*(</quantity>)", rb"\g<1>-50\g<2>", body, count=1),
        )

    out = pipeline(tmp_path, plant=plant)
    assert (LOAD, "contract_ranges") in blocking_names(out.report)
    assert DAY in find(out.report, "contract_ranges", LOAD)[0].affected


def test_an_e_redes_value_in_the_wrong_unit_is_out_of_range(tmp_path: Path) -> None:
    def plant(web: FakeWeb) -> None:
        doc = json.loads(web.world.payload(EREDES, DAY))
        doc["results"][3]["total"] *= 1000  # MWh published as kWh
        web.set(EREDES, DAY, body=json.dumps(doc).encode())

    out = pipeline(tmp_path, plant=plant)
    assert (EREDES, "contract_ranges") in blocking_names(out.report)


def test_a_value_outside_the_range_but_plausible_for_other_days_is_still_caught(
    tmp_path: Path,
) -> None:
    """A 12,500 MW load is not a drift problem, it is a corrupt value (limit 12,000)."""

    def plant(web: FakeWeb) -> None:
        body = web.world.payload(LOAD, DAY)
        web.set(
            LOAD,
            DAY,
            body=re.sub(rb"(<quantity>)[^<]*(</quantity>)", rb"\g<1>12500\g<2>", body, count=1),
        )

    assert (LOAD, "contract_ranges") in blocking_names(pipeline(tmp_path, plant=plant).report)


# --- planted bad batches: breaking schema changes -------------------------------------------


def test_a_changed_unit_in_entsoe_data_blocks_the_build(tmp_path: Path) -> None:
    def plant(web: FakeWeb) -> None:
        web.set(GEN, DAY, body=edit(web.world.payload(GEN, DAY), "MAW", "KAW", count=99))

    out = pipeline(tmp_path, plant=plant)
    assert (GEN, "parse_errors") in blocking_names(out.report)
    assert "unit changed" in find(out.report, "parse_errors", GEN)[0].message


def test_a_renamed_field_in_the_e_redes_api_blocks_the_build(tmp_path: Path) -> None:
    def plant(web: FakeWeb) -> None:
        web.set(
            EREDES,
            DAY,
            body=edit(web.world.payload(EREDES, DAY), '"total"', '"total_kwh"', count=999),
        )

    out = pipeline(tmp_path, plant=plant)
    assert (EREDES, "parse_errors") in blocking_names(out.report)


def test_an_extra_column_in_the_omie_file_blocks_the_build(tmp_path: Path) -> None:
    def plant(web: FakeWeb) -> None:
        body = web.world.payload(OMIE, DAY).decode().replace(";\n", ";0;\n")
        web.set(OMIE, DAY, body=body.encode())

    out = pipeline(tmp_path, plant=plant)
    assert (OMIE, "parse_errors") in blocking_names(out.report)
    assert "expected 6" in find(out.report, "parse_errors", OMIE)[0].message


def test_an_unknown_production_type_blocks_the_build(tmp_path: Path) -> None:
    def plant(web: FakeWeb) -> None:
        web.set(
            GEN,
            DAY,
            body=edit(
                web.world.payload(GEN, DAY), "<psrType>B16</psrType>", "<psrType>B99</psrType>"
            ),
        )

    assert (GEN, "contract_allowed_values") in blocking_names(
        pipeline(tmp_path, plant=plant).report
    )


def test_a_naive_timestamp_column_fails_the_schema_check(tmp_path: Path) -> None:
    import duckdb

    from pt_energy.contracts import load_yaml
    from pt_energy.quality.contract_checks import validate_dataset

    out = pipeline(tmp_path)
    df = (
        duckdb.connect(str(out.out / "pt_energy.duckdb"), read_only=True)
        .execute(f"select * from staging.{PRICE}")
        .df()
    )
    df["interval_start_utc"] = df["interval_start_utc"].dt.tz_localize(None)
    results = validate_dataset(load_yaml(CONTRACTS / f"{PRICE}.yaml"), df)
    assert next(r for r in results if r.name == "contract_schema").status == "fail"


def test_a_missing_required_value_fails_the_null_check(tmp_path: Path) -> None:
    import duckdb

    from pt_energy.contracts import load_yaml
    from pt_energy.quality.contract_checks import validate_dataset

    out = pipeline(tmp_path)
    df = (
        duckdb.connect(str(out.out / "pt_energy.duckdb"), read_only=True)
        .execute(f"select * from staging.{PRICE}")
        .df()
    )
    df.loc[3, "price_eur_mwh"] = float("nan")
    results = validate_dataset(load_yaml(CONTRACTS / f"{PRICE}.yaml"), df)
    nulls = next(r for r in results if r.name == "contract_nulls")
    assert nulls.status == "fail" and nulls.affected  # names the day


# --- planted bad batches: stale feeds -------------------------------------------------------


def test_a_gate_run_three_days_later_with_no_new_data_blocks_on_stale_feeds(tmp_path: Path) -> None:
    out = pipeline(tmp_path, gate_now=NOW + timedelta(days=3))
    stale = {ds for ds, name in blocking_names(out.report) if name == "freshness"}
    assert stale == {LOAD, GEN, OMIE, PRICE}
    assert (EREDES, "freshness") in warning_names(out.report)  # E-REDES is only a warning
    assert not out.report.ok


def test_a_feed_that_silently_stops_publishing_is_caught(tmp_path: Path) -> None:
    ack = World(as_of=NOW).payload(LOAD, "2099-01-01")

    def plant(web: FakeWeb) -> None:
        for d in ("2026-11-07", "2026-11-08", "2026-11-09"):
            web.set(LOAD, d, body=ack)

    out = pipeline(tmp_path, plant=plant)
    assert (LOAD, "freshness") in blocking_names(out.report)
    assert "h ago" in find(out.report, "freshness", LOAD)[0].message
    assert find(out.report, "freshness", GEN)[0].status == "pass"


def test_missing_prices_for_tomorrow_after_the_publication_hour_are_caught(tmp_path: Path) -> None:
    early = World(as_of=datetime(2026, 11, 9, 9, 0, tzinfo=UTC))  # tomorrow not published yet

    def plant(web: FakeWeb) -> None:
        web.set(OMIE, "2026-11-10", status=404, body=b"")
        for d in ("2026-11-09", "2026-11-10"):
            web.set(PRICE, d, body=early.payload(PRICE, d))

    out = pipeline(tmp_path, plant=plant)
    assert {(OMIE, "freshness"), (PRICE, "freshness")} <= blocking_names(out.report)
    assert "tomorrow" in find(out.report, "freshness", PRICE)[0].message


# --- frozen values, drift, cross-source -----------------------------------------------------


def test_a_load_value_stuck_for_ten_hours_is_caught(tmp_path: Path) -> None:
    out = pipeline(tmp_path, world=Tweaked(as_of=NOW, frozen=True))
    (res,) = find(out.report, "frozen_values", LOAD)
    assert res.status == "fail" and res.severity == "blocking" and "2026-11-03" in res.affected


def test_a_day_with_load_at_forty_percent_of_normal_is_caught_by_drift(tmp_path: Path) -> None:
    out = pipeline(tmp_path, world=Tweaked(as_of=NOW, load_scale=0.4))
    blocks = [r for r in find(out.report, "drift_load") if r.blocks]
    assert blocks and "2026-11-03" in blocks[0].affected


def test_a_generation_day_at_three_times_normal_is_caught_by_drift(tmp_path: Path) -> None:
    out = pipeline(tmp_path, world=Tweaked(as_of=NOW, gen_scale=3.2))
    assert (GEN, "drift_generation") in blocking_names(out.report)


def test_a_price_day_five_times_normal_is_only_a_warning(tmp_path: Path) -> None:
    out = pipeline(tmp_path, world=Tweaked(as_of=NOW, price_scale=5.0))
    assert out.report.ok  # prices can really spike; OMIE and ENTSO-E agree, so nothing blocks
    assert (OMIE, "drift_price") in warning_names(out.report)


def test_a_single_wrong_omie_price_is_caught_by_the_cross_check(tmp_path: Path) -> None:
    def plant(web: FakeWeb) -> None:
        body = web.world.payload(OMIE, "2026-11-03").decode()
        web.set(
            OMIE,
            "2026-11-03",
            body=body.replace(";10;", ";10;", 1)
            .replace("2026;11;03;20;", "2026;11;03;20;", 1)
            .encode(),
        )
        lines = body.splitlines()
        i = next(k for k, line in enumerate(lines) if line.startswith("2026;11;03;20;"))
        parts = lines[i].split(";")
        parts[4] = str(float(parts[4]) + 5)
        lines[i] = ";".join(parts)
        web.set(OMIE, "2026-11-03", body=("\n".join(lines) + "\n").encode())

    out = pipeline(tmp_path, plant=plant)
    (res,) = [r for r in find(out.report, "price_cross_check") if r.blocks]
    assert "1 of" in res.message and "2026-11-03" in res.affected


def scaled_eredes(factor: float) -> Callable[[FakeWeb], None]:
    def plant(web: FakeWeb) -> None:
        doc = json.loads(web.world.payload(EREDES, DAY))
        for row in doc["results"]:
            for k in ("bt", "mt", "at", "mat", "total"):
                row[k] = round(row[k] * factor, 3)
        web.set(EREDES, DAY, body=json.dumps(doc).encode())

    return plant


def test_e_redes_20_percent_above_entsoe_for_a_day_blocks(tmp_path: Path) -> None:
    out = pipeline(tmp_path, plant=scaled_eredes(1.2))
    blocks = [r for r in find(out.report, "demand_daily_energy") if r.blocks]
    assert blocks and DAY in blocks[0].affected


def test_a_seven_percent_gap_between_load_sources_is_only_a_warning(tmp_path: Path) -> None:
    out = pipeline(tmp_path, plant=scaled_eredes(1.07))
    assert out.report.ok
    assert ("demand", "demand_daily_energy") in warning_names(out.report)


def test_a_two_percent_gap_is_normal(tmp_path: Path) -> None:
    out = pipeline(tmp_path, plant=scaled_eredes(1.02))
    assert out.report.ok and out.report.warnings == []


# --- revisions ------------------------------------------------------------------------------


def revise(tmp_path: Path, kind: str, day: str, edit_fn: Callable[[bytes], bytes]) -> Out:
    out = pipeline(tmp_path)
    out.web.set(kind, day, body=edit_fn(out.web.world.payload(kind, day)))
    ingest_days(out.web, out.root, NOW.date(), datasets=[kind])
    staged = stage(RawStore(out.root))
    build(staged, tmp_path / "out2", now=NOW)
    report = run_gate(tmp_path / "out2", contracts_dir=CONTRACTS, now=NOW)
    return Out(report, out.root, tmp_path / "out2", staged, out.web)


def test_a_revised_price_asks_for_an_issue_and_blocks_while_the_sources_disagree(
    tmp_path: Path,
) -> None:
    out = revise(
        tmp_path,
        PRICE,
        "2026-11-08",
        lambda b: re.sub(
            rb"<price.amount>([\d.]+)<",
            lambda m: b"<price.amount>" + str(float(m[1]) + 0.5).encode() + b"<",
            b,
            count=1,
        ),
    )
    (res,) = find(out.report, "revisions", PRICE)
    assert res.status == "fail" and res.severity == "warning" and res.opens_issue
    assert res in out.report.issue_worthy
    # the revised ENTSO-E price no longer equals OMIE's: that blocks until the sources agree again
    assert ("day_ahead_price", "price_cross_check") in blocking_names(out.report)


def test_a_small_load_revision_is_normal_and_a_large_one_warns(tmp_path: Path) -> None:
    def bump(factor: float) -> Callable[[bytes], bytes]:
        return lambda b: re.sub(
            rb"<quantity>([\d.]+)<",
            lambda m: b"<quantity>" + str(round(float(m[1]) * factor, 1)).encode() + b"<",
            b,
            count=1,
        )

    small = revise(tmp_path / "a", LOAD, "2026-11-08", bump(1.02))
    assert find(small.report, "revisions", LOAD)[0].status == "pass"
    large = revise(tmp_path / "b", LOAD, "2026-11-08", bump(1.20))
    (res,) = find(large.report, "revisions", LOAD)
    assert res.status == "fail" and not res.opens_issue and large.report.ok


# --- E-REDES quirks never block; known issues -----------------------------------------------


def test_missing_e_redes_rows_are_a_warning_and_a_listed_day_is_known(tmp_path: Path) -> None:
    def plant(web: FakeWeb) -> None:
        doc = json.loads(web.world.payload(EREDES, "2026-11-01"))
        doc["results"] = doc["results"][:-3]  # the last three quarters never arrived
        doc["total_count"] = len(doc["results"])
        web.set(EREDES, "2026-11-01", body=json.dumps(doc).encode())

    out = pipeline(tmp_path, plant=plant)
    assert out.report.ok
    (res,) = find(out.report, "completeness", EREDES)
    assert res.status == "known" and res.known == ["2026-11-01"]  # listed in known_issues.yaml
    assert {r.name for r in out.report.known} == {"completeness", "rejected_rows"}  # both listed


def test_the_same_defect_on_a_day_nobody_has_looked_at_still_warns(tmp_path: Path) -> None:
    def plant(web: FakeWeb) -> None:
        doc = json.loads(web.world.payload(EREDES, "2026-11-02"))
        doc["results"] = doc["results"][:-3]
        doc["total_count"] = len(doc["results"])
        web.set(EREDES, "2026-11-02", body=json.dumps(doc).encode())

    out = pipeline(tmp_path, plant=plant)
    (res,) = find(out.report, "completeness", EREDES)
    assert res.status == "fail" and res.severity == "warning" and "2026-11-02" in res.affected


# --- persistence and report -----------------------------------------------------------------


def test_results_are_stored_in_the_build_and_the_report_lists_what_failed(tmp_path: Path) -> None:
    import duckdb

    from pt_energy.quality.gate import write_report

    out = pipeline(tmp_path, gate_now=NOW + timedelta(days=2))
    con = duckdb.connect(str(out.out / "pt_energy.duckdb"), read_only=True)
    total = con.execute("select count(*) from meta.check_results").fetchone()[0]  # type: ignore[index]
    failing = con.execute(
        "select count(*) from meta.check_results where status = 'fail' and severity = 'blocking'"
    ).fetchone()[0]  # type: ignore[index]
    assert total == len(out.report.results) and failing == len(out.report.blocking) > 0
    assert (out.out / "meta" / "check_results.parquet").exists()

    path = write_report(tmp_path / "reports", out.report)
    doc = json.loads((tmp_path / "reports" / "latest.json").read_text())
    assert path.name == f"{out.report.batch_id}.json" and doc["ok"] is False
    assert doc["counts"]["blocking_failures"] == len(out.report.blocking)
    assert {b["name"] for b in doc["blocking"]} == {"freshness"}
    assert doc["raw_state_sha256"] == out.staged.raw_state_sha256
    assert doc["issue_worthy"] and {"name", "dataset", "message", "affected"} <= set(
        doc["blocking"][0]
    )
    assert replace  # keep the import used when the module is edited
