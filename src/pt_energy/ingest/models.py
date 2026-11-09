"""Shared types for ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Status = Literal["new", "unchanged", "revised", "no_data", "failed", "skipped"]

SECRET_PARAMS = frozenset({"securityToken"})


class IngestError(Exception):
    """Base class for ingestion failures."""


class TransientError(IngestError):
    """Worth retrying: network trouble, HTTP 429 or 5xx."""


class PermanentError(IngestError):
    """Retrying will not help: bad token, bad request, unexpected content."""


@dataclass(frozen=True)
class RequestSpec:
    """One HTTP GET. Secret parameters are sent but never stored."""

    url: str
    params: dict[str, str] = field(default_factory=dict)
    secret_params: frozenset[str] = SECRET_PARAMS

    def redacted_params(self) -> dict[str, str]:
        return {k: ("REDACTED" if k in self.secret_params else v) for k, v in self.params.items()}


@dataclass(frozen=True)
class WindowResult:
    source: str
    dataset: str
    window: str
    status: Status
    sha256: str | None = None
    path: str | None = None
    attempts: int = 0
    message: str | None = None
    supersedes: str | None = None
