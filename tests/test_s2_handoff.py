"""Synthetic S2 packaging controls, not actual verification or acceptance."""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import zipfile

import pytest

from bbag import s2_cli
from bbag.provenance import json_bytes
from bbag.s2_contracts import S2CheckRecord, S2ReviewReceipt, S2Verification
from bbag.s2_quantconnect import QuantConnectCheckResponse
from test_provenance_boundaries import fixture_git, guarded_script, make_repository


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SHA = "b" * 40
FIXTURE_TIME = datetime(2026, 10, 31, tzinfo=timezone.utc)


def script(name):
    spec = importlib.util.spec_from_file_location("synthetic_" + name, ROOT / "scripts" / (name + ".py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PACKAGER = script("build_stage2_package")
VERIFIER = script("verify_stage2")


def write(path, value):
    path.write_bytes(json_bytes(value))


def payload(path):
    return json.loads(path.read_bytes())


def copy_reference_source(target):
    for name in ("requirements.lock", "configs/s2.example.json", "configs/s2.cloud-scope.json", "configs/s2.offline-campaign.json", "tests/fixtures/s2_raw.json", "tests/fixtures/s2_expected.json", "scripts/check_stage2_results.py"):
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)


def synthetic_evidence(evidence, source, monkeypatch, commit=FIXTURE_SHA):
    evidence.mkdir()
    monkeypatch.setattr(s2_cli, "git_provenance", lambda: {"commit": commit, "dirty": False})
    s2_cli.synthetic_command(source / "configs/s2.example.json", evidence / "runs", "synthetic")
    records = []
    for name, args, expected in VERIFIER.commands(evidence):
        command = [sys.executable, *(commit if value == "{commit}" else value for value in args)]
        records.append(S2CheckRecord(name=name, command=command, started_at_utc=FIXTURE_TIME,
            duration_seconds=0.0, exit_code=expected, expected_exit_code=expected, passed=True))
        for stream in ("stdout", "stderr"):
            (evidence / f"{name}.{stream}.txt").write_text("SYNTHETIC UNIT CONTROL ONLY; no subprocess/independent acceptance.\n", encoding="utf-8")
    verification = S2Verification(implementation_commit=commit, code_commit_end=commit, working_tree_dirty_start=False, working_tree_dirty_end=False,
        generated_at_utc=FIXTURE_TIME, python="SYNTHETIC FIXTURE ONLY", platform="synthetic-unit-control",
        dependency_lock_sha256=hashlib.sha256((source / "requirements.lock").read_bytes()).hexdigest(), checks=records, developer_checks_passed=True)
    write(evidence / "verification.json", verification.model_dump(mode="json"))
    write(evidence / "review_request.json", S2ReviewReceipt(audited_commit=commit, reviewer="SYNTHETIC PENDING FIXTURE ONLY").model_dump(mode="json"))
    (evidence / "junit.xml").write_text('<testsuites><testsuite name="synthetic_only" tests="1" failures="0" errors="0" skipped="0"><testcase name="synthetic_not_verification"/></testsuite></testsuites>', encoding="utf-8")


@pytest.fixture
def control(tmp_path, monkeypatch):
    source = tmp_path / "synthetic-source"
    copy_reference_source(source)
    evidence = tmp_path / "SYNTHETIC_EVIDENCE_ONLY"
    synthetic_evidence(evidence, source, monkeypatch)
    return evidence, source


def rehash_result(evidence):
    directory = evidence / "runs/synthetic"
    record = payload(directory / "run.json")
    record["result_sha256"] = hashlib.sha256((directory / "result.json").read_bytes()).hexdigest()
    write(directory / "run.json", record)


def test_valid_fixed_synthetic_handoff_and_relocation_are_readonly(control, tmp_path):
    evidence, source = control
    db = evidence / "runs/synthetic/task_ledger.sqlite"
    before = db.read_bytes()
    verification, receipt, counts = PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)
    assert verification["stage"] == "S2" and verification["platform_integration"] == "NOT_RUN"
    assert receipt["decision"] == "PENDING"
    assert counts == dict(tests=1, failures=0, errors=0, skipped=0)
    assert db.read_bytes() == before
    copied = tmp_path / "relocated-evidence"
    shutil.copytree(evidence, copied)
    PACKAGER.validate_evidence(copied, FIXTURE_SHA, source)
    assert (copied / "runs/synthetic/task_ledger.sqlite").read_bytes() == before


def test_collector_has_exact_ten_s2_commands(tmp_path):
    commands = VERIFIER.commands(tmp_path / "fresh")
    assert [name for name, _, _ in commands] == ["pip_check", "pytest", "schemas_s0", "schemas_s1", "schemas_s2", "config_synthetic", "config_cloud", "synthetic_run", "reference_check", "pending_review_gate"]
    assert [code for _, _, code in commands] == [0] * 9 + [3]
    assert "scripts/check_stage2_results.py" in next(args for name, args, _ in commands if name == "reference_check")
    assert next(args for name, args, _ in commands if name == "pending_review_gate")[4] == "S2"


@pytest.mark.parametrize("fault", ["missing_check", "duplicate_check", "boolean_exit", "wrong_command", "failed", "dirty", "wrong_sha", "wrong_lock", "extra", "missing_raw", "duplicate_json", "approved", "naive_time", "negative_duration"])
def test_verification_protocol_and_unfilled_request_are_required(control, fault):
    evidence, source = control
    verification = payload(evidence / "verification.json")
    receipt = payload(evidence / "review_request.json")
    if fault == "missing_check": verification["checks"].pop()
    elif fault == "duplicate_check": verification["checks"][-1] = verification["checks"][0]
    elif fault == "boolean_exit": verification["checks"][0]["exit_code"] = False
    elif fault == "wrong_command": verification["checks"][0]["command"] = [sys.executable, "-c", "print('pretend pass')"]
    elif fault == "failed":
        verification["checks"][0].update(exit_code=1, passed=False)
        verification["developer_checks_passed"] = False
    elif fault == "dirty": verification.update(working_tree_dirty_end=True, developer_checks_passed=False)
    elif fault == "wrong_sha": verification.update(implementation_commit="a" * 40, code_commit_end="a" * 40)
    elif fault == "wrong_lock": verification["dependency_lock_sha256"] = "a" * 64
    elif fault == "extra": verification["unexpected"] = True
    elif fault == "missing_raw": (evidence / "schemas_s2.stdout.txt").unlink()
    elif fault == "approved": receipt.update(decision="APPROVED", engineering_status="PASS", platform_status="PASS", evidence=["SYNTHETIC_NOT_REAL"], reviewed_at=FIXTURE_TIME.isoformat())
    elif fault == "naive_time": verification["checks"][0]["started_at_utc"] = "2026-10-31T00:00:00"
    elif fault == "negative_duration": verification["checks"][0]["duration_seconds"] = -1.0
    write(evidence / "verification.json", verification)
    write(evidence / "review_request.json", receipt)
    if fault == "duplicate_json":
        path = evidence / "verification.json"
        path.write_text('{"stage":"S1",' + path.read_text(encoding="utf-8")[1:], encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)


@pytest.mark.parametrize("child", ["", "<testcase><failure/></testcase>", "<testcase><skipped/></testcase>"])
def test_junit_cannot_substitute_summary_for_executed_passing_details(control, child):
    evidence, source = control
    (evidence / "junit.xml").write_text('<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">' + child + '</testsuite></testsuites>', encoding="utf-8")
    with pytest.raises(ValueError):
        PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)


@pytest.mark.parametrize("fault", ["dirty_run", "wrong_run_sha", "wrong_result_hash", "extra_run", "noncanonical_config", "different_config", "numeric_wrong_reference", "fake_task_source", "metadata_tamper", "metadata_missing", "extra_metadata", "db_corrupt", "db_wrong_spec", "db_duplicate_json"])
def test_fixed_run_source_references_metadata_and_database_are_bound(control, fault):
    evidence, source = control
    directory = evidence / "runs/synthetic"
    record = payload(directory / "run.json")
    if fault == "dirty_run": record["working_tree_dirty"] = True
    elif fault == "wrong_run_sha": record["code_commit"] = "a" * 40
    elif fault == "wrong_result_hash": record["result_sha256"] = "a" * 64
    elif fault == "extra_run": record["unexpected"] = True
    write(directory / "run.json", record)
    if fault in {"noncanonical_config", "different_config"}:
        config = payload(directory / "config.json")
        if fault == "different_config": config["pending_decisions"].append("SYNTHETIC DIFFERENT INPUT")
        write(directory / "config.json", config)
    elif fault in {"numeric_wrong_reference", "fake_task_source"}:
        result = payload(directory / "result.json")
        if fault == "numeric_wrong_reference": result["quality"]["coverage"] = 0.123
        else: result["task"]["spec"]["source_commit"] = "a" * 40
        write(directory / "result.json", result)
        rehash_result(evidence)
    elif fault in {"metadata_tamper", "metadata_missing", "extra_metadata"}:
        path = next((directory / "metadata").iterdir())
        if fault == "metadata_missing": path.unlink()
        elif fault == "extra_metadata": (directory / "metadata/extra.json").write_text("{}")
        else: path.write_bytes(path.read_bytes() + b" ")
    elif fault == "db_corrupt": (directory / "task_ledger.sqlite").write_bytes(b"not SQLite")
    elif fault in {"db_wrong_spec", "db_duplicate_json"}:
        with sqlite3.connect(directory / "task_ledger.sqlite") as connection:
            value = connection.execute("SELECT spec_json FROM tasks").fetchone()[0]
            if fault == "db_duplicate_json": value = '{"source_commit":"' + "a" * 40 + '",' + value[1:]
            else: value = value.replace(FIXTURE_SHA, "a" * 40)
            connection.execute("UPDATE tasks SET spec_json=?", (value,))
    with pytest.raises((ValueError, OSError)):
        PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)


def test_optional_platform_attachment_is_control_only_and_never_approval(control):
    evidence, source = control
    platform = evidence / "platform"
    platform.mkdir()
    response = QuantConnectCheckResponse(project_id=1, request_sha256="a" * 64, source_commit=FIXTURE_SHA, config_sha256="c" * 64,
        status="COMPLETED", check_results={"no_data_export": True})
    write(platform / "response.json", response.model_dump(mode="json"))
    verification, receipt, counts = PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)
    report = PACKAGER.development_report(ROOT, evidence, FIXTURE_SHA, verification, counts)
    assert receipt["decision"] == "PENDING" and receipt["platform_status"] == "NOT_RUN"
    assert "导入本身不认证" in report and "不开始 S3" in report
    assert "evidence/platform" in report
    raw = payload(platform / "response.json")
    raw["prices"] = [10.0]
    write(platform / "response.json", raw)
    with pytest.raises(ValueError, match="allowlisted"):
        PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)


@pytest.mark.parametrize("fault", ["raw_text", "wrong_source", "unknown_extra"])
def test_unbounded_or_wrong_source_optional_evidence_is_rejected(control, fault):
    evidence, source = control
    if fault == "unknown_extra": (evidence / "unlicensed.csv").write_text("private")
    else:
        platform = evidence / "platform"
        platform.mkdir()
        if fault == "raw_text": (platform / "raw.log").write_text("raw provider logs forbidden")
        else:
            response = QuantConnectCheckResponse(project_id=1, request_sha256="a" * 64, source_commit="a" * 40, config_sha256="c" * 64, status="UNKNOWN")
            write(platform / "wrong.json", response.model_dump(mode="json"))
    with pytest.raises(ValueError): PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)


@pytest.mark.parametrize("name", ["verify_stage2", "build_stage2_package"])
def test_gitless_child_is_rejected_before_any_execution(tmp_path, name):
    outer, _ = make_repository(tmp_path / "outer")
    source = outer / "source"
    shutil.copytree(ROOT / "src/bbag", source / "src/bbag", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (source / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / (name + ".py"), source / "scripts" / (name + ".py"))
    (outer / ".git/info/exclude").write_text("source/\n", encoding="utf-8")
    assert fixture_git(outer, "status", "--porcelain") == ""
    output = tmp_path / "never-created"
    args = ["--output-dir", str(output)]
    if name == "build_stage2_package": args += ["--evidence-dir", str(tmp_path / "absent")]
    result, _ = guarded_script(source, name + ".py", args, tmp_path / "guard")
    assert result.returncode == 2 and "Git checkout root" in result.stderr
    assert not output.exists()


def test_archive_collision_precedes_new_package_output(tmp_path):
    repository, commit = make_repository(tmp_path / "repository")
    shutil.copytree(ROOT / "src/bbag", repository / "src/bbag", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (repository / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/build_stage2_package.py", repository / "scripts/build_stage2_package.py")
    (repository / ".git/info/exclude").write_text("src/\nscripts/\n", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    archive = output / f"BBAG_S2_GPT_Work_{commit[:12]}.zip"
    original = b"SYNTHETIC EXISTING ARCHIVE MUST NOT CHANGE"
    archive.write_bytes(original)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    result, _ = guarded_script(repository, "build_stage2_package.py", ["--commit", commit, "--evidence-dir", str(evidence), "--output-dir", str(output)], tmp_path / "guard")
    assert result.returncode == 2 and "already exists" in result.stderr
    assert archive.read_bytes() == original and not (output / commit).exists()


def test_actual_temporary_git_package_has_exact_source_and_zip_inventory(tmp_path, monkeypatch):
    """An actual fixture archive proves packaging behavior, never project approval."""
    repository, _ = make_repository(tmp_path / "temporary-git")
    copy_reference_source(repository)
    shutil.copytree(ROOT / "src/bbag", repository / "src/bbag", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("verify_stage2.py", "build_stage2_package.py"):
        shutil.copy2(ROOT / "scripts" / name, repository / "scripts" / name)
    fixture_git(repository, "add", ".")
    tree = fixture_git(repository, "write-tree")
    commit_text = (f"tree {tree}\n" "author Unit Fixture <fixture@example.invalid> 1700000000 +0000\n"
        "committer Unit Fixture <fixture@example.invalid> 1700000000 +0000\n\nSynthetic packaging control only\n")
    commit = fixture_git(repository, "hash-object", "-t", "commit", "-w", "--stdin", input_text=commit_text)
    fixture_git(repository, "update-ref", "refs/heads/fixture-main", commit)
    assert fixture_git(repository, "status", "--porcelain") == ""
    evidence = tmp_path / "SYNTHETIC_INPUT_METADATA"
    synthetic_evidence(evidence, repository, monkeypatch, commit=commit)
    output = tmp_path / "package-output"
    environment = dict(os.environ, PYTHONPATH=str(repository / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    result = subprocess.run([sys.executable, str(repository / "scripts/build_stage2_package.py"), "--commit", commit,
        "--evidence-dir", str(evidence), "--output-dir", str(output)], cwd=repository, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    directory = output / commit
    manifest = payload(directory / "manifest.json")
    assert manifest["implementation_commit"] == commit and manifest["independent_review"] == "PENDING"
    assert manifest["platform_integration"] == "NOT_RUN" and manifest["next_stage_authorized"] is False
    for item in manifest["files"]:
        raw = (directory / item["path"]).read_bytes()
        assert len(raw) == item["bytes"] and hashlib.sha256(raw).hexdigest() == item["sha256"]
    with zipfile.ZipFile(directory / "source.zip") as archive:
        names = set(archive.namelist())
        tracked = set(fixture_git(repository, "ls-files").splitlines())
        assert {name for name in names if not name.endswith("/")} == tracked
        assert not any(set(Path(name).parts) & {".git", ".venv", "artifacts", "deliverables"} for name in names)
        for name in tracked:
            assert archive.read(name) == subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=repository)
    outer = Path(report["package"])
    assert hashlib.sha256(outer.read_bytes()).hexdigest() == report["package_sha256"]
    with zipfile.ZipFile(outer) as archive:
        files = {path.relative_to(directory).as_posix(): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
        assert set(archive.namelist()) == set(files)
        assert all(archive.read(name) == raw for name, raw in files.items())
    assert fixture_git(repository, "status", "--porcelain") == ""


def test_s2_evidence_links_and_output_overlap_reuse_shared_boundaries(control, tmp_path):
    evidence, source = control
    link = evidence / "redirected.json"
    try:
        link.symlink_to(source / "requirements.lock")
    except OSError:
        pytest.skip("symlink creation requires privileges on this Windows host")
    with pytest.raises(ValueError, match="links|junctions"):
        PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)
    link.unlink()
    with pytest.raises(ValueError, match="overlap"):
        PACKAGER.validate_package_paths(evidence, evidence / "nested-output", tmp_path / "output.zip")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO boundary is not available on Windows")
def test_s2_evidence_fifo_is_rejected_before_read(control):
    evidence, source = control
    os.mkfifo(evidence / "unsafe_fifo")
    with pytest.raises(ValueError, match="non-regular"):
        PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)
