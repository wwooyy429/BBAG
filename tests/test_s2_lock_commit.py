"""S2 restored-source lock binding; synthetic Git/SQLite fixtures, no platform I/O."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from test_provenance_boundaries import fixture_git, guarded_script, make_repository
from test_s2_handoff import copy_reference_source, synthetic_evidence


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def git_lock(repository: Path, commit: str) -> bytes:
    return subprocess.check_output(["git", "show", commit + ":requirements.lock"], cwd=repository)


@pytest.fixture
def s2_source(tmp_path: Path, project_root: Path, monkeypatch) -> tuple[Path, str, Path]:
    repository, _ = make_repository(tmp_path / "source")
    copy_reference_source(repository)
    shutil.copytree(project_root / "src/bbag", repository / "src/bbag",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("verify_stage2.py", "build_stage2_package.py"):
        shutil.copy2(project_root / "scripts" / name, repository / "scripts" / name)
    fixture_git(repository, "add", ".")
    tree = fixture_git(repository, "write-tree")
    text = (f"tree {tree}\n" "author Unit Fixture <fixture@example.invalid> 1700000000 +0000\n"
            "committer Unit Fixture <fixture@example.invalid> 1700000000 +0000\n\nSynthetic S2 lock control only\n")
    commit = fixture_git(repository, "hash-object", "-t", "commit", "-w", "--stdin", input_text=text)
    fixture_git(repository, "update-ref", "refs/heads/fixture-main", commit)
    assert fixture_git(repository, "status", "--porcelain") == ""
    assert git_lock(repository, commit) == (repository / "requirements.lock").read_bytes()
    evidence = tmp_path / "SYNTHETIC_S2_EVIDENCE_ONLY"
    synthetic_evidence(evidence, repository, monkeypatch, commit=commit)
    assert json.loads((evidence / "verification.json").read_bytes())["dependency_lock_sha256"] == sha(git_lock(repository, commit))
    return repository, commit, evidence


def hide_changed_lock(repository: Path, commit: str, evidence: Path, flag: str) -> tuple[str, str]:
    committed = git_lock(repository, commit)
    fixture_git(repository, "update-index", flag, "requirements.lock")
    lock = repository / "requirements.lock"
    lock.write_bytes(committed + b"\n# SYNTHETIC S2 HIDDEN DISK LOCK CHANGE\n")
    before = {path.relative_to(evidence): path.read_bytes() for path in evidence.rglob("*") if path.is_file()}
    path = evidence / "verification.json"
    payload = json.loads(path.read_bytes())
    payload["dependency_lock_sha256"] = sha(lock.read_bytes())
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert all((evidence / name).read_bytes() == raw for name, raw in before.items() if name != Path("verification.json"))
    assert fixture_git(repository, "status", "--porcelain") == ""
    assert sha(committed) != payload["dependency_lock_sha256"]
    return sha(committed), payload["dependency_lock_sha256"]


def run_cli(repository: Path, commit: str, evidence: Path, output: Path, *, cwd: Path | None = None):
    environment = dict(os.environ, PYTHONPATH=str(repository / "src"), PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run([sys.executable, str(repository / "scripts/build_stage2_package.py"),
        "--commit", commit, "--evidence-dir", str(evidence), "--output-dir", str(output)],
        cwd=cwd or repository, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=40, check=False)


@pytest.mark.parametrize("git_flag", ["--assume-unchanged", "--skip-worktree"])
def test_full_cli_rejects_hidden_s2_lock_even_when_evidence_matches_disk(
    tmp_path: Path, s2_source: tuple[Path, str, Path], git_flag: str,
) -> None:
    repository, commit, evidence = s2_source
    committed_hash, disk_hash = hide_changed_lock(repository, commit, evidence, git_flag)
    output = tmp_path / "must-not-be-created"
    result = run_cli(repository, commit, evidence, output)
    witness = dict(git_flag=git_flag, committed_lock=committed_hash,
                   matching_disk_and_evidence_lock=disk_hash, actual_exit_code=result.returncode)
    if result.returncode == 0:
        with zipfile.ZipFile(output / commit / "source.zip") as source:
            witness["incorrectly_packaged_source_lock"] = sha(source.read("requirements.lock"))
        packaged = json.loads((output / commit / "evidence/verification.json").read_bytes())
        witness["incorrectly_packaged_evidence_lock"] = packaged["dependency_lock_sha256"]
    assert result.returncode == 2, (witness, result.stdout, result.stderr)
    assert json.loads(result.stderr)["stage"] == "S2" and "dependency lock" in result.stderr.lower()
    assert not output.exists()


@pytest.mark.parametrize("git_flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("existing_parent", [False, True])
def test_s2_hidden_lock_rejection_precedes_archive_and_preserves_existing_bytes(
    tmp_path: Path, s2_source: tuple[Path, str, Path], git_flag: str, existing_parent: bool,
) -> None:
    repository, commit, evidence = s2_source
    hide_changed_lock(repository, commit, evidence, git_flag)
    output = tmp_path / "deliveries"
    if existing_parent:
        (output / "historical").mkdir(parents=True)
        (output / "historical/report.txt").write_bytes(b"SYNTHETIC OLD DELIVERY: RETAIN EXACTLY")
        (output / "unrelated.zip").write_bytes(b"SYNTHETIC EXISTING ARCHIVE")
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    result, marker = guarded_script(repository, "build_stage2_package.py", [
        "--commit", commit, "--evidence-dir", str(evidence), "--output-dir", str(output),
    ], tmp_path / "archive-guard")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert json.loads(result.stderr)["stage"] == "S2" and "dependency lock" in result.stderr.lower()
    assert not marker.exists() and not (output / commit).exists()
    assert not (output / f"BBAG_S2_GPT_Work_{commit[:12]}.zip").exists()
    assert {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()} == before
    assert output.exists() is existing_parent


def test_correct_s2_lock_binds_git_disk_and_evidence_when_launched_elsewhere(
    tmp_path: Path, s2_source: tuple[Path, str, Path],
) -> None:
    repository, commit, evidence = s2_source
    committed = git_lock(repository, commit)
    launcher = tmp_path / "unrelated-launcher"
    launcher.mkdir()
    (launcher / "requirements.lock").write_bytes(b"DECOY: NOT THE COMMITTED SOURCE LOCK")
    output = tmp_path / "SYNTHETIC_POSITIVE_PACKAGE"
    result = run_cli(repository, commit, evidence, output, cwd=launcher)
    assert result.returncode == 0, (result.stdout, result.stderr)
    with zipfile.ZipFile(output / commit / "source.zip") as source:
        assert source.read("requirements.lock") == committed
    assert (repository / "requirements.lock").read_bytes() == committed
    verification = json.loads((output / commit / "evidence/verification.json").read_bytes())
    assert verification["dependency_lock_sha256"] == sha(committed)
    assert (output / f"BBAG_S2_GPT_Work_{commit[:12]}.zip").is_file()
    receipt = json.loads((output / commit / "review_request.json").read_bytes())
    assert receipt["decision"] == "PENDING" and receipt["platform_status"] == "NOT_RUN"
