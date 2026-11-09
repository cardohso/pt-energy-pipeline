"""Environment, pipeline version and contract versions."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pt_energy.contracts import load_yaml


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines into the environment without overriding values already set."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def pipeline_version() -> str:
    """Git SHA of the code that ran: CI variable first, then ``git rev-parse``, else ``unknown``."""
    for var in ("PT_ENERGY_GIT_SHA", "GITHUB_SHA"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent,
            timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def contract_versions(contracts_dir: Path) -> dict[str, str]:
    """Current version of every contract, keyed by dataset name."""
    return {p.stem: str(load_yaml(p)["version"]) for p in sorted(contracts_dir.glob("*.yaml"))}
