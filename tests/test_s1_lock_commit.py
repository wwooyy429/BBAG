"""S1-002 exact-commit lock regressions; all evidence and Git repos are synthetic."""

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

from test_provenance_boundaries import copy_script_source, fixture_git, guarded_script, make_repository
from test_s1_handoff import copy_reference_source, synthetic_evidence


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def s1_source(tmp_path: Path, project_root: Path) -> tuple[Path, str, Path]:
    repository, commit = make_repository(tmp_path / "source")
    copy_script_source(project_root, repository)
    shutil.copy2(project_root / "scripts/build_stage1_package.py", repository / "scripts/build_stage1_package.py")
    copy_reference_source(repository)
    (repository / ".git/info/exclude").write_text("src/\nscripts/\nconfigs/\ntests/\n", encoding="utf-8")
    assert fixture_git(repository, "status", "--porcelain") == ""
    evidence = tmp_path / "SYNTHETIC_S1_EVIDENCE_ONLY"
    synthetic_evidence(evidence, repository, commit)
    return repository, commit, evidence


def git_lock(repository: Path, commit: str) -> bytes:
    return subprocess.check_output(["git", "show", commit + ":requirements.lock"], cwd=repository)


def hide_changed_lock(repository: Path, commit: str, evidence: Path, flag: str) -> tuple[str, str]:
    committed = git_lock(repository, commit)
    fixture_git(repository, "update-index", flag, "requirements.lock")
    lock = repository / "requirements.lock"
    lock.write_bytes(committed + b"\n# SYNTHETIC S1-002 HIDDEN DISK LOCK CHANGE\n")
    verification = evidence / "verification.json"
    original_files = {path.relative_to(evidence): path.read_bytes() for path in evidence.rglob("*") if path.is_file()}
    payload = json.loads(verification.read_bytes())
    payload["dependency_lock_sha256"] = sha(lock.read_bytes())
    verification.write_text(json.dumps(payload), encoding="utf-8")
    for relative, raw in original_files.items():
        if relative != Path("verification.json"):
            assert (evidence / relative).read_bytes() == raw
    assert fixture_git(repository, "status", "--porcelain") == ""
    assert sha(committed) != payload["dependency_lock_sha256"]
    return sha(committed), payload["dependency_lock_sha256"]


def run_cli(repository: Path, commit: str, evidence: Path, output: Path, *, cwd: Path | None = None):
    environment = dict(os.environ, PYTHONPATH=str(repository / "src"), PYTHONUTF8="1",
        PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run([sys.executable, str(repository / "scripts/build_stage1_package.py"),
        "--commit", commit, "--evidence-dir", str(evidence), "--output-dir", str(output)],
        cwd=cwd or repository, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)


@pytest.mark.parametrize("git_flag", ["--assume-unchanged", "--skip-worktree"])
def test_full_cli_rejects_hidden_lock_even_when_evidence_matches_disk(
    tmp_path: Path, s1_source: tuple[Path, str, Path], git_flag: str,
) -> None:
    repository, commit, evidence = s1_source
    committed_hash, disk_hash = hide_changed_lock(repository, commit, evidence, git_flag)
    output = tmp_path / "must-not-be-created"
    result = run_cli(repository, commit, evidence, output)
    witness = {"git_flag": git_flag, "committed_lock": committed_hash,
               "matching_disk_and_evidence_lock": disk_hash, "actual_exit_code": result.returncode}
    # On the old implementation this reads the actual incorrectly accepted ZIP,
    # recording the contradiction before the expected exit-2 assertion fails.
    if result.returncode == 0:
        with zipfile.ZipFile(output / commit / "source.zip") as source:
            witness["incorrectly_packaged_source_lock"] = sha(source.read("requirements.lock"))
        packaged = json.loads((output / commit / "evidence/verification.json").read_bytes())
        witness["incorrectly_packaged_evidence_lock"] = packaged["dependency_lock_sha256"]
    assert result.returncode == 2, (witness, result.stdout, result.stderr)
    assert "dependency lock" in result.stderr.lower() and "Traceback" not in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("git_flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("existing_parent", [False, True])
def test_hidden_lock_rejection_precedes_archive_and_preserves_existing_bytes(
    tmp_path: Path, s1_source: tuple[Path, str, Path], git_flag: str, existing_parent: bool,
) -> None:
    repository, commit, evidence = s1_source
    hide_changed_lock(repository, commit, evidence, git_flag)
    output = tmp_path / "deliveries"
    if existing_parent:
        (output / "historical").mkdir(parents=True)
        (output / "historical/report.txt").write_bytes(b"SYNTHETIC OLD DELIVERY: RETAIN EXACTLY")
        (output / "unrelated.zip").write_bytes(b"SYNTHETIC EXISTING ARCHIVE")
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    result, marker = guarded_script(repository, "build_stage1_package.py", [
        "--commit", commit, "--evidence-dir", str(evidence), "--output-dir", str(output),
    ], tmp_path / "archive-guard")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "dependency lock" in result.stderr.lower() and "Traceback" not in result.stderr
    assert not marker.exists()
    assert not (output / commit).exists()
    assert not (output / f"BBAG_S1_GPT_Work_{commit[:12]}.zip").exists()
    assert {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()} == before
    assert output.exists() is existing_parent


def test_correct_lock_binds_git_disk_and_evidence_when_launched_elsewhere(
    tmp_path: Path, s1_source: tuple[Path, str, Path],
) -> None:
    repository, commit, evidence = s1_source
    committed = git_lock(repository, commit)
    assert (repository / "requirements.lock").read_bytes() == committed
    assert json.loads((evidence / "verification.json").read_bytes())["dependency_lock_sha256"] == sha(committed)
    launcher = tmp_path / "unrelated-launcher"
    launcher.mkdir()
    (launcher / "requirements.lock").write_bytes(b"DECOY: DO NOT USE THE CALLER'S CWD")
    output = tmp_path / "SYNTHETIC_POSITIVE_PACKAGE"
    result = run_cli(repository, commit, evidence, output, cwd=launcher)
    assert result.returncode == 0, (result.stdout, result.stderr)
    directory = output / commit
    with zipfile.ZipFile(directory / "source.zip") as source:
        assert source.read("requirements.lock") == committed
    verification = json.loads((directory / "evidence/verification.json").read_bytes())
    assert verification["dependency_lock_sha256"] == sha(committed)
    assert (output / f"BBAG_S1_GPT_Work_{commit[:12]}.zip").is_file()
    assert json.loads((directory / "review_request.json").read_bytes())["decision"] == "PENDING"
