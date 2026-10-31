"""S0-004 package-boundary regressions; all evidence here is synthetic."""

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

from test_provenance_boundaries import (
    copy_script_source, fixture_git, guarded_script, make_repository, make_synthetic_package_evidence,
)
from test_s1_handoff import copy_reference_source, synthetic_evidence as s1_synthetic_evidence


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def corrupt_lock_claim(evidence: Path, fault: str) -> None:
    path = evidence / "verification.json"
    payload = json.loads(path.read_bytes())
    values = {
        "wrong_zero": "0" * 64,
        "different_valid": hashlib.sha256(b"a different independently named lock file").hexdigest(),
        "malformed": "NOT_A_SHA256",
        "uppercase": "A" * 64,
        "short": "a" * 63,
        "padded": " " + payload["dependency_lock_sha256"],
        "null": None, "boolean": False, "integer": 0, "float": 0.0,
        "list": [payload["dependency_lock_sha256"]],
        "object": {"sha256": payload["dependency_lock_sha256"]},
    }
    if fault == "missing":
        del payload["dependency_lock_sha256"]
    else:
        payload["dependency_lock_sha256"] = values[fault]
    write(path, payload)


@pytest.fixture
def s0_source(tmp_path: Path, project_root: Path, example_path: Path) -> tuple[Path, str, Path]:
    repository, commit = make_repository(tmp_path / "source")
    copy_script_source(project_root, repository)
    # The helper's committed lock remains tracked; only copied implementation
    # code is ignored in this temporary provenance fixture.
    (repository / ".git/info/exclude").write_text("src/\nscripts/\n", encoding="utf-8")
    assert fixture_git(repository, "status", "--porcelain") == ""
    assert fixture_git(repository, "show", commit + ":requirements.lock")
    evidence = tmp_path / "SYNTHETIC_EVIDENCE_ONLY"
    make_synthetic_package_evidence(evidence, commit, example_path)
    return repository, commit, evidence


def assert_rejected_before_archive(repository: Path, commit: str, evidence: Path,
                                   output: Path, guard: Path) -> None:
    result, marker = guarded_script(repository, "build_review_package.py", [
        "--commit", commit, "--evidence-dir", str(evidence), "--output-dir", str(output),
    ], guard)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "dependency lock" in result.stderr.lower(), result.stderr
    assert "Traceback" not in result.stderr and not marker.exists()
    assert not (output / commit).exists()
    assert not (output / f"BBAG_S0_GPT_Work_{commit[:12]}.zip").exists()


@pytest.mark.parametrize("fault", [
    "wrong_zero", "missing", "malformed", "different_valid", "uppercase", "short", "padded",
    "null", "boolean", "integer", "float", "list", "object",
])
def test_s0_cli_rejects_unbound_or_untyped_lock_claim_before_any_output(
    tmp_path: Path, s0_source: tuple[Path, str, Path], fault: str,
) -> None:
    repository, commit, evidence = s0_source
    corrupt_lock_claim(evidence, fault)
    output = tmp_path / "must-not-be-created"
    assert_rejected_before_archive(repository, commit, evidence, output, tmp_path / "archive-guard")
    assert not output.exists()


@pytest.mark.parametrize("fault", ["wrong_zero", "missing", "malformed"])
def test_s0_lock_rejection_preserves_unrelated_existing_delivery_bytes(
    tmp_path: Path, s0_source: tuple[Path, str, Path], fault: str,
) -> None:
    repository, commit, evidence = s0_source
    corrupt_lock_claim(evidence, fault)
    output = tmp_path / "existing-deliveries"
    (output / "historical-review").mkdir(parents=True)
    (output / "historical-review/report.txt").write_bytes(b"SYNTHETIC OLD REVIEW: KEEP EXACTLY")
    (output / "unrelated-package.zip").write_bytes(b"SYNTHETIC EXISTING ARCHIVE BYTES")
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    assert_rejected_before_archive(repository, commit, evidence, output, tmp_path / "archive-guard")
    assert {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()} == before


def test_correct_lock_can_package_from_an_unrelated_working_directory(
    tmp_path: Path, s0_source: tuple[Path, str, Path],
) -> None:
    repository, commit, evidence = s0_source
    lock = (repository / "requirements.lock").read_bytes()
    expected = hashlib.sha256(lock).hexdigest()
    assert json.loads((evidence / "verification.json").read_bytes())["dependency_lock_sha256"] == expected
    launcher = tmp_path / "unrelated-launch-directory"
    launcher.mkdir()
    (launcher / "requirements.lock").write_bytes(b"DECOY: THIS IS NOT THE SOURCE LOCK")
    output = tmp_path / "positive-synthetic-package"
    environment = dict(os.environ, PYTHONPATH=str(repository / "src"), PYTHONUTF8="1",
                       PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run([sys.executable, str(repository / "scripts/build_review_package.py"),
        "--commit", commit, "--evidence-dir", str(evidence), "--output-dir", str(output)],
        cwd=launcher, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
    assert result.returncode == 0, (result.stdout, result.stderr)
    directory = output / commit
    archive = output / f"BBAG_S0_GPT_Work_{commit[:12]}.zip"
    assert archive.is_file()
    with zipfile.ZipFile(directory / "source.zip") as source:
        assert source.read("requirements.lock") == lock
    verification = json.loads((directory / "evidence/verification.json").read_bytes())
    assert verification["dependency_lock_sha256"] == expected
    assert json.loads((directory / "review_request.json").read_bytes())["decision"] == "PENDING"


@pytest.mark.parametrize("git_flag", ["--assume-unchanged", "--skip-worktree"])
def test_matching_disk_and_evidence_hash_cannot_hide_a_different_committed_lock(
    tmp_path: Path, s0_source: tuple[Path, str, Path], git_flag: str,
) -> None:
    repository, commit, evidence = s0_source
    fixture_git(repository, "update-index", git_flag, "requirements.lock")
    lock = repository / "requirements.lock"
    lock.write_bytes(lock.read_bytes() + b"\n# SYNTHETIC HIDDEN DISK CHANGE\n")
    verification = json.loads((evidence / "verification.json").read_bytes())
    verification["dependency_lock_sha256"] = hashlib.sha256(lock.read_bytes()).hexdigest()
    write(evidence / "verification.json", verification)
    assert fixture_git(repository, "status", "--porcelain") == ""
    output = tmp_path / "must-not-be-created"
    assert_rejected_before_archive(repository, commit, evidence, output, tmp_path / "archive-guard")
    assert not output.exists()


@pytest.mark.parametrize("fault", ["wrong_zero", "missing", "malformed"])
def test_s1_cli_preserves_the_same_three_lock_rejections(
    tmp_path: Path, project_root: Path, fault: str,
) -> None:
    repository, commit = make_repository(tmp_path / "source")
    copy_script_source(project_root, repository)
    shutil.copy2(project_root / "scripts/build_stage1_package.py", repository / "scripts/build_stage1_package.py")
    copy_reference_source(repository)
    (repository / ".git/info/exclude").write_text("src/\nscripts/\nconfigs/\ntests/\n", encoding="utf-8")
    assert fixture_git(repository, "status", "--porcelain") == ""
    evidence = tmp_path / "SYNTHETIC_S1_EVIDENCE_ONLY"
    s1_synthetic_evidence(evidence, repository, commit)
    corrupt_lock_claim(evidence, fault)
    output = tmp_path / "must-not-be-created"
    result, marker = guarded_script(repository, "build_stage1_package.py", [
        "--commit", commit, "--evidence-dir", str(evidence), "--output-dir", str(output),
    ], tmp_path / "archive-guard")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "dependency_lock_sha256" in result.stderr or "dependency lock" in result.stderr.lower()
    assert "Traceback" not in result.stderr and not marker.exists()
    assert not output.exists()
