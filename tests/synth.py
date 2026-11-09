"""A small, deterministic synthetic world: five internally consistent feeds, no network.

The same underlying numbers feed every source, the way the real ones relate: OMIE and ENTSO-E
publish the same price, E-REDES consumption is about 1.5% below ENTSO-E load, and every
publisher's time quirks that matter (Madrid trading days, Lisbon labels, Central European price
days, truncated current day, publication lag) are reproduced. Daily variation is realistic
(weekday pattern, windy and calm days, +-4% noise) so that "normal" can be tested.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

MADRID, LISBON = ZoneInfo("Europe/Madrid"), ZoneInfo("Europe/Lisbon")
HDR = '<?xml version="1.0" encoding="utf-8"?>\n'
ACK = HDR + (
    "<Acknowledgement_MarketDocument "
    'xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0">'
    "<mRID>x</mRID><Reason><code>999</code><text>No matching data found</text></Reason>"
    "</Acknowledgement_MarketDocument>\n"
)
# (psr type, direction, base MW, is solar)
SERIES = [
    ("B01", "generation", 300, False),
    ("B04", "generation", 900, False),
    ("B10", "generation", 400, False),
    ("B10", "consumption", 150, False),
    ("B11", "generation", 300, False),
    ("B12", "generation", 120, False),
    ("B16", "generation", 1500, True),
    ("B18", "generation", 10, False),
    ("B19", "generation", 1400, False),
    ("B20", "generation", 5, False),
]
FMT = "%Y-%m-%dT%H:%MZ"


def _noise(seed: int, *key: object, spread: float) -> float:
    return random.Random(f"{seed}-{'-'.join(map(str, key))}").uniform(-spread, spread)


@dataclass(frozen=True)
class World:
    as_of: datetime  # "now" as the publishers see it
    seed: int = 1
    eredes_lag_days: int = 5

    # -- the underlying physical story ----------------------------------------------------------
    def day_factor(self, d: date) -> float:
        weekend = -0.08 if d.weekday() >= 5 else 0.0
        return 1.0 + weekend + _noise(self.seed, "day", d, spread=0.04)

    def load_mw(self, hour: datetime) -> float:
        shape = 1 + 0.18 * math.sin(2 * math.pi * (hour.hour - 6) / 24)
        return 5500 * self.day_factor(hour.date()) * shape

    def gen_mw(self, psr: str, base: float, solar: bool, hour: datetime) -> float:
        if solar:
            sun = (
                max(0.0, math.sin(math.pi * (hour.hour - 6.5) / 11)) if 7 <= hour.hour <= 17 else 0
            )
            return round(base * sun * (1 + _noise(self.seed, "sun", hour.date(), spread=0.3)), 1)
        if psr in ("B19", "B18"):
            drift = _noise(self.seed, "wind", hour.date(), spread=3)
            factor = 1 + 0.5 * math.sin(hour.hour / 5 + drift)
        else:
            factor = self.day_factor(hour.date())
        return round(base * factor * (1 + 0.02 * math.sin(hour.hour)), 1)

    def price(self, slot_start: datetime) -> float:
        local = slot_start.astimezone(MADRID)
        level = 110 * (1 + _noise(self.seed, "price", local.date(), spread=0.3))
        shape = 0.75 + 0.45 * math.sin(math.pi * (local.hour + local.minute / 60 - 5) / 17) ** 2
        return round(level * shape, 2)

    def prices_published_through(self) -> date:
        """Last Madrid trading day with published prices (tomorrow's appear from 11:00 UTC)."""
        today = self.as_of.date()
        return today + timedelta(days=1) if self.as_of.hour >= 11 else today

    @staticmethod
    def _slots(day: date, tz: ZoneInfo, minutes: int) -> list[datetime]:
        start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)
        nxt = day + timedelta(days=1)
        end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz).astimezone(UTC)
        n = int((end - start).total_seconds() // 60 // minutes)
        return [start + timedelta(minutes=minutes * i) for i in range(n)]

    # -- OMIE -----------------------------------------------------------------------------------
    def omie(self, day: date) -> bytes | None:
        if day > self.prices_published_through():
            return None
        lines = ["MARGINALPDBCPT;"]
        for i, t in enumerate(self._slots(day, MADRID, 15), 1):
            p = self.price(t)
            lines.append(f"{day.year};{day.month:02d};{day.day:02d};{i};{p};{p};")
        return ("\n".join([*lines, "*"]) + "\n").encode()

    # -- E-REDES --------------------------------------------------------------------------------
    def eredes(self, day: date) -> bytes | None:
        if day > self.as_of.date() - timedelta(days=self.eredes_lag_days):
            return b'{"total_count": 0, "results": []}'
        rows = []
        for t in self._slots(day, LISBON, 15):
            total = (
                self.load_mw(t.replace(minute=0)) * 1000 / 4 * (0.985 + 0.004 * math.sin(t.minute))
            )
            parts = [round(total * w, 3) for w in (0.5, 0.3, 0.15)]
            parts.append(round(total - sum(parts), 3))
            rows.append(
                {
                    "datahora": t.isoformat(),
                    "dia": f"{day.day:02d}",
                    "mes": f"{day.month:02d}",
                    "ano": str(day.year),
                    "date": day.isoformat(),
                    "time": t.astimezone(LISBON).strftime("%H:%M"),
                    "bt": parts[0],
                    "mt": parts[1],
                    "at": parts[2],
                    "mat": parts[3],
                    "total": round(sum(parts), 3),
                }
            )
        return json.dumps({"total_count": len(rows), "results": rows}).encode()

    # -- ENTSO-E --------------------------------------------------------------------------------
    @staticmethod
    def _points(values: list[float], tag: str) -> str:
        prev, out = None, []
        for i, v in enumerate(values, 1):
            if v != prev:
                out.append(f"<Point><position>{i}</position><{tag}>{v}</{tag}></Point>")
            prev = v
        return "".join(out)

    def _span(self, day: date) -> tuple[datetime, list[datetime]] | None:
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        cut = self.as_of.replace(minute=0, second=0, microsecond=0)
        end = min(start + timedelta(days=1), cut)
        if end <= start:
            return None
        return start, [
            start + timedelta(hours=i) for i in range(int((end - start).total_seconds() // 3600))
        ]

    @staticmethod
    def _series_xml(start: datetime, hours: int, points: str, zone: str, psr: str | None) -> str:
        end = start + timedelta(hours=hours)
        psr_xml = f"<MktPSRType><psrType>{psr}</psrType></MktPSRType>" if psr else ""
        return (
            f"<TimeSeries><mRID>1</mRID><{zone}BiddingZone_Domain.mRID>PT"
            f"</{zone}BiddingZone_Domain.mRID>"
            "<quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name><curveType>A03</curveType>"
            f"{psr_xml}<Period><timeInterval><start>{start.strftime(FMT)}</start>"
            f"<end>{end.strftime(FMT)}</end></timeInterval><resolution>PT60M</resolution>"
            f"{points}</Period></TimeSeries>"
        )

    @staticmethod
    def _gl(doc_type: str, series: str) -> bytes:
        ns = "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"
        return (
            f'{HDR}<GL_MarketDocument xmlns="{ns}"><mRID>m-{random.random()}</mRID>'
            f"<createdDateTime>2026-01-01T00:00:00Z</createdDateTime><type>{doc_type}</type>"
            f"{series}</GL_MarketDocument>\n"
        ).encode()

    def entsoe_load(self, day: date) -> bytes | None:
        span = self._span(day)
        if span is None:
            return None
        start, hours = span
        points = self._points([round(self.load_mw(h), 1) for h in hours], "quantity")
        return self._gl("A65", self._series_xml(start, len(hours), points, "out", None))

    def entsoe_generation(self, day: date) -> bytes | None:
        span = self._span(day)
        if span is None:
            return None
        start, hours = span
        series = "".join(
            self._series_xml(
                start,
                len(hours),
                self._points([self.gen_mw(p, b, s, h) for h in hours], "quantity"),
                "in" if d == "generation" else "out",
                p,
            )
            for p, d, b, s in SERIES
        )
        return self._gl("A75", series)

    def entsoe_price(self, day: date) -> bytes | None:
        through = self.prices_published_through()
        days = [d for d in (day, day + timedelta(days=1)) if d <= through]
        if not days:
            return None
        series = ""
        for d in days:
            slots = self._slots(d, MADRID, 15)
            end = slots[-1] + timedelta(minutes=15)
            body = self._points([self.price(t) for t in slots], "price.amount")
            series += (
                "<TimeSeries><mRID>1</mRID><currency_Unit.name>EUR</currency_Unit.name>"
                "<price_Measure_Unit.name>MWH</price_Measure_Unit.name><curveType>A03</curveType>"
                f"<Period><timeInterval><start>{slots[0].strftime(FMT)}</start>"
                f"<end>{end.strftime(FMT)}</end></timeInterval><resolution>PT15M</resolution>"
                f"{body}</Period></TimeSeries>"
            )
        ns = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"
        return (
            f'{HDR}<Publication_MarketDocument xmlns="{ns}"><mRID>m-{random.random()}</mRID>'
            f"<createdDateTime>2026-01-01T00:00:00Z</createdDateTime><type>A44</type>"
            f"{series}</Publication_MarketDocument>\n"
        ).encode()

    # -- the web --------------------------------------------------------------------------------
    def respond(self, kind: str, day: str) -> httpx.Response:
        make = {
            "omie_day_ahead_price": self.omie,
            "eredes_national_consumption": self.eredes,
            "entsoe_actual_load": self.entsoe_load,
            "entsoe_actual_generation": self.entsoe_generation,
            "entsoe_day_ahead_price": self.entsoe_price,
        }[kind]
        body = make(date.fromisoformat(day))
        if body is None:
            if kind == "omie_day_ahead_price":
                return httpx.Response(404, content=b"<html>not found</html>")
            return httpx.Response(200, content=ACK.encode())
        return httpx.Response(200, content=body)

    def payload(self, kind: str, day: str) -> bytes:
        """The healthy payload for a window, to be edited into a planted defect."""
        return self.respond(kind, day).content
