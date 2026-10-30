"""Real temporary Git boundaries; fixtures are not project commits or audit evidence."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

import pytest

from bbag.provenance import git_provenance
from bbag.contracts import canonical_protocol_bytes, load_protocol


def fixture_git(root: Path, *arguments: str, input_text: str | None = None) -> str:
    environment = os.environ.copy()
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *arguments],
        cwd=root, env=environment,
        input=input_text.encode("utf-8") if input_text is not None else None,
        capture_output=True, check=True, timeout=20,
    ).stdout.decode("utf-8").strip()


def make_repository(path: Path) -> tuple[Path, str]:
    """Use plumbing in a temporary fixture; no staging or git commit is performed."""
    if shutil.which("git") is None:
        pytest.skip("real Git executable is required for provenance boundary tests")
    path.mkdir()
    fixture_git(path, "init", "--initial-branch=fixture-main", "--object-format=sha1")
    tree = fixture_git(path, "mktree", input_text="")
    commit_text = (
        f"tree {tree}\n"
        "author Development Fixture <fixture@example.invalid> 1700000000 +0000\n"
        "committer Development Fixture <fixture@example.invalid> 1700000000 +0000\n"
        f"\nTemporary provenance fixture {path.name}\n"
    )
    commit = fixture_git(path, "hash-object", "-t", "commit", "-w", "--stdin", input_text=commit_text)
    fixture_git(path, "update-ref", "refs/heads/fixture-main", commit)
    assert fixture_git(path, "rev-parse", "HEAD") == commit
    return path, commit


def copy_script_source(project_root: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        project_root / "src" / "bbag", target / "src" / "bbag",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (target / "scripts").mkdir()
    for name in ("verify_stage0.py", "build_review_package.py"):
        shutil.copy2(project_root / "scripts" / name, target / "scripts" / name)


def guarded_script(
    source: Path, script: str, arguments: list[str], guard: Path
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Fail the test if rejection ever proceeds to verification or source archive."""
    guard.mkdir()
    marker = guard / "unexpected-command.txt"
    ready = guard / "guard-ready.txt"
    (guard / "sitecustomize.py").write_text(
        """import os
from pathlib import Path
import subprocess

Path(os.environ["BBAG_TEST_GUARD_READY"]).write_text("ready", encoding="utf-8")
_original_popen = subprocess.Popen

def guarded_popen(command, *args, **kwargs):
    parts = list(command) if isinstance(command, (list, tuple)) else [str(command)]
    executable = Path(str(parts[0])).name.lower() if parts else ""
    if executable not in {"git", "git.exe"} or "archive" in parts:
        Path(os.environ["BBAG_TEST_GUARD_MARKER"]).write_text(repr(command), encoding="utf-8")
        raise RuntimeError("Development test forbids full verification or source packaging")
    return _original_popen(command, *args, **kwargs)

subprocess.Popen = guarded_popen
""",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        PYTHONPATH=os.pathsep.join((str(guard), str(source / "src"))),
        PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
        BBAG_TEST_GUARD_MARKER=str(marker), BBAG_TEST_GUARD_READY=str(ready),
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
    )
    result = subprocess.run(
        [sys.executable, str(source / "scripts" / script), *arguments],
        cwd=source, env=environment, capture_output=True,
        text=True, encoding="utf-8", timeout=20, check=False,
    )
    assert ready.is_file(), "subprocess execution guard must actually be loaded"
    assert not marker.exists(), marker.read_text(encoding="utf-8") if marker.exists() else ""
    return result, marker


def test_gitless_directory_cannot_inherit_ancestor_head(tmp_path: Path) -> None:
    outer, _ = make_repository(tmp_path / "outer")
    inner = outer / "inner"
    inner.mkdir()
    assert not (inner / ".git").exists()
    assert git_provenance(inner) == {"commit": None, "branch": None, "dirty": True}


def test_real_nested_checkout_reports_own_head_and_dirty_state(tmp_path: Path) -> None:
    outer, outer_commit = make_repository(tmp_path / "outer")
    inner, inner_commit = make_repository(outer / "inner")
    assert inner_commit != outer_commit
    assert git_provenance(inner) == {
        "commit": inner_commit, "branch": "fixture-main", "dirty": False,
    }
    (inner / "untracked-fixture.txt").write_text("dirty fixture", encoding="utf-8")
    assert git_provenance(inner) == {
        "commit": inner_commit, "branch": "fixture-main", "dirty": True,
    }


def test_linked_worktree_git_file_is_valid_provenance(tmp_path: Path) -> None:
    repository, commit = make_repository(tmp_path / "repository")
    worktree = tmp_path / "linked-worktree"
    fixture_git(repository, "worktree", "add", "--detach", str(worktree), commit)
    assert (worktree / ".git").is_file()
    assert git_provenance(worktree) == {"commit": commit, "branch": "", "dirty": False}


@pytest.mark.parametrize("script", ["verify_stage0.py", "build_review_package.py"])
def test_scripts_reject_gitless_source_inside_ancestor_checkout(
    tmp_path: Path, project_root: Path, script: str
) -> None:
    outer, _ = make_repository(tmp_path / "outer")
    source = outer / "inner"
    copy_script_source(project_root, source)
    # A clean parent ensures "dirty checkout" cannot accidentally make this test pass.
    (outer / ".git" / "info" / "exclude").write_text("inner/\n", encoding="utf-8")
    assert fixture_git(outer, "status", "--porcelain") == ""
    output = tmp_path / "output"
    arguments = ["--output-dir", str(output)]
    if script == "build_review_package.py":
        arguments += ["--evidence-dir", str(tmp_path / "not-created-evidence")]
    result, _ = guarded_script(source, script, arguments, tmp_path / "guard")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "Git checkout root" in result.stderr
    assert not output.exists()


def make_synthetic_package_evidence(evidence: Path, commit: str, example_path: Path) -> None:
    """Only a temporary negative-test fixture, never an actual developer report."""
    evidence.mkdir()
    checks = []
    for name, expected in (
        ("pip_check", 0), ("pytest", 0), ("validate", 0),
        ("schemas", 0), ("experiment", 0), ("pending_review_gate", 3),
    ):
        checks.append({"name": name, "command": ["synthetic-fixture-only", name],
                       "exit_code": expected, "expected_exit_code": expected, "passed": True})
        for stream in ("stdout", "stderr"):
            (evidence / f"{name}.{stream}.txt").write_text(
                "Synthetic negative-test fixture; no verification was executed.\n", encoding="utf-8"
            )
    verification = {
        "stage": "S0", "implementation_commit": commit, "working_tree_dirty": False,
        "python": "synthetic fixture only", "checks": checks, "developer_checks_passed": True,
        "independent_review": "PENDING", "platform_integration": "NOT_APPLICABLE",
        "economic_evidence": "NOT_APPLICABLE", "next_stage_authorized": False,
    }
    receipt = {
        "schema_version": "1.0", "stage": "S0", "audited_commit": commit,
        "reviewer": "Synthetic pending fixture; not an independent reviewer", "decision": "PENDING",
        "engineering_status": "INCOMPLETE", "platform_status": "NOT_APPLICABLE",
        "economic_status": "NOT_APPLICABLE", "blocking_findings": 0,
        "evidence": [], "reviewed_at": None,
    }
    (evidence / "verification.json").write_text(json.dumps(verification), encoding="utf-8")
    (evidence / "review_request.json").write_text(json.dumps(receipt), encoding="utf-8")
    (evidence / "junit.xml").write_text(
        '<testsuites><testsuite name="synthetic_fixture_only" tests="1" failures="0" '
        'errors="0" skipped="0"><testcase name="fixture_not_actual_verification"/>'
        '</testsuite></testsuites>', encoding="utf-8",
    )
    experiment_dir = evidence / "experiments" / "s0-demo"
    experiment_dir.mkdir(parents=True)
    protocol = canonical_protocol_bytes(load_protocol(example_path))
    (experiment_dir / "protocol.json").write_bytes(protocol)
    record = {
        "schema_version": "1.0", "run_id": "s0-demo", "stage": "S0",
        "purpose": "ENGINEERING_TEST", "protocol_sha256": hashlib.sha256(protocol).hexdigest(),
        "code_commit": commit, "working_tree_dirty": False,
        "created_at": "2026-10-31T08:00:00+08:00", "validation_status": "VALIDATED",
        "platform_status": "NOT_RUN", "economic_status": "NOT_EVALUATED", "review_status": "PENDING",
    }
    (experiment_dir / "experiment.json").write_text(json.dumps(record), encoding="utf-8")


@pytest.mark.parametrize("missing", ["check", "raw_log"])
def test_packager_rejects_incomplete_evidence_before_source_archive(
    tmp_path: Path, project_root: Path, example_path: Path, missing: str
) -> None:
    repository, commit = make_repository(tmp_path / "repository")
    copy_script_source(project_root, repository)
    (repository / ".git" / "info" / "exclude").write_text("src/\nscripts/\n", encoding="utf-8")
    assert fixture_git(repository, "status", "--porcelain") == ""
    evidence = tmp_path / "synthetic-evidence-only"
    make_synthetic_package_evidence(evidence, commit, example_path)
    if missing == "check":
        path = evidence / "verification.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["checks"] = [record for record in payload["checks"] if record["name"] != "pytest"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        expected_error = "six required S0 checks"
    else:
        (evidence / "pytest.stdout.txt").unlink()
        expected_error = "raw verification output is missing"
    output = tmp_path / "audit-output"
    result, _ = guarded_script(
        repository, "build_review_package.py",
        ["--evidence-dir", str(evidence), "--output-dir", str(output)], tmp_path / "guard",
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert expected_error in result.stderr
    assert not output.exists()
