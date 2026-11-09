"""Immutable, content-addressed raw layer.

    <root>/raw/<source>/<dataset>/<window>/<sha256>.<ext>        the payload, byte for byte
    <root>/raw/<source>/<dataset>/<window>/<sha256>.meta.json    provenance (commit marker)
    <root>/raw/_log/ingest.jsonl                                  append-only log of attempts
    <root>/raw/_runs/<run_id>.json                                one report per run

Files are never modified or deleted. The file name is the payload hash, so fetching the same
window again with the same response writes nothing, and a revised response becomes a second file.
The log gives the order in which versions were seen; the newest log entry is the current version.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from pt_energy.ingest.models import WindowResult


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._latest: dict[tuple[str, str, str], str] | None = None
        self._history: dict[tuple[str, str, str], list[str]] = {}

    # -- layout ---------------------------------------------------------------------------------
    @property
    def log_path(self) -> Path:
        return self.root / "raw" / "_log" / "ingest.jsonl"

    def window_dir(self, source: str, dataset: str, window: str) -> Path:
        return self.root / "raw" / source / dataset / window

    # -- current version per window, from the log -----------------------------------------------
    def _latest_by_window(self) -> dict[tuple[str, str, str], str]:
        if self._latest is None:
            latest: dict[tuple[str, str, str], str] = {}
            if self.log_path.exists():
                for line in self.log_path.read_text(encoding="utf-8").splitlines():
                    entry = json.loads(line)
                    if entry.get("sha256") and entry["status"] in ("new", "unchanged", "revised"):
                        key = (entry["source"], entry["dataset"], entry["window"])
                        latest[key] = entry["sha256"]
                        chain = self._history.setdefault(key, [])
                        if not chain or chain[-1] != entry["sha256"]:
                            chain.append(entry["sha256"])
            self._latest = latest
        return self._latest

    def history(self, source: str, dataset: str, window: str) -> list[str]:
        """Content hashes of a window in the order they became current (oldest first)."""
        self._latest_by_window()
        return list(self._history.get((source, dataset, window), []))

    def windows(self, source: str, dataset: str) -> list[str]:
        """Every window that has a current version, sorted."""
        return sorted(w for (s, d, w) in self._latest_by_window() if (s, d) == (source, dataset))

    def current_sha(self, source: str, dataset: str, window: str) -> str | None:
        return self._latest_by_window().get((source, dataset, window))

    # -- writing --------------------------------------------------------------------------------
    def save(
        self,
        *,
        source: str,
        dataset: str,
        window: str,
        body: bytes,
        extension: str,
        provenance: dict[str, Any],
        content_key: bytes | None = None,
    ) -> WindowResult:
        """Store ``body`` and return ``new``, ``unchanged`` or ``revised``.

        A version is identified by the hash of ``content_key`` (default: the body itself), so
        fields the publisher changes on every request do not create fake revisions. The file keeps
        the first response seen for that content, byte for byte. ``revised`` means this window
        already had a different current version.
        """
        sha = sha256_hex(body if content_key is None else content_key)
        folder = self.window_dir(source, dataset, window)
        payload, meta = folder / f"{sha}{extension}", folder / f"{sha}.meta.json"
        previous = self.current_sha(source, dataset, window)

        if meta.exists():
            status = "unchanged" if previous == sha else "revised"
            return self._finish(source, dataset, window, status, sha, payload, previous, provenance)

        if not payload.exists() or payload.read_bytes() != body:  # heals a crash before the marker
            _atomic_write(payload, body)
        doc = {
            **provenance,
            "content_sha256": sha,
            "response_sha256": sha256_hex(body),
            "bytes": len(body),
            "supersedes": previous,
        }
        _atomic_write(meta, (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode())
        status = "revised" if previous else "new"
        return self._finish(source, dataset, window, status, sha, payload, previous, provenance)

    def _finish(
        self,
        source: str,
        dataset: str,
        window: str,
        status: str,
        sha: str,
        payload: Path,
        previous: str | None,
        provenance: dict[str, Any],
    ) -> WindowResult:
        result = WindowResult(
            source=source,
            dataset=dataset,
            window=window,
            status=status,  # type: ignore[arg-type]
            sha256=sha,
            path=payload.relative_to(self.root).as_posix(),
            attempts=int(provenance.get("attempts", 1)),
            supersedes=previous if status != "unchanged" else None,
        )
        self.log(result, run_id=str(provenance.get("run_id", "")), ts=provenance["fetched_at"])
        return result

    # -- log and run reports --------------------------------------------------------------------
    def log(self, result: WindowResult, *, run_id: str, ts: str) -> None:
        entry = {"ts": ts, "run_id": run_id, **asdict(result)}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        if result.sha256 and result.status in ("new", "unchanged", "revised"):
            key = (result.source, result.dataset, result.window)
            self._latest_by_window()[key] = result.sha256
            chain = self._history.setdefault(key, [])
            if not chain or chain[-1] != result.sha256:
                chain.append(result.sha256)

    def write_run_report(self, run_id: str, report: dict[str, Any]) -> Path:
        path = self.root / "raw" / "_runs" / f"{run_id}.json"
        _atomic_write(path, (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
        return path

    # -- reading --------------------------------------------------------------------------------
    def versions(self, source: str, dataset: str, window: str) -> list[dict[str, Any]]:
        folder = self.window_dir(source, dataset, window)
        metas = [json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.meta.json")]
        return sorted(metas, key=lambda m: str(m["fetched_at"]))


def utc_iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
