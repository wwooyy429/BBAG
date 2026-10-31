"""Synthetic S1 handoff controls; none of these fixtures is real approval."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

from bbag.factors import compute_factors
from bbag.labels import compute_labels
from bbag.provenance import json_bytes
from bbag.s1_contracts import (
    FactorResult, LabelResult, S1_REQUIRED_CHECKS, S1CheckRecord, S1ReviewReceipt, S1RunManifest, S1Verification,
    canonical_s1_bytes, load_s1_config,
)
from test_provenance_boundaries import fixture_git, guarded_script, make_repository


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SHA = "b" * 40
FIXTURE_TIME = datetime(2026, 10, 31, 3, tzinfo=timezone.utc)


def script(name: str):
    specification = importlib.util.spec_from_file_location("synthetic_" + name, ROOT / "scripts" / f"{name}.py")
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


PACKAGER = script("build_stage1_package")
VERIFIER = script("verify_stage1")


def write(path: Path, value: object) -> None:
    path.write_bytes(json_bytes(value))


def payload(path: Path) -> dict:
    return json.loads(path.read_bytes())


def copy_reference_source(target: Path) -> None:
    for name in ("requirements.lock", "configs/s1.example.json", "tests/fixtures/s1_expected.json",
                 "scripts/check_stage1_results.py"):
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)


def synthetic_evidence(evidence: Path, source: Path, commit: str = FIXTURE_SHA) -> None:
    """Produce valid typed artifacts while explicitly marking metadata synthetic."""
    evidence.mkdir()
    config = load_s1_config(source / "configs/s1.example.json")
    canonical = canonical_s1_bytes(config)
    records = []
    for name, expected in S1_REQUIRED_CHECKS.items():
        records.append(S1CheckRecord(name=name, command=["SYNTHETIC_METADATA_ONLY", name],
            exit_code=expected, expected_exit_code=expected, passed=True,
            started_at_utc=FIXTURE_TIME, duration_seconds=0.0))
        for stream in ("stdout", "stderr"):
            (evidence / f"{name}.{stream}.txt").write_text(
                "SYNTHETIC UNIT CONTROL ONLY; no subprocess or independent acceptance.\n", encoding="utf-8")
    verification = S1Verification(implementation_commit=commit, code_commit_end=commit,
        working_tree_dirty=False, working_tree_dirty_start=False, working_tree_dirty_end=False,
        generated_at_utc=FIXTURE_TIME, python="SYNTHETIC FIXTURE ONLY", python_executable="synthetic-python",
        platform="synthetic-unit-control", dependency_lock_sha256=hashlib.sha256((source / "requirements.lock").read_bytes()).hexdigest(),
        subprocess_output_encoding="Synthetic metadata only", checks=records, developer_checks_passed=True)
    write(evidence / "verification.json", verification.model_dump(mode="json"))
    receipt = S1ReviewReceipt(audited_commit=commit, reviewer="SYNTHETIC PENDING FIXTURE ONLY")
    write(evidence / "review_request.json", receipt.model_dump(mode="json"))
    (evidence / "junit.xml").write_text(
        '<testsuites><testsuite name="SYNTHETIC_UNIT_CONTROL" tests="1" failures="0" errors="0" skipped="0">'
        '<testcase name="synthetic_not_actual_verification"/></testsuite></testsuites>', encoding="utf-8")
    for run_id, kind in (("batch", "factors_batch"), ("incremental", "factors_incremental"), ("labels", "labels")):
        directory = evidence / "runs" / run_id
        directory.mkdir(parents=True)
        if run_id == "labels":
            result = compute_labels(config.panel, config.label, config.universe, config.as_of)
        else:
            result = compute_factors(config.panel, config.factor, config.audit_limits,
                                     config.universe, config.as_of, mode=run_id)
        raw = json_bytes(result.model_dump(mode="json"))
        record = S1RunManifest(run_id=run_id, kind=kind, config_sha256=hashlib.sha256(canonical).hexdigest(),
            result_sha256=hashlib.sha256(raw).hexdigest(), code_commit=commit,
            working_tree_dirty=False, created_at=FIXTURE_TIME)
        (directory / "config.json").write_bytes(canonical)
        (directory / "result.json").write_bytes(raw)
        write(directory / "run.json", record.model_dump(mode="json"))


@pytest.fixture
def evidence_control(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "synthetic-source-inputs"
    copy_reference_source(source)
    evidence = tmp_path / "SYNTHETIC_EVIDENCE_ONLY"
    synthetic_evidence(evidence, source)
    return evidence, source


def update_run_hashes(directory: Path) -> None:
    record = payload(directory / "run.json")
    record["config_sha256"] = hashlib.sha256((directory / "config.json").read_bytes()).hexdigest()
    record["result_sha256"] = hashlib.sha256((directory / "result.json").read_bytes()).hexdigest()
    write(directory / "run.json", record)


def test_valid_synthetic_handoff_control(evidence_control: tuple[Path, Path]) -> None:
    evidence, source = evidence_control
    verification, receipt, counts = PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)
    assert verification["stage"] == "S1" and receipt["decision"] == "PENDING"
    assert counts == {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}


def test_handoff_allows_honest_partial_skips(evidence_control: tuple[Path, Path]) -> None:
    evidence, source = evidence_control
    (evidence / "junit.xml").write_text(
        '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="1">'
        '<testcase/><testcase><skipped/></testcase></testsuite></testsuites>', encoding="utf-8")
    _, _, counts = PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)
    assert counts == {"tests": 2, "failures": 0, "errors": 0, "skipped": 1}


@pytest.mark.parametrize("fault", ["factor_signal", "unmatured_entry", "maturity"])
def test_valid_numeric_results_cannot_forge_timing_metadata(evidence_control: tuple[Path, Path], fault: str) -> None:
    evidence, source = evidence_control
    run_id = "batch" if fault == "factor_signal" else "labels"
    directory = evidence / "runs" / run_id
    result = payload(directory / "result.json")
    if fault == "factor_signal":
        row = result["rows"][0]
        actual = datetime.fromisoformat(row["signal_at"].replace("Z", "+00:00"))
        row["signal_at"] = (actual - timedelta(minutes=1)).isoformat()
    else:
        row = next(item for item in result["rows"] if (
            item["mature_at"] is not None if fault == "maturity"
            else item["status"] != "MATURED" and item["entry_at"] is not None))
        name = "entry_at" if fault == "unmatured_entry" else "mature_at"
        actual = datetime.fromisoformat(row[name].replace("Z", "+00:00"))
        row[name] = (actual + timedelta(minutes=1)).isoformat()
    model = FactorResult if run_id == "batch" else LabelResult
    checked = model.model_validate_json(json.dumps(result))
    (directory / "result.json").write_bytes(json_bytes(checked.model_dump(mode="json")))
    update_run_hashes(directory)
    with pytest.raises(ValueError, match="timestamp"):
        PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)


def test_schedule_binds_unmatured_maturity_under_earlier_valid_cutoff(evidence_control: tuple[Path, Path]) -> None:
    _, source = evidence_control
    config = load_s1_config(source / "configs/s1.example.json")
    config.as_of = config.panel.sessions[1].signal_at
    result = compute_labels(config.panel, config.label, config.universe, config.as_of)
    PACKAGER.validate_schedule(result, config)
    changed = result.model_dump(mode="json")
    row = next(item for item in changed["rows"] if item["status"] == "NOT_MATURED" and item["mature_at"] is not None)
    actual = datetime.fromisoformat(row["mature_at"].replace("Z", "+00:00"))
    row["mature_at"] = (actual + timedelta(minutes=1)).isoformat()
    typed = LabelResult.model_validate_json(json.dumps(changed))
    with pytest.raises(ValueError, match="maturity timestamp"):
        PACKAGER.validate_schedule(typed, config)


@pytest.mark.parametrize("fault", [
    "missing_check", "duplicate_check", "boolean_exit", "failed_check", "dirty_source",
    "wrong_commit", "wrong_lock", "extra_verification", "missing_raw", "duplicate_json",
    "s0_receipt", "approved_request", "naive_check_time", "negative_duration",
])
def test_invalid_verification_or_request_is_rejected(evidence_control: tuple[Path, Path], fault: str) -> None:
    evidence, source = evidence_control
    verification = payload(evidence / "verification.json")
    receipt = payload(evidence / "review_request.json")
    if fault == "missing_check":
        verification["checks"].pop()
    elif fault == "duplicate_check":
        verification["checks"][-1] = verification["checks"][0]
    elif fault == "boolean_exit":
        verification["checks"][0]["exit_code"] = False
    elif fault == "failed_check":
        verification["checks"][0].update(exit_code=1, passed=False)
        verification["developer_checks_passed"] = False
    elif fault == "dirty_source":
        verification.update(working_tree_dirty=True, working_tree_dirty_end=True, developer_checks_passed=False)
    elif fault == "wrong_commit":
        verification.update(implementation_commit="a" * 40, code_commit_end="a" * 40)
    elif fault == "wrong_lock":
        verification["dependency_lock_sha256"] = "a" * 64
    elif fault == "extra_verification":
        verification["unexpected"] = True
    elif fault == "missing_raw":
        (evidence / "audit.stdout.txt").unlink()
    elif fault == "s0_receipt":
        receipt["stage"] = "S0"
    elif fault == "approved_request":
        receipt.update(decision="APPROVED", engineering_status="PASS", evidence=["SYNTHETIC_NOT_REAL_APPROVAL"],
                       reviewed_at=FIXTURE_TIME.isoformat())
    elif fault == "naive_check_time":
        verification["checks"][0]["started_at_utc"] = "2026-10-31T03:00:00"
    elif fault == "negative_duration":
        verification["checks"][0]["duration_seconds"] = -1.0
    write(evidence / "verification.json", verification)
    write(evidence / "review_request.json", receipt)
    if fault == "duplicate_json":
        path = evidence / "verification.json"
        path.write_text('{"independent_review":"APPROVED",' + path.read_text(encoding="utf-8")[1:], encoding="utf-8")
    with pytest.raises(ValueError):
        PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)


@pytest.mark.parametrize("fault", [
    "missing_timestamp", "naive_timestamp", "wrong_run_kind", "dirty_run", "extra_run",
    "wrong_result_hash", "noncanonical_config", "valid_but_different_config", "wrong_result_mode",
    "invalid_result_type", "extra_result", "differing_incremental", "wrong_hand_answer_both_modes",
    "duplicate_run_json", "missing_result", "wrong_label_rule",
])
def test_invalid_run_contract_hash_input_or_reference_is_rejected(
    evidence_control: tuple[Path, Path], fault: str,
) -> None:
    evidence, source = evidence_control
    directory = evidence / "runs/batch"
    record = payload(directory / "run.json")
    if fault == "missing_timestamp":
        del record["created_at"]
    elif fault == "naive_timestamp":
        record["created_at"] = "2026-10-31T03:00:00"
    elif fault == "wrong_run_kind":
        record["kind"] = "factors_incremental"
    elif fault == "dirty_run":
        record["working_tree_dirty"] = True
    elif fault == "extra_run":
        record["unexpected"] = True
    elif fault == "wrong_result_hash":
        record["result_sha256"] = "a" * 64
    write(directory / "run.json", record)
    if fault in {"noncanonical_config", "valid_but_different_config"}:
        config = payload(directory / "config.json")
        if fault == "valid_but_different_config":
            config["pending_decisions"].append("SYNTHETIC DIFFERENT INPUT")
        if fault == "noncanonical_config":
            write(directory / "config.json", config)
        else:
            from bbag.s1_contracts import S1Config
            (directory / "config.json").write_bytes(canonical_s1_bytes(S1Config.model_validate_json(json.dumps(config))))
        update_run_hashes(directory)
    elif fault in {"wrong_result_mode", "invalid_result_type", "extra_result"}:
        result = payload(directory / "result.json")
        if fault == "wrong_result_mode":
            result["mode"] = "incremental"
        elif fault == "invalid_result_type":
            result["rows"][0]["value"] = "bad"
        else:
            result["unexpected"] = True
        write(directory / "result.json", result)
        update_run_hashes(directory)
    elif fault in {"differing_incremental", "wrong_hand_answer_both_modes"}:
        for run_id in (("incremental",) if fault == "differing_incremental" else ("batch", "incremental")):
            changed = evidence / "runs" / run_id
            result = payload(changed / "result.json")
            row = next(item for item in result["rows"] if item["value"] is not None)
            row["value"] += 0.1
            write(changed / "result.json", result)
            update_run_hashes(changed)
    elif fault == "duplicate_run_json":
        path = directory / "run.json"
        path.write_text('{"kind":"labels",' + path.read_text(encoding="utf-8")[1:], encoding="utf-8")
    elif fault == "missing_result":
        (directory / "result.json").unlink()
    elif fault == "wrong_label_rule":
        directory = evidence / "runs/labels"
        result = payload(directory / "result.json")
        result["rule"]["horizon_sessions"] += 1
        write(directory / "result.json", result)
        update_run_hashes(directory)
    with pytest.raises((ValueError, OSError)):
        PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)


@pytest.mark.parametrize("child", ["", "<testcase><failure/></testcase>", "<testcase><skipped/></testcase>"])
def test_junit_summary_cannot_replace_executed_details(evidence_control: tuple[Path, Path], child: str) -> None:
    evidence, source = evidence_control
    (evidence / "junit.xml").write_text('<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
                                       + child + '</testsuite></testsuites>', encoding="utf-8")
    with pytest.raises(ValueError):
        PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)


def test_report_only_claims_actual_optional_history(evidence_control: tuple[Path, Path]) -> None:
    evidence, source = evidence_control
    verification, _, counts = PACKAGER.validate_evidence(evidence, FIXTURE_SHA, source)
    absent = PACKAGER.development_report(ROOT, evidence, FIXTURE_SHA, verification, counts)
    assert "本包未包含可选历史审核目录" in absent
    assert "`evidence/prior_audit/`" not in absent
    (evidence / "prior_audit").mkdir()
    present = PACKAGER.development_report(ROOT, evidence, FIXTURE_SHA, verification, counts)
    assert "本包实际包含历史输入目录：`evidence/prior_audit/`" in present
    assert "独立审核保持 PENDING" in present and "不开始 S2" in present


def test_collector_has_exact_s1_commands_and_fixed_expected_exits(tmp_path: Path) -> None:
    commands = VERIFIER.commands(tmp_path / "new-evidence")
    assert {name: expected for name, _, expected in commands} == S1_REQUIRED_CHECKS
    assert len(commands) == 10
    assert next(args for name, args, _ in commands if name == "pytest")[:2] == ["-m", "pytest"]
    assert next(args for name, args, _ in commands if name == "pending_review_gate") == [
        "-m", "bbag", "check-review", "--stage", "S1", "--receipt",
        str(tmp_path / "new-evidence/review_request.json"), "--commit", "{commit}"]
    assert "scripts/verify_stage0.py" not in str(commands)


@pytest.mark.parametrize("name", ["verify_stage1", "build_stage1_package"])
def test_s1_scripts_reject_gitless_child_before_any_execution(tmp_path: Path, name: str) -> None:
    outer, _ = make_repository(tmp_path / "outer")
    source = outer / "source"
    shutil.copytree(ROOT / "src/bbag", source / "src/bbag", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (source / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / f"{name}.py", source / "scripts" / f"{name}.py")
    (outer / ".git/info/exclude").write_text("source/\n", encoding="utf-8")
    assert fixture_git(outer, "status", "--porcelain") == ""
    output = tmp_path / "never-created"
    arguments = ["--output-dir", str(output)]
    if name == "build_stage1_package":
        arguments += ["--evidence-dir", str(tmp_path / "missing-evidence")]
    result, _ = guarded_script(source, f"{name}.py", arguments, tmp_path / "guard")
    assert result.returncode == 2
    assert "Git checkout root" in result.stderr
    assert not output.exists()


def test_s1_package_archive_collision_precedes_any_new_output(tmp_path: Path) -> None:
    repository, commit = make_repository(tmp_path / "repository")
    shutil.copytree(ROOT / "src/bbag", repository / "src/bbag", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (repository / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/build_stage1_package.py", repository / "scripts/build_stage1_package.py")
    (repository / ".git/info/exclude").write_text("src/\nscripts/\n", encoding="utf-8")
    output = tmp_path / "existing-output"
    output.mkdir()
    archive = output / f"BBAG_S1_GPT_Work_{commit[:12]}.zip"
    original = b"SYNTHETIC EXISTING ARCHIVE MUST NOT CHANGE"
    archive.write_bytes(original)
    evidence = tmp_path / "SYNTHETIC_EMPTY_EVIDENCE"
    evidence.mkdir()
    result, _ = guarded_script(repository, "build_stage1_package.py", [
        "--commit", commit, "--evidence-dir", str(evidence), "--output-dir", str(output)], tmp_path / "guard")
    assert result.returncode == 2 and "already exists" in result.stderr
    assert archive.read_bytes() == original and not (output / commit).exists()
