"""HTTP with retries and per-source rate limiting. Clock and sleep are injectable."""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from pt_energy import __version__
from pt_energy.ingest.models import RequestSpec, TransientError

USER_AGENT = f"pt-energy-pipeline/{__version__} (+https://github.com/cardohso/pt-energy-pipeline)"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 60.0

Sleep = Callable[[float], None]


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(30.0), follow_redirects=True
    )


class RateLimiter:
    """Keeps at least ``min_interval`` seconds between calls."""

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep | None = None,
    ) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._sleep: Sleep = sleep or time.sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            remaining = self.min_interval - (now - self._last)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last = now


def _retry_after(response: httpx.Response, fallback: float) -> float:
    try:
        return min(float(response.headers["Retry-After"]), MAX_RETRY_AFTER_S)
    except (KeyError, ValueError):
        return fallback


def fetch(
    client: httpx.Client,
    spec: RequestSpec,
    *,
    limiter: RateLimiter,
    sleep: Sleep | None = None,
    max_attempts: int = 4,
    base_delay: float = 2.0,
) -> tuple[httpx.Response, int]:
    """GET ``spec`` and return the response and the number of attempts used.

    Network errors, 429 and 5xx are retried with exponential backoff (honouring ``Retry-After``).
    Any other status is returned as is; the source decides what it means. Raises
    ``TransientError`` when all attempts fail.
    """
    sleep = sleep or time.sleep
    last = "no attempt made"
    for attempt in range(1, max_attempts + 1):
        limiter.wait()
        delay = base_delay * 2 ** (attempt - 1)
        try:
            response = client.get(spec.url, params=spec.params)
        except httpx.TransportError as exc:
            last = f"{type(exc).__name__}"
        else:
            if response.status_code not in RETRYABLE_STATUS:
                return response, attempt
            last = f"HTTP {response.status_code}"
            delay = _retry_after(response, delay)
        if attempt < max_attempts:
            sleep(delay)
    raise TransientError(f"{last} after {max_attempts} attempts")
