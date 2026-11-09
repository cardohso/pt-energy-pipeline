"""One adapter per dataset: how to ask for a day, and how to read the answer.

An adapter never changes the payload. ``interpret`` only decides whether the response is usable
data (``ok``), a legitimate "nothing there" (``no_data``), or an error.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import ClassVar, Literal

from pt_energy.ingest.models import PermanentError, RequestSpec

Verdict = Literal["ok", "no_data"]

PT_EIC = "10YPT-REN------W"  # Portugal bidding zone (ENTSO-E area code)
ENTSOE_URL = "https://web-api.tp.entsoe.eu/api"
_ACK_CODE = re.compile(rb"<code>(\d+)</code>")
_ACK_TEXT = re.compile(rb"<text>(.*?)</text>", re.DOTALL)
_DOC_MRID = re.compile(rb"<mRID>[^<]*</mRID>")
_CREATED = re.compile(rb"<createdDateTime>[^<]*</createdDateTime>")


class Source:
    """Base adapter. ``name`` is the publisher, ``dataset`` is also the contract name."""

    name: str
    dataset: str
    extension: str
    publishes_ahead = False  # data for tomorrow can already exist
    min_interval_s = 0.5

    def request(self, day: date) -> RequestSpec:
        raise NotImplementedError

    def interpret(self, status: int, body: bytes) -> Verdict:
        if status == 200:
            return self.check_body(body)
        raise PermanentError(f"HTTP {status}")

    def check_body(self, body: bytes) -> Verdict:
        raise NotImplementedError

    def canonical(self, body: bytes) -> bytes:
        """Bytes whose hash identifies the *content*. Identity unless the publisher adds fields
        that change on every request without the data changing."""
        return body


class OmieDayAheadPrice(Source):
    name = "omie"
    dataset = "omie_day_ahead_price"
    extension = ".txt"
    publishes_ahead = True

    def request(self, day: date) -> RequestSpec:
        return RequestSpec(
            url="https://www.omie.es/es/file-download",
            params={"parents": "marginalpdbcpt", "filename": f"marginalpdbcpt_{day:%Y%m%d}.1"},
        )

    def interpret(self, status: int, body: bytes) -> Verdict:
        if status == 404:  # not published (yet) for that trading day
            return "no_data"
        return super().interpret(status, body)

    def check_body(self, body: bytes) -> Verdict:
        if not body.startswith(b"MARGINALPDBCPT;"):
            raise PermanentError("unexpected OMIE content (not a marginalpdbcpt file)")
        return "ok"


class EredesNationalConsumption(Source):
    name = "eredes"
    dataset = "eredes_national_consumption"
    extension = ".json"

    def request(self, day: date) -> RequestSpec:
        return RequestSpec(
            url="https://e-redes.opendatasoft.com/api/explore/v2.1/catalog/datasets/"
            "consumo-total-nacional/records",
            params={"where": f"date=date'{day:%Y-%m-%d}'", "order_by": "datahora", "limit": "100"},
        )

    def check_body(self, body: bytes) -> Verdict:
        try:
            doc = json.loads(body)
            total, rows = int(doc["total_count"]), doc["results"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PermanentError(f"unexpected E-REDES JSON: {type(exc).__name__}") from exc
        if total == 0:
            return "no_data"
        if len(rows) != total:
            raise PermanentError(f"E-REDES returned {len(rows)} of {total} rows (paging needed)")
        return "ok"


class _Entsoe(Source):
    name = "entsoe"
    min_interval_s = 0.25  # well under the 400 requests/minute limit per token
    document_markers: tuple[bytes, ...] = (b"GL_MarketDocument", b"Publication_MarketDocument")
    query: ClassVar[dict[str, str]]

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("ENTSO-E token is empty")
        self._token = token

    def request(self, day: date) -> RequestSpec:
        nxt = day + timedelta(days=1)
        params = {
            "securityToken": self._token,
            **self.query,
            "periodStart": f"{day:%Y%m%d}0000",
            "periodEnd": f"{nxt:%Y%m%d}0000",
        }
        return RequestSpec(url=ENTSOE_URL, params=params)

    def canonical(self, body: bytes) -> bytes:
        """ENTSO-E stamps every response with a new document mRID and creation time."""
        body = _DOC_MRID.sub(b"<mRID/>", body, count=1)
        return _CREATED.sub(b"<createdDateTime/>", body, count=1)

    def interpret(self, status: int, body: bytes) -> Verdict:
        if status == 401:
            raise PermanentError("ENTSO-E rejected the security token (HTTP 401)")
        if status in (200, 400):
            return self.check_body(body, status)
        raise PermanentError(f"HTTP {status}")

    def check_body(self, body: bytes, status: int = 200) -> Verdict:
        if b"Acknowledgement_MarketDocument" in body[:600]:
            code = _ACK_CODE.search(body)
            text = _ACK_TEXT.search(body)
            if code and code.group(1) == b"999":
                return "no_data"
            detail = text.group(1).decode(errors="replace")[:200] if text else "no reason given"
            raise PermanentError(f"ENTSO-E rejected the request: {detail}")
        if status == 200 and any(m in body[:600] for m in self.document_markers):
            return "ok"
        raise PermanentError("unexpected ENTSO-E content")


class EntsoeActualLoad(_Entsoe):
    dataset = "entsoe_actual_load"
    extension = ".xml"
    query: ClassVar[dict[str, str]] = {
        "documentType": "A65",
        "processType": "A16",
        "outBiddingZone_Domain": PT_EIC,
    }


class EntsoeActualGeneration(_Entsoe):
    dataset = "entsoe_actual_generation"
    extension = ".xml"
    query: ClassVar[dict[str, str]] = {
        "documentType": "A75",
        "processType": "A16",
        "in_Domain": PT_EIC,
    }


class EntsoeDayAheadPrice(_Entsoe):
    dataset = "entsoe_day_ahead_price"
    extension = ".xml"
    publishes_ahead = True
    query: ClassVar[dict[str, str]] = {
        "documentType": "A44",
        "in_Domain": PT_EIC,
        "out_Domain": PT_EIC,
    }


ENTSOE_DATASETS: dict[str, type[_Entsoe]] = {
    cls.dataset: cls for cls in (EntsoeActualLoad, EntsoeActualGeneration, EntsoeDayAheadPrice)
}
OPEN_DATASETS: dict[str, type[Source]] = {
    OmieDayAheadPrice.dataset: OmieDayAheadPrice,
    EredesNationalConsumption.dataset: EredesNationalConsumption,
}
ALL_DATASETS = [*OPEN_DATASETS, *ENTSOE_DATASETS]


def build_sources(names: list[str], entsoe_token: str | None) -> list[Source]:
    """Instantiate adapters for dataset ``names`` (``all`` means every dataset)."""
    wanted = ALL_DATASETS if not names or "all" in names else names
    unknown = [n for n in wanted if n not in ALL_DATASETS]
    if unknown:
        raise ValueError(f"unknown dataset(s): {', '.join(unknown)}; choose from {ALL_DATASETS}")
    sources: list[Source] = []
    for name in wanted:
        if name in OPEN_DATASETS:
            sources.append(OPEN_DATASETS[name]())
        elif not entsoe_token:
            raise ValueError(
                f"{name} needs ENTSOE_API_TOKEN: copy .env.example to .env and fill it in"
            )
        else:
            sources.append(ENTSOE_DATASETS[name](entsoe_token))
    return sources
