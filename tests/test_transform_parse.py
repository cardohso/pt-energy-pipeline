"""Parsers and time zones: every clock-change day, every source. No network."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import fixture_bytes
from pt_energy.transform.parse import (
    ParseError,
    parse_entsoe_generation,
    parse_entsoe_load,
    parse_entsoe_price,
    parse_eredes,
    parse_omie,
)

MADRID, LISBON = ZoneInfo("Europe/Madrid"), ZoneInfo("Europe/Lisbon")


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def starts(rows: list[dict]) -> list[datetime]:
    return [r["interval_start_utc"] for r in rows]


def assert_regular(rows: list[dict], minutes: int) -> None:
    """Strictly increasing, no gaps, no repeats."""
    ts = starts(rows)
    assert all(b - a == timedelta(minutes=minutes) for a, b in itertools.pairwise(ts))


# --- OMIE: trading days follow Spanish time -------------------------------------------------


@pytest.mark.parametrize(
    ("file", "count", "minutes", "first", "last"),
    [
        # 15-minute era
        ("20261105", 96, 15, utc(2026, 11, 4, 23), utc(2026, 11, 5, 22, 45)),  # winter, UTC+1
        (
            "20260329",
            92,
            15,
            utc(2026, 3, 28, 23),
            utc(2026, 3, 29, 21, 45),
        ),  # spring forward: 23 h
        ("20261025", 100, 15, utc(2026, 10, 24, 22), utc(2026, 10, 25, 22, 45)),  # fall back: 25 h
        # hourly era
        ("20250930", 24, 60, utc(2025, 9, 29, 22), utc(2025, 9, 30, 21)),  # last hourly day
        ("20250330", 23, 60, utc(2025, 3, 29, 23), utc(2025, 3, 30, 21)),  # spring forward
        ("20241027", 25, 60, utc(2024, 10, 26, 22), utc(2024, 10, 27, 22)),  # fall back
    ],
)
def test_omie_day_lengths_and_utc_boundaries(
    file: str, count: int, minutes: int, first: datetime, last: datetime
) -> None:
    rows, rejected = parse_omie(fixture_bytes("omie", f"marginalpdbcpt_{file}.1"))
    assert (len(rows), rejected) == (count, [])
    assert starts(rows)[0] == first and starts(rows)[-1] == last
    assert {r["resolution_minutes"] for r in rows} == {minutes}
    assert_regular(rows, minutes)


@pytest.mark.parametrize("file", ["20261105", "20260329", "20261025", "20250330", "20241027"])
def test_omie_first_interval_is_local_midnight_in_madrid(file: str) -> None:
    rows, _ = parse_omie(fixture_bytes("omie", f"marginalpdbcpt_{file}.1"))
    y, m, d = int(file[:4]), int(file[4:6]), int(file[6:])
    assert starts(rows)[0] == datetime(y, m, d, tzinfo=MADRID).astimezone(UTC)


def test_omie_values_are_kept_and_the_first_column_is_portugal() -> None:
    rows, _ = parse_omie(fixture_bytes("omie", "marginalpdbcpt_20261105.1"))
    assert rows[0]["price_pt_eur_mwh"] == 262.24 and rows[1]["price_pt_eur_mwh"] == 255.07


def _omie_file(day: str, periods: int) -> bytes:
    y, m, d = day.split("-")
    lines = ["MARGINALPDBCPT;"] + [f"{y};{m};{d};{p};10.5;10.5;" for p in range(1, periods + 1)]
    return ("\n".join([*lines, "*"]) + "\n").encode()


def test_omie_switch_to_15_minutes_happens_on_2025_10_01() -> None:
    rows, _ = parse_omie(_omie_file("2025-10-01", 96))
    assert {r["resolution_minutes"] for r in rows} == {15}
    with pytest.raises(ParseError, match=r"expected periods 1\.\.24"):
        parse_omie(_omie_file("2025-09-30", 96))  # 15-minute count on an hourly day
    with pytest.raises(ParseError, match=r"expected periods 1\.\.96"):
        parse_omie(_omie_file("2025-10-01", 24))  # hourly count on a 15-minute day


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"", "not an OMIE"),
        (b"<html></html>", "not an OMIE"),
        (b"MARGINALPDBCPT;\n*\n", "no price rows"),
        (b"MARGINALPDBCPT;\n2026;11;05;1;abc;abc;\n", "unreadable OMIE line"),
        (b"MARGINALPDBCPT;\n2026;11;05;1;1.0;\n", "expected 6"),
        (b"MARGINALPDBCPT;\n2026;11;05;1;1;1;\n2026;11;06;1;1;1;\n", "mixes trading days"),
    ],
)
def test_omie_rejects_broken_files(body: bytes, message: str) -> None:
    with pytest.raises(ParseError, match=message):
        parse_omie(body)


def test_omie_a_missing_period_is_an_error_not_a_silent_gap() -> None:
    lines = _omie_file("2026-11-05", 96).decode().splitlines()
    del lines[10]
    with pytest.raises(ParseError, match=r"expected periods 1\.\.96"):
        parse_omie(("\n".join(lines) + "\n").encode())


# --- E-REDES: Lisbon labels, UTC timestamps -------------------------------------------------


def _eredes(day: str) -> bytes:
    return fixture_bytes("eredes", f"consumo-total-nacional_{day}.json")


def test_eredes_normal_day() -> None:
    rows, rejected = parse_eredes(_eredes("2026-11-03"))
    assert (len(rows), rejected) == (96, [])
    assert starts(rows)[0] == utc(2026, 11, 3) and starts(rows)[-1] == utc(2026, 11, 3, 23, 45)
    assert_regular(rows, 15)


def test_eredes_spring_forward_drops_the_four_phantom_rows_and_keeps_the_real_ones() -> None:
    rows, rejected = parse_eredes(_eredes("2026-03-29"))
    assert [r["reason"] for r in rejected] == ["nonexistent_local_time"] * 4
    assert sorted(r["time"] for r in rejected) == ["01:00", "01:15", "01:30", "01:45"]
    assert all(r["total_kwh"] == 0 for r in rejected)
    assert len(rows) == 92
    assert starts(rows)[0] == utc(2026, 3, 29) and starts(rows)[-1] == utc(2026, 3, 29, 22, 45)
    assert_regular(rows, 15)
    assert all(r["total_kwh"] > 0 for r in rows)  # no zeros left


def test_eredes_fall_back_rejects_rows_without_timestamp_and_leaves_the_gap() -> None:
    rows, rejected = parse_eredes(_eredes("2026-10-25"))
    assert sorted(r["time"] for r in rejected) == ["24:15", "24:30", "24:45", "25:00"]
    assert {r["reason"] for r in rejected} == {"no_timestamp"}
    assert len(rows) == 96 and starts(rows)[0] == utc(2026, 10, 24, 23)
    ts = starts(rows)
    missing = [t for t in (utc(2026, 10, 25, 0, m) for m in (0, 15, 30, 45)) if t not in ts]
    assert len(missing) == 4  # the slots those rows probably belong to stay empty


def test_eredes_local_label_and_utc_stamp_must_agree() -> None:
    doc = json.loads(_eredes("2026-11-03"))
    doc["results"][5]["datahora"] = "2026-11-03T09:00:00+00:00"  # label says 01:15
    rows, rejected = parse_eredes(json.dumps(doc).encode())
    assert len(rows) == 95 and [r["reason"] for r in rejected] == ["timestamp_mismatch"]


def test_eredes_summer_time_labels_are_converted() -> None:
    doc = json.loads(_eredes("2026-11-03"))
    first = doc["results"][0]
    first.update(date="2026-07-01", time="00:00", datahora="2026-06-30T23:00:00+00:00")
    rows, rejected = parse_eredes(json.dumps({"results": [first]}).encode())
    assert rejected == [] and starts(rows) == [utc(2026, 6, 30, 23)]


def test_eredes_duplicates_with_conflicting_values_are_all_rejected() -> None:
    doc = json.loads(_eredes("2026-11-03"))
    twin = dict(doc["results"][4], total=1.0)  # same timestamp, different value
    doc["results"].insert(5, twin)
    rows, rejected = parse_eredes(json.dumps(doc).encode())
    assert len(rows) == 95  # the timestamp is gone entirely: we cannot tell which copy is right
    assert [r["reason"] for r in rejected] == ["conflicting_duplicate"] * 2


def test_eredes_identical_duplicates_are_kept_once() -> None:
    doc = json.loads(_eredes("2026-11-03"))
    doc["results"].insert(5, dict(doc["results"][4]))
    rows, rejected = parse_eredes(json.dumps(doc).encode())
    assert (len(rows), rejected) == (96, [])


@pytest.mark.parametrize("body", [b"not json", b"{}", b"[]"])
def test_eredes_rejects_non_records(body: bytes) -> None:
    with pytest.raises(ParseError):
        parse_eredes(body)


def test_eredes_a_row_without_values_is_an_error() -> None:
    doc = json.loads(_eredes("2026-11-03"))
    del doc["results"][0]["total"]
    with pytest.raises(ParseError, match="no usable values"):
        parse_eredes(json.dumps(doc).encode())


# --- ENTSO-E: UTC periods, omitted repeats --------------------------------------------------


def gl_doc(series: str) -> bytes:
    ns = "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"
    return f'<?xml version="1.0"?><GL_MarketDocument xmlns="{ns}"><mRID>x</mRID>{series}</GL_MarketDocument>'.encode()


def series_xml(
    start: str,
    end: str,
    res: str,
    points: dict[int, float],
    psr: str = "B16",
    zone: str = "in",
    unit: str = "MAW",
) -> str:
    pts = "".join(
        f"<Point><position>{p}</position><quantity>{v}</quantity></Point>"
        for p, v in points.items()
    )
    return (
        f"<TimeSeries><mRID>1</mRID><{zone}BiddingZone_Domain.mRID>PT</{zone}BiddingZone_Domain.mRID>"
        f"<quantity_Measure_Unit.name>{unit}</quantity_Measure_Unit.name>"
        f"<MktPSRType><psrType>{psr}</psrType></MktPSRType><curveType>A03</curveType>"
        f"<Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
        f"<resolution>{res}</resolution>{pts}</Period></TimeSeries>"
    )


def test_entsoe_load_fixture() -> None:
    rows, _ = parse_entsoe_load(fixture_bytes("entsoe", "load_2026-11-05.xml"))
    assert len(rows) == 24 and {r["resolution_minutes"] for r in rows} == {60}
    assert starts(rows)[0] == utc(2026, 11, 5) and starts(rows)[-1] == utc(2026, 11, 5, 23)
    assert_regular(rows, 60)


def test_entsoe_omitted_points_repeat_the_previous_value() -> None:
    xml = gl_doc(series_xml("2026-11-05T00:00Z", "2026-11-05T06:00Z", "PT60M", {1: 10.0, 4: 40.0}))
    rows, _ = parse_entsoe_generation(xml)
    assert [r["generation_mw"] for r in rows] == [10.0, 10.0, 10.0, 40.0, 40.0, 40.0]


def test_entsoe_partial_day_stops_at_the_published_end_and_invents_nothing() -> None:
    """For today the publisher truncates the period; filling up to its end adds no future data."""
    xml = gl_doc(series_xml("2026-11-09T00:00Z", "2026-11-09T13:00Z", "PT60M", {1: 5.0, 7: 6.0}))
    rows, _ = parse_entsoe_generation(xml)
    assert len(rows) == 13 and starts(rows)[-1] == utc(2026, 11, 9, 12)


def test_entsoe_generation_fixture_directions_and_types() -> None:
    rows, _ = parse_entsoe_generation(fixture_bytes("entsoe", "generation_2026-11-05.xml"))
    assert len(rows) == 5 * 24
    pairs = {(r["psr_type"], r["direction"]) for r in rows}
    assert pairs == {
        ("B16", "generation"),
        ("B19", "generation"),
        ("B04", "generation"),
        ("B10", "generation"),
        ("B10", "consumption"),
    }


def test_entsoe_price_fixture_has_two_overlapping_central_european_days() -> None:
    rows, _ = parse_entsoe_price(fixture_bytes("entsoe", "price_2026-11-05.xml"))
    assert len(rows) == 192 and {r["resolution_minutes"] for r in rows} == {15}
    assert starts(rows)[0] == utc(2026, 11, 4, 23) and starts(rows)[-1] == utc(2026, 11, 6, 22, 45)
    assert_regular(rows, 15)
    assert rows[0]["price_eur_mwh"] == 262.24  # same as OMIE's first period for that day


@pytest.mark.parametrize(
    ("start", "end", "slots"),
    [
        ("2026-03-28T23:00Z", "2026-03-29T22:00Z", 92),  # Central European spring-forward day
        ("2026-10-24T22:00Z", "2026-10-25T23:00Z", 100),  # fall-back day
        ("2026-11-04T23:00Z", "2026-11-05T23:00Z", 96),
    ],
)
def test_entsoe_price_day_length_follows_the_period_not_a_fixed_96(
    start: str, end: str, slots: int
) -> None:
    pts = "".join("<Point><position>1</position><price.amount>1.5</price.amount></Point>")
    ns = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"
    xml = (
        f'<Publication_MarketDocument xmlns="{ns}"><TimeSeries><currency_Unit.name>EUR</currency_Unit.name><price_Measure_Unit.name>MWH</price_Measure_Unit.name><Period><timeInterval><start>{start}</start>'
        f"<end>{end}</end></timeInterval><resolution>PT15M</resolution>{pts}</Period></TimeSeries>"
        "</Publication_MarketDocument>"
    ).encode()
    rows, _ = parse_entsoe_price(xml)
    assert len(rows) == slots and starts(rows)[0] == datetime.fromisoformat(
        start.replace("Z", "+00:00")
    )
    assert_regular(rows, 15)


def test_entsoe_hourly_load_on_a_clock_change_day_is_24_utc_hours() -> None:
    """ENTSO-E load and generation use UTC days, so the local 23- or 25-hour day does not matter."""
    for day in ("2026-03-29", "2026-10-25"):
        nxt = (datetime.fromisoformat(day) + timedelta(days=1)).strftime("%Y-%m-%d")
        xml = gl_doc(series_xml(f"{day}T00:00Z", f"{nxt}T00:00Z", "PT60M", {1: 1.0}, zone="out"))
        rows, _ = parse_entsoe_load(xml)
        assert len(rows) == 24 and starts(rows)[0] == datetime.fromisoformat(day).replace(
            tzinfo=UTC
        )


def test_entsoe_conflicting_values_for_one_interval_are_an_error() -> None:
    twice = series_xml("2026-11-05T00:00Z", "2026-11-05T02:00Z", "PT60M", {1: 1.0}) + series_xml(
        "2026-11-05T01:00Z", "2026-11-05T03:00Z", "PT60M", {1: 9.0}
    )
    with pytest.raises(ParseError, match="conflicting values"):
        parse_entsoe_generation(gl_doc(twice))


def test_entsoe_identical_overlap_is_deduplicated() -> None:
    twice = series_xml("2026-11-05T00:00Z", "2026-11-05T02:00Z", "PT60M", {1: 1.0}) + series_xml(
        "2026-11-05T01:00Z", "2026-11-05T03:00Z", "PT60M", {1: 1.0}
    )
    rows, _ = parse_entsoe_generation(gl_doc(twice))
    assert len(rows) == 3


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"<not xml", "not valid or safe XML"),
        (fixture_bytes("entsoe", "ack_no_data.xml"), "acknowledgement"),
        (gl_doc(""), "no time series"),
        (
            gl_doc(series_xml("2026-11-05T00:00Z", "2026-11-05T03:00Z", "PT60M", {2: 1.0})),
            "position 1",
        ),
        (
            gl_doc(series_xml("2026-11-05T00:00Z", "2026-11-05T03:00Z", "PT60M", {1: 1.0, 9: 1.0})),
            "beyond",
        ),
        (
            gl_doc(series_xml("2026-11-05T00:00Z", "2026-11-05T03:00Z", "PT7S", {1: 1.0})),
            "unknown resolution",
        ),
        (
            gl_doc(series_xml("yesterday", "2026-11-05T03:00Z", "PT60M", {1: 1.0})),
            "bad ENTSO-E timestamp",
        ),
    ],
)
def test_entsoe_rejects_broken_documents(body: bytes, message: str) -> None:
    with pytest.raises(ParseError, match=message):
        parse_entsoe_generation(body)


def test_entsoe_generation_series_without_direction_is_an_error() -> None:
    xml = gl_doc(
        "<TimeSeries><quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name><MktPSRType><psrType>B16</psrType></MktPSRType><Period><timeInterval>"
        "<start>2026-11-05T00:00Z</start><end>2026-11-05T01:00Z</end></timeInterval>"
        "<resolution>PT60M</resolution><Point><position>1</position><quantity>1</quantity></Point>"
        "</Period></TimeSeries>"
    )
    with pytest.raises(ParseError, match="without production type or direction"):
        parse_entsoe_generation(xml)


def test_entsoe_entity_expansion_attack_is_refused() -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        b"<GL_MarketDocument>&b;</GL_MarketDocument>"
    )
    with pytest.raises(ParseError, match="not valid or safe XML"):
        parse_entsoe_load(bomb)


# --- units: a changed unit is a schema change, never a silent re-interpretation --------------


@pytest.mark.parametrize("unit", ["KAW", "MWH", "", "mw"])
def test_entsoe_power_series_with_another_unit_is_refused(unit: str) -> None:
    xml = gl_doc(series_xml("2026-11-05T00:00Z", "2026-11-05T02:00Z", "PT60M", {1: 1.0}, unit=unit))
    with pytest.raises(ParseError, match="unit changed"):
        parse_entsoe_generation(xml)
    with pytest.raises(ParseError, match="unit changed"):
        parse_entsoe_load(xml)


def test_entsoe_price_in_another_currency_or_unit_is_refused() -> None:
    good = fixture_bytes("entsoe", "price_2026-11-05.xml")
    for old, new in (
        (b"<currency_Unit.name>EUR<", b"<currency_Unit.name>GBP<"),
        (b"<price_Measure_Unit.name>MWH<", b"<price_Measure_Unit.name>KWH<"),
    ):
        assert old in good
        with pytest.raises(ParseError, match="unit changed"):
            parse_entsoe_price(good.replace(old, new))


def test_entsoe_series_without_a_unit_is_refused() -> None:
    xml = gl_doc(series_xml("2026-11-05T00:00Z", "2026-11-05T02:00Z", "PT60M", {1: 1.0}))
    xml = xml.replace(b"<quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>", b"")
    with pytest.raises(ParseError, match="unit changed"):
        parse_entsoe_load(xml)
