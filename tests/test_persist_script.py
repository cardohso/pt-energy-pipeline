"""scripts/persist_data.sh against real local git repositories (a bare 'remote' and clones)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "persist_data.sh"
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
ENV = {
    **os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test", "PERSIST_RETRY_SLEEP": "0",
}  # fmt: skip


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=ENV, capture_output=True, text=True, check=True
    ).stdout


def persist(data: Path, message: str = "data: run") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), str(data), message], env=ENV, capture_output=True, text=True
    )


def write(root: Path, rel: str, text: str = "x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "--quiet", "-b", "main", str(remote))
    data = tmp_path / "data"
    git(tmp_path, "clone", "--quiet", str(remote), str(data))
    git(data, "checkout", "-q", "-b", "main")
    return remote, data


def remote_files(remote: Path) -> set[str]:
    return set(git(remote, "ls-tree", "-r", "--name-only", "main").split())


def populate(data: Path, day: str = "2026-11-09") -> None:
    write(data, f"raw/omie/omie_day_ahead_price/{day}/abc.txt", "prices")
    write(data, f"raw/omie/omie_day_ahead_price/{day}/abc.meta.json", "{}")
    write(data, "raw/_log/ingest.jsonl", day + "\n")
    write(data, "reports/latest.json", "{}")
    write(data, "lineage/openlineage/b.jsonl", "{}")
    write(data, "serving/model/demand.parquet", "pq")
    write(data, "serving/meta/check_results.parquet", "pq")


def test_first_run_persists_exactly_what_belongs_in_the_data_repo(repo: tuple[Path, Path]) -> None:
    remote, data = repo
    populate(data)
    # things that must never be committed
    write(data, "serving/staging/omie.parquet")
    write(data, "serving/pt_energy.duckdb")
    write(data, "serving/pt_energy.duckdb.wal")
    write(data, "rejected/pt_energy.duckdb")
    write(data, "serving.build/model/x.parquet")
    result = persist(data)
    assert result.returncode == 0, result.stderr
    assert remote_files(remote) == {
        ".gitignore", "README.md",
        "raw/omie/omie_day_ahead_price/2026-11-09/abc.txt",
        "raw/omie/omie_day_ahead_price/2026-11-09/abc.meta.json",
        "raw/_log/ingest.jsonl", "reports/latest.json", "lineage/openlineage/b.jsonl",
        "serving/model/demand.parquet", "serving/meta/check_results.parquet",
    }  # fmt: skip


def test_a_run_with_nothing_new_makes_no_commit(repo: tuple[Path, Path]) -> None:
    remote, data = repo
    populate(data)
    persist(data)
    before = git(remote, "rev-list", "--count", "main")
    result = persist(data)
    assert result.returncode == 0 and "nothing to commit" in result.stdout
    assert git(remote, "rev-list", "--count", "main") == before


def test_raw_history_is_append_only(repo: tuple[Path, Path]) -> None:
    remote, data = repo
    populate(data)
    persist(data)
    write(data, "raw/omie/omie_day_ahead_price/2026-11-10/def.txt", "tomorrow")
    write(data, "raw/_log/ingest.jsonl", "2026-11-09\n2026-11-10\n")
    persist(data, "data: second run")
    changes = dict(
        line.split("\t")[::-1]
        for line in git(remote, "diff", "--name-status", "HEAD~1", "HEAD").splitlines()
    )
    assert changes["raw/omie/omie_day_ahead_price/2026-11-10/def.txt"] == "A"
    assert not any(v == "D" for v in changes.values())
    assert "raw/omie/omie_day_ahead_price/2026-11-09/abc.txt" not in changes  # untouched


def test_a_failed_gate_keeps_the_previous_good_tables_in_the_repo(repo: tuple[Path, Path]) -> None:
    remote, data = repo
    populate(data)
    persist(data)
    good = git(remote, "show", "main:serving/model/demand.parquet")
    # a refused run: new raw data and a report, serving untouched, rejected build set aside
    write(data, "raw/omie/omie_day_ahead_price/2026-11-10/bad.txt", "bad")
    write(data, "reports/latest.json", '{"ok": false}')
    write(data, "rejected/model/demand.parquet", "REFUSED")
    persist(data, "data: refused run")
    assert git(remote, "show", "main:serving/model/demand.parquet") == good
    assert "rejected/model/demand.parquet" not in remote_files(remote)
    assert git(remote, "show", "main:reports/latest.json") == '{"ok": false}'


def test_a_push_race_with_another_writer_is_resolved_by_rebasing(
    tmp_path: Path, repo: tuple[Path, Path]
) -> None:
    remote, data = repo
    populate(data)
    persist(data)
    other = tmp_path / "other"
    git(tmp_path, "clone", "--quiet", str(remote), str(other))
    write(other, "NOTES.md", "someone else")
    git(other, "add", "NOTES.md")
    git(other, "commit", "--quiet", "-m", "other writer")
    git(other, "push", "--quiet")
    write(data, "raw/omie/omie_day_ahead_price/2026-11-10/def.txt", "tomorrow")
    result = persist(data, "data: raced")
    assert result.returncode == 0, result.stderr
    assert {"NOTES.md", "raw/omie/omie_day_ahead_price/2026-11-10/def.txt"} <= remote_files(remote)


def test_a_conflicting_change_fails_loudly_instead_of_overwriting(
    tmp_path: Path, repo: tuple[Path, Path]
) -> None:
    remote, data = repo
    populate(data)
    persist(data)
    other = tmp_path / "other"
    git(tmp_path, "clone", "--quiet", str(remote), str(other))
    write(other, "reports/latest.json", "theirs")
    git(other, "commit", "-aq", "-m", "other writer edits the same file")
    git(other, "push", "--quiet")
    write(data, "reports/latest.json", "ours")
    result = persist(data, "data: conflict")
    assert result.returncode == 1 and "manual fix needed" in result.stderr
    assert git(remote, "show", "main:reports/latest.json") == "theirs"  # nothing was overwritten


def test_the_script_is_executable_and_documents_itself() -> None:
    assert os.access(SCRIPT, os.X_OK)
    assert "append-only" in SCRIPT.read_text() and "serving/staging" in SCRIPT.read_text()
