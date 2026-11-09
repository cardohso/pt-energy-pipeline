"""Parsers: one raw payload in, rows with UTC timestamps out.

Every timestamp leaving this module is an absolute UTC instant. Local-time conventions of the
publishers (OMIE: Spanish trading day; E-REDES: Lisbon labels) are resolved here and nowhere else.
A parser that cannot make sense of a payload raises ``ParseError``; it never guesses.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

Row = dict[str, Any]

MADRID = ZoneInfo("Europe/Madrid")
LISBON = ZoneInfo("Europe/Lisbon")
OMIE_15_MIN_FROM = date(2025, 10, 1)  # OMIE moved the day-ahead market to 15 minutes this day


class ParseError(ValueError):
    """The payload is not what the contract says it should be."""


def _local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC instants of local midnight at the start and end of ``day``."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz).astimezone(UTC)
    return start, end


# --- OMIE -----------------------------------------------------------------------------------


def parse_omie(body: bytes) -> tuple[list[Row], list[Row]]:
    """OMIE ``marginalpdbcpt`` file: ``year;month;day;period;price_PT;price_ES;`` per period.

    Periods count elapsed time from local (Spanish) midnight, so a 23- or 25-hour day simply has
    fewer or more periods. The first column is Portugal (checked against ENTSO-E on 2026-11-05/06).
    """
    lines = body.decode("utf-8").splitlines()
    if not lines or not lines[0].startswith("MARGINALPDBCPT;"):
        raise ParseError("not an OMIE marginalpdbcpt file")
    parsed: list[tuple[int, date, float]] = []
    for line in lines[1:]:
        if not line or line.startswith("*"):
            continue
        parts = line.rstrip(";").split(";")
        if len(parts) != 6:
            raise ParseError(f"OMIE line has {len(parts)} fields, expected 6: {line[:60]!r}")
        try:
            parsed.append(
                (int(parts[3]), date(int(parts[0]), int(parts[1]), int(parts[2])), float(parts[4]))
            )
        except ValueError as exc:
            raise ParseError(f"unreadable OMIE line {line[:60]!r}") from exc
    if not parsed:
        raise ParseError("OMIE file has no price rows")
    days = {d for _, d, _ in parsed}
    if len(days) != 1:
        raise ParseError(f"OMIE file mixes trading days: {sorted(days)}")
    (day,) = days
    resolution = 15 if day >= OMIE_15_MIN_FROM else 60
    start, end = _local_day_bounds(day, MADRID)
    expected = int((end - start).total_seconds() // 60 // resolution)
    periods = [p for p, _, _ in parsed]
    if periods != list(range(1, expected + 1)):
        raise ParseError(
            f"OMIE {day}: expected periods 1..{expected} ({resolution}-minute day), "
            f"got {len(periods)} starting {periods[:1]} ending {periods[-1:]}"
        )
    rows = [
        {
            "interval_start_utc": start + timedelta(minutes=resolution * (p - 1)),
            "price_pt_eur_mwh": price,
            "resolution_minutes": resolution,
        }
        for p, _, price in parsed
    ]
    return rows, []


# --- E-REDES --------------------------------------------------------------------------------

_EREDES_FIELDS = {
    "bt": "low_voltage_kwh",
    "mt": "medium_voltage_kwh",
    "at": "high_voltage_kwh",
    "mat": "very_high_voltage_kwh",
    "total": "total_kwh",
}


def _lisbon_candidates(day: str, clock: str) -> tuple[bool, set[datetime]]:
    """Whether Lisbon wall time ``day clock`` exists, and its possible UTC instants."""
    naive = datetime.fromisoformat(f"{day}T{clock}:00")
    first = naive.replace(tzinfo=LISBON, fold=0)
    exists = first.astimezone(UTC).astimezone(LISBON).replace(tzinfo=None) == naive
    candidates = {naive.replace(tzinfo=LISBON, fold=f).astimezone(UTC) for f in (0, 1)}
    return exists, candidates


def parse_eredes(body: bytes) -> tuple[list[Row], list[Row]]:
    """E-REDES ``consumo-total-nacional`` records. Returns (rows, rejected rows).

    ``datahora`` is the UTC start of the 15-minute interval. Rejected, never repaired:

    - ``no_timestamp``: ``datahora`` is null (seen on the fall-back day: four extra rows labelled
      24:15 to 25:00 whose slot cannot be known);
    - ``nonexistent_local_time``: label that does not exist in Lisbon (spring-forward day: four
      rows with value 0 that collide with the real 01:00 to 01:45 UTC rows);
    - ``timestamp_mismatch``: ``datahora`` disagrees with the local date and time label;
    - ``conflicting_duplicate``: the same timestamp appears more than once with different values
      (seen on 2026-11-01, not a clock-change day); all copies are rejected because there is no
      way to tell which one is right. Repeats with identical values are kept once.
    """
    try:
        records = json.loads(body)["results"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseError("not an E-REDES records response") from exc
    rows: list[Row] = []
    rejected: list[Row] = []
    for rec in records:
        info = {"date": rec.get("date"), "time": rec.get("time"), "datahora": rec.get("datahora")}
        info["total_kwh"] = rec.get("total")

        def reject(reason: str, info: Row = info) -> None:
            rejected.append({**info, "reason": reason})

        if rec.get("datahora") is None:
            reject("no_timestamp")
            continue
        try:
            exists, candidates = _lisbon_candidates(rec["date"], rec["time"])
        except (ValueError, KeyError, TypeError):
            reject("unreadable_local_time")
            continue
        if not exists:
            reject("nonexistent_local_time")
            continue
        stamp = datetime.fromisoformat(rec["datahora"]).astimezone(UTC)
        if stamp not in candidates:
            reject("timestamp_mismatch")
            continue
        try:
            values = {name: float(rec[key]) for key, name in _EREDES_FIELDS.items()}
        except (KeyError, TypeError, ValueError) as exc:
            raise ParseError(
                f"E-REDES record at {rec.get('datahora')} has no usable values"
            ) from exc
        rows.append({"interval_start_utc": stamp, **values, "resolution_minutes": 15})
    by_stamp: dict[datetime, list[Row]] = {}
    for row in rows:
        by_stamp.setdefault(row["interval_start_utc"], []).append(row)
    kept: list[Row] = []
    for stamp, group in by_stamp.items():
        if all(g == group[0] for g in group):
            kept.append(group[0])
            continue
        for g in group:
            rejected.append(
                {
                    "date": stamp.date().isoformat(),
                    "time": None,
                    "datahora": stamp.isoformat(),
                    "total_kwh": g["total_kwh"],
                    "reason": "conflicting_duplicate",
                }
            )
    return kept, rejected


# --- ENTSO-E --------------------------------------------------------------------------------

_RES = re.compile(r"^PT(\d+)([MH])$")


def _tag(element: Any) -> str:
    return str(element.tag).rsplit("}", 1)[-1]


def _child_text(element: Any, name: str) -> str | None:
    for child in element:
        if _tag(child) == name:
            return str(child.text) if child.text is not None else None
    return None


def _resolution_minutes(text: str | None) -> int:
    m = _RES.match(text or "")
    if not m:
        raise ParseError(f"unknown resolution {text!r}")
    return int(m.group(1)) * (60 if m.group(2) == "H" else 1)


def _entsoe_instant(text: str | None) -> datetime:
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ParseError(f"bad ENTSO-E timestamp {text!r}") from exc


def _expand_period(period: Any, value_tag: str) -> tuple[int, list[tuple[datetime, float]]]:
    """All slots of one ``Period``. A missing position repeats the previous value (curve type A03).

    The period's declared end is where the publisher stops: for a day still in progress it is
    truncated to the last published interval, so filling up to it never invents future data.
    """
    interval = next(c for c in period if _tag(c) == "timeInterval")
    start = _entsoe_instant(_child_text(interval, "start"))
    end = _entsoe_instant(_child_text(interval, "end"))
    minutes = _resolution_minutes(_child_text(period, "resolution"))
    slots = int((end - start).total_seconds() // 60 // minutes)
    points: dict[int, float] = {}
    for point in (c for c in period if _tag(c) == "Point"):
        position = int(_child_text(point, "position") or 0)
        points[position] = float(_child_text(point, value_tag) or "nan")
    if not points or 1 not in points:
        raise ParseError("ENTSO-E period has no point at position 1")
    if max(points) > slots:
        raise ParseError(f"ENTSO-E point {max(points)} is beyond the {slots}-slot period")
    out: list[tuple[datetime, float]] = []
    last = points[1]
    for position in range(1, slots + 1):
        last = points.get(position, last)
        out.append((start + timedelta(minutes=minutes * (position - 1)), last))
    return minutes, out


_POWER_UNITS = {"quantity_Measure_Unit.name": "MAW"}
_PRICE_UNITS = {"price_Measure_Unit.name": "MWH", "currency_Unit.name": "EUR"}


def _check_units(series: Any, expected: dict[str, str]) -> None:
    """A changed or missing unit is a schema change: the numbers would silently change meaning."""
    for tag, wanted in expected.items():
        got = _child_text(series, tag)
        if got != wanted:
            raise ParseError(
                f"ENTSO-E unit changed: {tag} is {got!r}, the contract says {wanted!r}"
            )


def _entsoe_series(
    body: bytes, value_tag: str, units: dict[str, str]
) -> list[tuple[Any, int, datetime, float]]:
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ParseError("ENTSO-E payload is not valid or safe XML") from exc
    if "Acknowledgement" in _tag(root):
        raise ParseError("ENTSO-E payload is an acknowledgement, not data")
    out: list[tuple[Any, int, datetime, float]] = []
    for series in (c for c in root if _tag(c) == "TimeSeries"):
        _check_units(series, units)
        for period in (c for c in series if _tag(c) == "Period"):
            minutes, slots = _expand_period(period, value_tag)
            out += [(series, minutes, at, value) for at, value in slots]
    if not out:
        raise ParseError("ENTSO-E payload has no time series")
    return out


def _no_duplicates(rows: list[Row], key: tuple[str, ...]) -> list[Row]:
    """Drop exact repeats (overlapping periods), refuse conflicting ones."""
    seen: dict[tuple[Any, ...], Row] = {}
    for row in rows:
        k = tuple(row[c] for c in key)
        if k in seen and seen[k] != row:
            raise ParseError(f"conflicting values for {k} inside one ENTSO-E document")
        seen[k] = row
    return list(seen.values())


def parse_entsoe_load(body: bytes) -> tuple[list[Row], list[Row]]:
    rows = [
        {"interval_start_utc": at, "load_mw": value, "resolution_minutes": minutes}
        for _, minutes, at, value in _entsoe_series(body, "quantity", _POWER_UNITS)
    ]
    return _no_duplicates(rows, ("interval_start_utc",)), []


def parse_entsoe_generation(body: bytes) -> tuple[list[Row], list[Row]]:
    rows: list[Row] = []
    for series, minutes, at, value in _entsoe_series(body, "quantity", _POWER_UNITS):
        psr = next((_child_text(c, "psrType") for c in series if _tag(c) == "MktPSRType"), None)
        names = {_tag(c) for c in series}
        if psr is None or not (names & {"inBiddingZone_Domain.mRID", "outBiddingZone_Domain.mRID"}):
            raise ParseError("generation series without production type or direction")
        direction = "generation" if "inBiddingZone_Domain.mRID" in names else "consumption"
        rows.append(
            {
                "interval_start_utc": at,
                "psr_type": psr,
                "direction": direction,
                "generation_mw": value,
                "resolution_minutes": minutes,
            }
        )
    return _no_duplicates(rows, ("interval_start_utc", "psr_type", "direction")), []


def parse_entsoe_price(body: bytes) -> tuple[list[Row], list[Row]]:
    rows = [
        {"interval_start_utc": at, "price_eur_mwh": value, "resolution_minutes": minutes}
        for _, minutes, at, value in _entsoe_series(body, "price.amount", _PRICE_UNITS)
    ]
    return _no_duplicates(rows, ("interval_start_utc",)), []
