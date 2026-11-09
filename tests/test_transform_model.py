"""Raw -> staging -> modelled tables, end to end on fixtures. No network."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from conftest import FakeClock, FakeWeb, fixture_bytes, ingest
from pt_energy.cli import app
from pt_energy.ingest.store import RawStore, sha256_hex
from pt_energy.transform.model import build, swap_directory
from pt_energy.transform.stage import stage

OMIE, EREDES = "omie_day_ahead_price", "eredes_national_consumption"
ALL5 = [OMIE, EREDES, "entsoe_actual_load", "entsoe_actual_generation", "entsoe_day_ahead_price"]


def make_raw(
    web: FakeWeb,
    root: Path,
    clock: FakeClock,
    sleeps: list[float],
    days: list[str] | None = None,
    datasets: list[str] | None = None,
) -> RawStore:
    ingest(web, root, clock, sleeps, days or ["2026-11-03", "2026-11-05"], datasets or ALL5)
    return RawStore(root)


def build_into(store: RawStore, out: Path) -> duckdb.DuckDBPyConnection:
    build(stage(store), out)
    con = duckdb.connect(str(out / "pt_energy.duckdb"), read_only=True)
    con.execute("SET TimeZone = 'UTC'")
    return con


@pytest.fixture
def built(web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]):
    store = make_raw(web, tmp_path / "data", clock, sleeps)
    return store, build_into(store, tmp_path / "out"), tmp_path / "out"


# --- outputs --------------------------------------------------------------------------------


def test_tables_exist_in_duckdb_and_as_parquet(built) -> None:
    _, con, out = built
    tables = {
        f"{s}.{t}"
        for s, t in con.execute(
            "select table_schema, table_name from information_schema.tables where table_schema in "
            "('staging','model','meta')"
        ).fetchall()
    }
    assert {
        "model.generation_by_source",
        "model.demand",
        "model.day_ahead_price",
        "meta.lineage",
        "meta.revisions",
        "meta.rejected",
        "meta.errors",
        "meta.batches",
        *(f"staging.{d}" for d in ALL5),
    } <= tables
    for name in (
        "model/demand.parquet",
        "model/day_ahead_price.parquet",
        "model/generation_by_source.parquet",
        "staging/omie_day_ahead_price.parquet",
        "meta/lineage.parquet",
    ):
        assert pq.read_table(out / name).num_rows >= 0


def test_every_timestamp_is_a_utc_instant(built) -> None:
    _, con, out = built
    for table in ("model.demand", "model.day_ahead_price", "model.generation_by_source"):
        assert con.execute(
            f"select typeof(interval_start_utc) from {table} limit 1"
        ).fetchone() == ("TIMESTAMP WITH TIME ZONE",)
    schema = pq.read_table(out / "model/demand.parquet").schema
    assert str(schema.field("interval_start_utc").type) == "timestamp[us, tz=UTC]"


def test_prices_from_omie_and_entsoe_agree_on_every_shared_interval(built) -> None:
    _, con, _ = built
    n, equal = con.execute(
        "select count(*), sum((abs(o.price_eur_mwh - e.price_eur_mwh) < 0.005)::int) "
        "from model.day_ahead_price o join model.day_ahead_price e using (interval_start_utc) "
        "where o.source = 'omie' and e.source = 'entsoe'"
    ).fetchone()
    assert n == 96 and equal == 96  # one OMIE day, all 96 quarters, identical to ENTSO-E


def test_demand_hourly_mw_comes_from_four_quarters_of_kwh(built) -> None:
    _, con, _ = built
    got = con.execute(
        "select demand_mw from model.demand where source = 'eredes' "
        "and interval_start_utc = '2026-11-03 00:00+00'"
    ).fetchone()[0]
    quarters = [
        r
        for r in __import__("json").loads(
            fixture_bytes("eredes", "consumo-total-nacional_2026-11-03.json")
        )["results"][:4]
    ]
    assert got == pytest.approx(sum(q["total"] for q in quarters) / 1000.0)
    assert 3000 < got < 8000  # plausible national demand in MW, same scale as ENTSO-E load


def test_demand_has_both_sources_hourly(built) -> None:
    _, con, _ = built
    rows = dict(con.execute("select source, count(*) from model.demand group by 1").fetchall())
    assert rows == {"entsoe": 24, "eredes": 24}
    assert con.execute("select distinct resolution_minutes from model.demand").fetchall() == [(60,)]


def test_generation_keeps_direction_and_type(built) -> None:
    _, con, _ = built
    got = set(
        con.execute(
            "select distinct psr_type, direction from model.generation_by_source"
        ).fetchall()
    )
    assert (
        ("B10", "consumption") in got
        and ("B10", "generation") in got
        and ("B16", "generation") in got
    )


# --- clock-change days through the whole pipeline -------------------------------------------


def test_clock_change_days_have_the_right_number_of_modelled_intervals(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    days = ["2026-03-29", "2026-10-25"]
    store = make_raw(web, tmp_path / "d", clock, sleeps, days=days, datasets=[OMIE, EREDES])
    con = build_into(store, tmp_path / "o")
    q = lambda sql: con.execute(sql).fetchall()  # noqa: E731
    per_day = dict(q("select raw_window, count(*) from model.day_ahead_price group by 1"))
    assert per_day == {"2026-03-29": 92, "2026-10-25": 100}  # 23 h and 25 h of quarters
    # Hourly E-REDES: spring day 92 quarters -> 23 hours. Fall day: 96 stamped quarters, but the
    # 00:00-00:45 UTC hour is missing, so 24 hours of the 25 are complete... minus the partial.
    hours = dict(
        q("select raw_window, count(*) from model.demand where source='eredes' group by 1")
    )
    assert hours["2026-03-29"] == 23
    assert hours["2026-10-25"] == 24
    # No hour is duplicated or stamped twice.
    assert q(
        "select count(*) - count(distinct (source, interval_start_utc)) from model.demand"
    ) == [(0,)]


def test_the_phantom_zero_rows_never_reach_the_model(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    store = make_raw(web, tmp_path / "d", clock, sleeps, days=["2026-03-29"], datasets=[EREDES])
    con = build_into(store, tmp_path / "o")
    assert con.execute("select count(*) from model.demand where demand_mw <= 0").fetchone() == (0,)
    reasons = con.execute("select reason, count(*) from meta.rejected group by 1").fetchall()
    assert reasons == [("nonexistent_local_time", 4)]


def test_incomplete_hours_are_left_out_and_listed(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    import json

    doc = json.loads(fixture_bytes("eredes", "consumo-total-nacional_2026-11-03.json"))
    doc["results"] = doc["results"][:-1]  # drop the last quarter: the 23:00 hour has 3 of 4
    doc["total_count"] = len(doc["results"])
    web.set(EREDES, "2026-11-03", body=json.dumps(doc).encode())
    store = make_raw(web, tmp_path / "d", clock, sleeps, days=["2026-11-03"], datasets=[EREDES])
    con = build_into(store, tmp_path / "o")
    assert con.execute("select count(*) from model.demand").fetchone() == (23,)
    assert con.execute("select reason from meta.rejected").fetchall() == [
        ("incomplete_hour (3 of 4 quarters)",)
    ]


# --- overlapping windows --------------------------------------------------------------------


def test_overlapping_entsoe_price_windows_give_one_row_per_interval(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    store = make_raw(
        web,
        tmp_path / "d",
        clock,
        sleeps,
        days=["2026-11-05", "2026-11-06"],
        datasets=["entsoe_day_ahead_price"],
    )
    stg = stage(store).tables["entsoe_day_ahead_price"]
    assert len(stg) == 384  # two windows x 192 intervals, same document served twice
    con = build_into(store, tmp_path / "o")
    assert con.execute(
        "select count(*), count(distinct interval_start_utc) from model.day_ahead_price"
    ).fetchone() == (192, 192)


def test_the_newest_fetch_wins_where_windows_overlap(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    base = fixture_bytes("entsoe", "price_2026-11-05.xml")
    web.set("entsoe_day_ahead_price", "2026-11-05", body=base)
    web.set(
        "entsoe_day_ahead_price",
        "2026-11-06",
        body=base.replace(
            b"<price.amount>262.24</price.amount>", b"<price.amount>999.0</price.amount>"
        ),
    )
    store = make_raw(
        web,
        tmp_path / "d",
        clock,
        sleeps,
        days=["2026-11-05", "2026-11-06"],
        datasets=["entsoe_day_ahead_price"],
    )
    con = build_into(store, tmp_path / "o")
    price, window = con.execute(
        "select price_eur_mwh, raw_window from model.day_ahead_price "
        "where interval_start_utc = '2026-11-04 23:00+00'"
    ).fetchone()
    assert (price, window) == (999.0, "2026-11-06")


# --- revisions ------------------------------------------------------------------------------


def test_a_revision_updates_the_model_and_is_recorded_with_old_and_new_values(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    root = tmp_path / "d"
    ingest(web, root, clock, sleeps, ["2026-11-05"], [OMIE])
    original = fixture_bytes("omie", "marginalpdbcpt_20261105.1")
    revised = original.replace(b"2026;11;05;1;262.24;262.24;", b"2026;11;05;1;270.00;262.24;")
    web.set(OMIE, "2026-11-05", body=revised)
    ingest(web, root, clock, sleeps, ["2026-11-05"], [OMIE])
    store = RawStore(root)
    staged = stage(store)
    assert len(staged.tables[OMIE]) == 96  # staging holds the current version only
    con = build_into(store, tmp_path / "o")
    assert con.execute(
        "select price_eur_mwh from model.day_ahead_price "
        "where interval_start_utc = '2026-11-04 23:00+00'"
    ).fetchone() == (270.0,)
    rev = con.execute(
        "select dataset, field, old_value, new_value, old_sha256, new_sha256, key "
        "from meta.revisions"
    ).fetchall()
    assert rev == [
        (
            OMIE,
            "price_pt_eur_mwh",
            262.24,
            270.0,
            sha256_hex(original),
            sha256_hex(revised),
            '["2026-11-04T23:00:00+00:00"]',
        )
    ]


def test_no_revision_means_an_empty_revisions_table(built) -> None:
    _, con, _ = built
    assert con.execute("select count(*) from meta.revisions").fetchone() == (0,)


# --- lineage --------------------------------------------------------------------------------


def test_every_modelled_row_traces_to_an_existing_raw_file(built, tmp_path: Path) -> None:
    store, con, _ = built
    for table in ("model.demand", "model.day_ahead_price", "model.generation_by_source"):
        orphans = con.execute(
            f"select count(*) from {table} m where not exists "
            "(select 1 from meta.lineage l where l.content_sha256 = m.raw_sha256 and l.is_current)"
        ).fetchone()
        assert orphans == (0,), table
    paths = [p for (p,) in con.execute("select path from meta.lineage").fetchall()]
    assert paths and all((store.root / p).is_file() for p in paths)


def test_a_dashboard_number_can_be_found_in_its_raw_file(built) -> None:
    """Pick a price from the model, follow its hash to the raw file, find the number there."""
    store, con, _ = built
    price, sha, window = con.execute(
        "select price_eur_mwh, raw_sha256, raw_window from model.day_ahead_price "
        "where source = 'omie' and interval_start_utc = '2026-11-04 23:15+00'"
    ).fetchone()
    path = con.execute(
        "select path from meta.lineage where content_sha256 = ? and dataset = ?", [sha, OMIE]
    ).fetchone()[0]
    raw = (store.root / path).read_text()
    assert f"2026;11;05;2;{price};" in raw  # period 2 of the trading day = 23:15 UTC the day before
    assert window == "2026-11-05"


def test_lineage_records_provenance_of_every_version(built) -> None:
    _, con, _ = built
    row = con.execute(
        "select pipeline_version, contract_version, is_current from meta.lineage limit 1"
    ).fetchone()
    assert row == ("test-sha", "1.0.0", True)


def test_batch_row_identifies_the_inputs(built) -> None:
    store, con, _ = built
    (sha,) = con.execute("select raw_state_sha256 from meta.batches").fetchone()
    assert sha == stage(store).raw_state_sha256 and len(sha) == 64


# --- idempotency ----------------------------------------------------------------------------


def _contents(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame]:
    out = {}
    for schema, table in con.execute(
        "select table_schema, table_name from information_schema.tables "
        "where table_schema in ('staging','model','meta') and table_name != 'batches' order by 1, 2"
    ).fetchall():
        out[f"{schema}.{table}"] = con.execute(f"select * from {schema}.{table} order by all").df()
    return out


def test_rebuilding_from_the_same_raw_files_gives_identical_tables(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    store = make_raw(web, tmp_path / "d", clock, sleeps)
    first, second = (
        build_into(store, tmp_path / "a"),
        build_into(RawStore(tmp_path / "d"), tmp_path / "b"),
    )
    a, b = _contents(first), _contents(second)
    assert a.keys() == b.keys() and len(a) >= 11
    for name in a:
        pd.testing.assert_frame_equal(a[name], b[name], obj=name)
    assert (
        first.execute("select raw_state_sha256 from meta.batches").fetchone()
        == second.execute("select raw_state_sha256 from meta.batches").fetchone()
    )


def test_ingesting_again_changes_neither_the_raw_state_nor_the_tables(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    store = make_raw(web, tmp_path / "d", clock, sleeps)
    before = stage(store).raw_state_sha256
    ingest(web, tmp_path / "d", clock, sleeps, ["2026-11-03", "2026-11-05"], ALL5)
    assert stage(RawStore(tmp_path / "d")).raw_state_sha256 == before


# --- empty and broken input -----------------------------------------------------------------


def test_an_empty_raw_layer_builds_empty_tables(tmp_path: Path) -> None:
    con = build_into(RawStore(tmp_path / "nothing"), tmp_path / "o")
    for table in (
        "model.demand",
        "model.day_ahead_price",
        "model.generation_by_source",
        "meta.lineage",
    ):
        assert con.execute(f"select count(*) from {table}").fetchone() == (0,)


def test_a_window_that_cannot_be_parsed_is_reported_not_swallowed(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    lines = fixture_bytes("omie", "marginalpdbcpt_20261105.1").decode().splitlines()
    web.set(
        OMIE, "2026-11-05", body=("\n".join(lines[:50] + lines[51:]) + "\n").encode()
    )  # one period missing
    store = make_raw(web, tmp_path / "d", clock, sleeps, days=["2026-11-05"], datasets=[OMIE])
    staged = stage(store)
    assert len(staged.errors) == 1 and "expected periods" in staged.errors.iloc[0]["message"]
    assert len(staged.tables[OMIE]) == 0  # nothing from the bad window leaks into staging


def test_build_refuses_to_write_over_an_existing_folder(tmp_path: Path) -> None:
    (tmp_path / "o").mkdir()
    with pytest.raises(FileExistsError):
        build(stage(RawStore(tmp_path / "x")), tmp_path / "o")


def test_swap_directory_replaces_the_old_build_completely(tmp_path: Path) -> None:
    old, new = tmp_path / "serving", tmp_path / "serving.build"
    old.mkdir()
    (old / "stale.txt").write_text("old")
    new.mkdir()
    (new / "fresh.txt").write_text("new")
    swap_directory(new, old)
    assert [p.name for p in old.iterdir()] == ["fresh.txt"] and not new.exists()
    assert not (tmp_path / "serving.old").exists()


# --- CLI ------------------------------------------------------------------------------------


def test_cli_transform_publishes_serving_and_reports_counts(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    root = tmp_path / "d"
    make_raw(web, root, clock, sleeps)
    result = CliRunner().invoke(app, ["transform", "--data-dir", str(root)])
    assert result.exit_code == 0, result.output
    assert "model.demand" in result.stdout and (root / "serving" / "pt_energy.duckdb").exists()
    assert not (root / "serving.build").exists()


def test_cli_transform_keeps_the_last_good_build_when_parsing_fails(
    web: FakeWeb, tmp_path: Path, clock: FakeClock, sleeps: list[float]
) -> None:
    root = tmp_path / "d"
    make_raw(web, root, clock, sleeps, days=["2026-11-05"], datasets=[OMIE])
    assert CliRunner().invoke(app, ["transform", "--data-dir", str(root)]).exit_code == 0
    good = (root / "serving" / "model" / "day_ahead_price.parquet").read_bytes()

    lines = fixture_bytes("omie", "marginalpdbcpt_20261105.1").decode().splitlines()
    web.set(OMIE, "2026-11-05", body=("\n".join(lines[:5] + lines[6:]) + "\n").encode())
    ingest(web, root, clock, sleeps, ["2026-11-05"], [OMIE])  # a bad revision arrives

    result = CliRunner().invoke(app, ["transform", "--data-dir", str(root)])
    assert result.exit_code == 1 and "PARSE ERROR omie_day_ahead_price 2026-11-05" in result.output
    assert (root / "serving" / "model" / "day_ahead_price.parquet").read_bytes() == good


def test_the_built_at_time_is_recorded_in_utc(built) -> None:
    _, con, _ = built
    kind, text = con.execute(
        "select typeof(built_at), built_at::varchar from meta.batches"
    ).fetchone()
    assert kind == "TIMESTAMP WITH TIME ZONE" and text.endswith("+00")


# --- determinism on larger data (found on the real 30-day run) ------------------------------


def _big_eredes_staging(hours: int = 6000) -> pd.DataFrame:
    import random

    rng = random.Random(7)
    start = pd.Timestamp("2025-01-01", tz="UTC")
    rows = [
        {
            "interval_start_utc": start + pd.Timedelta(minutes=15 * i),
            "low_voltage_kwh": 0.0,
            "medium_voltage_kwh": 0.0,
            "high_voltage_kwh": 0.0,
            "very_high_voltage_kwh": 0.0,
            "total_kwh": rng.uniform(1e6, 1.6e6),
            "resolution_minutes": 15,
            "raw_window": f"w{i // 96}",
            "raw_sha256": "a" * 64,
            "fetched_at": pd.Timestamp("2026-01-01", tz="UTC"),
        }
        for i in range(hours * 4)
    ]
    return pd.DataFrame(rows)


def test_hourly_sums_do_not_depend_on_thread_scheduling(tmp_path: Path) -> None:
    empty = stage(RawStore(tmp_path / "none"))
    empty.tables[EREDES] = _big_eredes_staging()
    results = []
    for name in ("a", "b", "c"):
        build(empty, tmp_path / name)
        con = duckdb.connect(str(tmp_path / name / "pt_energy.duckdb"), read_only=True)
        results.append(
            con.execute(
                "select md5(string_agg(demand_mw::varchar, '|' order by "
                "interval_start_utc)) from model.demand"
            ).fetchone()
        )
    assert results[0] == results[1] == results[2]
