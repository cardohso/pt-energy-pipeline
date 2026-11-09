"""HTTP retries, rate limiting and the per-source readers."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from conftest import FakeWeb, fixture_bytes
from pt_energy.ingest.http import RateLimiter, fetch
from pt_energy.ingest.models import PermanentError, RequestSpec, TransientError
from pt_energy.ingest.sources import (
    EntsoeActualGeneration,
    EntsoeActualLoad,
    EntsoeDayAheadPrice,
    EredesNationalConsumption,
    OmieDayAheadPrice,
    build_sources,
)

DAY = date(2026, 11, 5)
SPEC = RequestSpec(url="https://example.test/x", params={"a": "1"})


def no_wait() -> RateLimiter:
    return RateLimiter(0.0, sleep=lambda _s: None)


# --- fetch: retries -------------------------------------------------------------------------


def test_fetch_returns_first_good_response_with_one_attempt(web: FakeWeb) -> None:
    web.set("omie_day_ahead_price", body=b"MARGINALPDBCPT;\n")
    spec = OmieDayAheadPrice().request(DAY)
    response, attempts = fetch(web.client(), spec, limiter=no_wait(), sleep=lambda _s: None)
    assert (response.status_code, attempts) == (200, 1)


def test_fetch_retries_503_with_exponential_backoff_then_succeeds(sleeps: list[float]) -> None:
    answers = iter([503, 503, 200])
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(next(answers), content=b"ok"))
    )
    response, attempts = fetch(client, SPEC, limiter=no_wait(), sleep=sleeps.append)
    assert (response.status_code, attempts, sleeps) == (200, 3, [2.0, 4.0])


def test_fetch_honours_retry_after_on_429(sleeps: list[float]) -> None:
    answers = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)])
    client = httpx.Client(transport=httpx.MockTransport(lambda r: next(answers)))
    fetch(client, SPEC, limiter=no_wait(), sleep=sleeps.append)
    assert sleeps == [7.0]


def test_fetch_caps_retry_after(sleeps: list[float]) -> None:
    answers = iter([httpx.Response(429, headers={"Retry-After": "99999"}), httpx.Response(200)])
    client = httpx.Client(transport=httpx.MockTransport(lambda r: next(answers)))
    fetch(client, SPEC, limiter=no_wait(), sleep=sleeps.append)
    assert sleeps == [60.0]


def test_fetch_retries_network_errors(sleeps: list[float]) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200)

    _, attempts = fetch(
        httpx.Client(transport=httpx.MockTransport(handler)),
        SPEC,
        limiter=no_wait(),
        sleep=sleeps.append,
    )
    assert attempts == 3


def test_fetch_gives_up_after_max_attempts(sleeps: list[float]) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(TransientError, match="HTTP 500 after 4 attempts"):
        fetch(client, SPEC, limiter=no_wait(), sleep=sleeps.append)
    assert sleeps == [2.0, 4.0, 8.0]


def test_fetch_does_not_retry_client_errors(sleeps: list[float]) -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(401)

    response, attempts = fetch(
        httpx.Client(transport=httpx.MockTransport(handler)),
        SPEC,
        limiter=no_wait(),
        sleep=sleeps.append,
    )
    assert (response.status_code, attempts, len(seen), sleeps) == (401, 1, 1, [])


# --- rate limiter ---------------------------------------------------------------------------


def test_rate_limiter_spaces_calls() -> None:
    now = {"t": 100.0}
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    limiter = RateLimiter(0.5, clock=lambda: now["t"], sleep=fake_sleep)
    limiter.wait()  # first call never waits
    now["t"] += 0.2
    limiter.wait()  # 0.3 s still owed
    limiter.wait()  # immediately again: full interval owed
    assert [round(s, 3) for s in slept] == [0.3, 0.5]


# --- requests -------------------------------------------------------------------------------


def test_request_redacts_secrets_but_sends_them() -> None:
    spec = EntsoeActualLoad("SECRET").request(DAY)
    assert spec.params["securityToken"] == "SECRET"
    assert spec.redacted_params()["securityToken"] == "REDACTED"
    assert "SECRET" not in str(spec.redacted_params())


def test_entsoe_requests_use_the_right_document_types_and_window() -> None:
    load = EntsoeActualLoad("t").request(DAY).params
    gen = EntsoeActualGeneration("t").request(DAY).params
    price = EntsoeDayAheadPrice("t").request(DAY).params
    assert (load["documentType"], load["processType"]) == ("A65", "A16")
    assert "outBiddingZone_Domain" in load and "in_Domain" in gen
    assert (gen["documentType"], price["documentType"]) == ("A75", "A44")
    assert (price["in_Domain"], price["out_Domain"]) == ("10YPT-REN------W",) * 2
    assert (load["periodStart"], load["periodEnd"]) == ("202611050000", "202611060000")


def test_omie_and_eredes_requests() -> None:
    assert OmieDayAheadPrice().request(DAY).params["filename"] == "marginalpdbcpt_20261105.1"
    params = EredesNationalConsumption().request(DAY).params
    assert params["where"] == "date=date'2026-11-05'" and params["limit"] == "100"


def test_entsoe_needs_a_token() -> None:
    with pytest.raises(ValueError, match="token"):
        EntsoeActualLoad("")


# --- reading responses ----------------------------------------------------------------------


def test_omie_reading() -> None:
    omie = OmieDayAheadPrice()
    assert omie.interpret(200, fixture_bytes("omie", "marginalpdbcpt_20261105.1")) == "ok"
    assert omie.interpret(200, fixture_bytes("omie", "marginalpdbcpt_20250930.1")) == "ok"
    assert omie.interpret(404, b"<html>") == "no_data"
    with pytest.raises(PermanentError, match="unexpected OMIE content"):
        omie.interpret(200, b"<html>maintenance</html>")
    with pytest.raises(PermanentError, match="HTTP 403"):
        omie.interpret(403, b"")


def test_eredes_reading() -> None:
    src = EredesNationalConsumption()
    for day in ("2026-11-03", "2026-03-29", "2026-10-25"):
        assert (
            src.interpret(200, fixture_bytes("eredes", f"consumo-total-nacional_{day}.json"))
            == "ok"
        )
    assert src.interpret(200, b'{"total_count": 0, "results": []}') == "no_data"
    with pytest.raises(PermanentError, match="1 of 5 rows"):
        src.interpret(200, b'{"total_count": 5, "results": [{}]}')
    with pytest.raises(PermanentError, match="unexpected E-REDES JSON"):
        src.interpret(200, b"not json")
    with pytest.raises(PermanentError, match="unexpected E-REDES JSON"):
        src.interpret(200, b'{"other": 1}')


def test_entsoe_reading() -> None:
    src = EntsoeActualLoad("t")
    assert src.interpret(200, fixture_bytes("entsoe", "load_2026-11-05.xml")) == "ok"
    assert src.interpret(200, fixture_bytes("entsoe", "price_2026-11-05.xml")) == "ok"
    assert src.interpret(200, fixture_bytes("entsoe", "ack_no_data.xml")) == "no_data"
    with pytest.raises(PermanentError, match="rejected the security token"):
        src.interpret(401, b"")
    with pytest.raises(PermanentError, match="rejected the request: Unexpected parameter"):
        src.interpret(400, fixture_bytes("entsoe", "ack_bad_request.xml"))
    with pytest.raises(PermanentError, match="unexpected ENTSO-E content"):
        src.interpret(200, b"<html></html>")
    with pytest.raises(PermanentError, match="HTTP 403"):
        src.interpret(403, b"")


# --- choosing sources -----------------------------------------------------------------------


def test_build_sources_all_needs_the_token_and_names_the_dataset() -> None:
    with pytest.raises(ValueError, match="entsoe_actual_load needs ENTSOE_API_TOKEN"):
        build_sources(["all"], None)


def test_build_sources_without_token_works_for_open_datasets() -> None:
    sources = build_sources(["omie_day_ahead_price", "eredes_national_consumption"], None)
    assert [s.dataset for s in sources] == ["omie_day_ahead_price", "eredes_national_consumption"]


def test_build_sources_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="unknown dataset"):
        build_sources(["nope"], "t")


def test_build_sources_all_with_token_gives_five() -> None:
    assert len(build_sources([], "t")) == 5
