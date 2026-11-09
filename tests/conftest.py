"""Shared test helpers: a fake web server built from recorded fixtures. No test uses the network."""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "SECRET-TOKEN-123"  # fake; any appearance in stored data is a test failure
ENTSOE_KINDS = {
    "A65": "entsoe_actual_load",
    "A75": "entsoe_actual_generation",
    "A44": "entsoe_day_ahead_price",
}
ENTSOE_FILES = {
    "entsoe_actual_load": "load_2026-11-05.xml",
    "entsoe_actual_generation": "generation_2026-11-05.xml",
    "entsoe_day_ahead_price": "price_2026-11-05.xml",
}


def fixture_bytes(*parts: str) -> bytes:
    return FIXTURES.joinpath(*parts).read_bytes()


class FakeWeb:
    """Serves OMIE, E-REDES and ENTSO-E from fixtures; individual answers can be overridden."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.world: Any = None  # tests/synth.World: serves every feed when set
        self._overrides: dict[tuple[str, str | None], Callable[[], httpx.Response]] = {}

    # -- configuration --------------------------------------------------------------------------
    def set(
        self,
        kind: str,
        day: str | None = None,
        *,
        status: int = 200,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._overrides[(kind, day)] = lambda: httpx.Response(
            status, content=body, headers=headers or {}
        )

    def set_fn(self, kind: str, fn: Callable[[], httpx.Response], day: str | None = None) -> None:
        self._overrides[(kind, day)] = fn

    def count(self, kind: str, day: str | None = None) -> int:
        return sum(1 for r in self.requests if _classify(r) == (kind, day or _classify(r)[1]))

    # -- transport ------------------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport())

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        kind, day = _classify(request)
        for key in ((kind, day), (kind, None)):
            if key in self._overrides:
                return self._overrides[key]()
        if self.world is not None:
            return self.world.respond(kind, day)  # type: ignore[no-any-return]
        return _default(kind, day)


def _classify(request: httpx.Request) -> tuple[str, str]:
    host, params = request.url.host, dict(request.url.params)
    if host.endswith("omie.es"):
        m = re.search(r"_(\d{4})(\d{2})(\d{2})\.1$", params["filename"])
        assert m
        return "omie_day_ahead_price", "-".join(m.groups())
    if "e-redes" in host:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", params["where"])
        assert m
        return "eredes_national_consumption", m.group(1)
    if host.endswith("entsoe.eu"):
        s = params["periodStart"]
        return ENTSOE_KINDS[params["documentType"]], f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    raise AssertionError(f"unexpected request to {request.url}")


def _default(kind: str, day: str) -> httpx.Response:
    if kind == "omie_day_ahead_price":
        name = f"marginalpdbcpt_{day.replace('-', '')}.1"
        path = FIXTURES / "omie" / name
        if path.exists():
            return httpx.Response(200, content=path.read_bytes())
        return httpx.Response(404, content=b"<html>not found</html>")
    if kind == "eredes_national_consumption":
        path = FIXTURES / "eredes" / f"consumo-total-nacional_{day}.json"
        body = path.read_bytes() if path.exists() else b'{"total_count": 0, "results": []}'
        return httpx.Response(200, content=body)
    return httpx.Response(200, content=fixture_bytes("entsoe", ENTSOE_FILES[kind]))


class FakeClock:
    """Moves forward one second per call, so every run and fetch has its own timestamp."""

    def __init__(self, start: datetime | None = None) -> None:
        self._ticks = itertools.count()
        self._start = start or datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._start + timedelta(seconds=next(self._ticks))


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeps() -> list[float]:
    return []


def tree(root: Path, skip: tuple[str, ...] = ("_log", "_runs")) -> dict[str, bytes]:
    """Every file under ``root`` as {relative path: content}, ignoring bookkeeping folders."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not any(part in skip for part in p.relative_to(root).parts)
    }


def ingest(
    web: FakeWeb,
    root: Path,
    clock: FakeClock,
    sleeps: list[float],
    days: list[str],
    datasets: list[str] | None = None,
) -> None:
    """Fetch ``days`` for ``datasets`` from the fake web into the raw layer at ``root``."""
    from datetime import date

    from pt_energy.ingest.runner import run_ingest
    from pt_energy.ingest.sources import build_sources
    from pt_energy.ingest.store import RawStore

    sources = build_sources(datasets or [], TOKEN)
    parsed_days = [date.fromisoformat(d) for d in days]
    run_ingest(
        [(s, parsed_days) for s in sources],
        client=web.client(),
        store=RawStore(root),
        pipeline_version="test-sha",
        contract_versions={s.dataset: "1.0.0" for s in sources},
        now=clock,
        sleep=sleeps.append,
    )
