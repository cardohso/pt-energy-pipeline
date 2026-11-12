"""Check results and the gate report."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Severity = Literal["blocking", "warning"]
Status = Literal["pass", "fail", "known"]


@dataclass
class CheckResult:
    """One check on one dataset.

    ``severity`` is how serious a failure of this check is (for a passing check, how serious it
    would have been). ``affected`` lists the days (YYYY-MM-DD) the finding is about;
    ``known`` is the part of it that matches ``contracts/known_issues.yaml``.
    """

    name: str
    dataset: str
    severity: Severity
    status: Status
    message: str
    observed: str = ""
    threshold: str = ""
    affected: list[str] = field(default_factory=list)
    known: list[str] = field(default_factory=list)
    opens_issue: bool = False

    @property
    def blocks(self) -> bool:
        return self.status == "fail" and self.severity == "blocking"


def passed(name: str, dataset: str, severity: Severity, message: str, **kw: Any) -> CheckResult:
    return CheckResult(name, dataset, severity, "pass", message, **kw)


def failed(name: str, dataset: str, severity: Severity, message: str, **kw: Any) -> CheckResult:
    return CheckResult(name, dataset, severity, "fail", message, **kw)


@dataclass
class GateReport:
    batch_id: str
    raw_state_sha256: str
    checked_at: str
    results: list[CheckResult]
    published: bool = False
    previous_build: bool = True  # was there a published build before this run?

    @property
    def blocking(self) -> list[CheckResult]:
        return [r for r in self.results if r.blocks]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == "fail" and r.severity == "warning"]

    @property
    def known(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == "known"]

    @property
    def ok(self) -> bool:
        return not self.blocking

    @property
    def issue_worthy(self) -> list[CheckResult]:
        """Findings that should open or update a GitHub issue."""
        return [r for r in self.results if r.blocks or (r.status == "fail" and r.opens_issue)]

    def to_dict(self) -> dict[str, Any]:
        affected = sorted({d for r in self.results if r.status == "fail" for d in r.affected})
        return {
            "batch_id": self.batch_id,
            "raw_state_sha256": self.raw_state_sha256,
            "checked_at": self.checked_at,
            "ok": self.ok,
            "published": self.published,
            "previous_build": self.previous_build,
            "counts": {
                "checks": len(self.results),
                "passed": sum(r.status == "pass" for r in self.results),
                "blocking_failures": len(self.blocking),
                "warnings": len(self.warnings),
                "known": len(self.known),
            },
            "affected_days": affected,
            "blocking": [asdict(r) for r in self.blocking],
            "warnings": [asdict(r) for r in self.warnings],
            "known": [asdict(r) for r in self.known],
            "issue_worthy": [asdict(r) for r in self.issue_worthy],
            "results": [asdict(r) for r in self.results],
        }
