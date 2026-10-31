"""S2 integration with SYNTHETIC_TEST data and receipt fixtures only."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import socket

from pydantic import ValidationError
import pytest

from bbag import s2_cli
from bbag.provenance import git_provenance, json_bytes
from bbag.s2_access import AccessGuard
from bbag.s2_contracts import (
    S2Config, S2ReviewReceipt, S2Verification, S2_REQUIRED_CHECKS,
    canonical_s2_config, load_s2_config,
)
from bbag.s2_quantconnect import QuantConnectCheckRequest, QuantConnectGateway


def write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(payload))
    return path


def read(path: Path) -> dict:
    return json.loads(path.read_bytes())


@pytest.fixture
def synthetic_config(project_root: Path) -> Path:
    return project_root / "configs/s2.example.json"


@pytest.fixture
def cloud_config(project_root: Path) -> Path:
    return project_root / "configs/s2.cloud-scope.json"


@pytest.fixture
def reference_checker(project_root: Path):
    spec = importlib.util.spec_from_file_location(
        "s2_reference_checker_integration", project_root / "scripts/check_stage2_results.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def synthetic_run(tmp_path: Path, synthetic_config: Path, run_cli, cli_json):
    result = run_cli("s2-run-synthetic", "--config", synthetic_config,
                     "--output-dir", tmp_path, "--run-id", "synthetic-integration")
    assert result.returncode == 0, result.stderr
    output = cli_json(result)
    assert output["fixture_class"] == "SYNTHETIC_TEST"
    assert output["platform_execution"] == "NOT_RUN"
    return tmp_path / "synthetic-integration"


@pytest.mark.parametrize("name", ["s2.example.json", "s2.cloud-scope.json"])
def test_s2_examples_are_typed_drafts_with_stable_canonical_values(project_root: Path, name: str):
    config = load_s2_config(project_root / "configs" / name)
    assert config.purpose == "ENGINEERING_TEST" and config.status == "DRAFT"
    assert config.stage == "S2" and config.pending_decisions
    assert canonical_s2_config(config) == canonical_s2_config(
        S2Config.model_validate_json(config.model_dump_json())
    )
    assert (config.bundle is None) == (config.request.provider == "QUANTCONNECT_CLOUD")
    if config.bundle is None:
        assert config.request.output_mode == "CONTROL_ONLY"
        assert config.task_budget.operation_limits == {"compile": 2, "research_check": 3}
        assert config.task_budget.max_cost_microusd == 0


@pytest.mark.parametrize("fault", ["extra_nested", "boolean_attempts", "outside_dates", "empty_pending"])
def test_s2_config_rejects_type_shape_and_scope_faults(synthetic_config: Path, fault: str):
    payload = read(synthetic_config)
    if fault == "extra_nested":
        payload["bundle"]["provenance"]["raw_history"] = [123.0]
    elif fault == "boolean_attempts":
        payload["task_budget"]["max_attempts"] = True
    elif fault == "outside_dates":
        payload["request"]["end"] = "2024-01-06"
    else:
        payload["pending_decisions"] = []
    with pytest.raises(ValidationError):
        S2Config.model_validate_json(json.dumps(payload))


def test_s2_loader_and_cli_reject_nested_duplicate_json_keys(tmp_path: Path, synthetic_config: Path, run_cli, cli_json):
    content = synthetic_config.read_text(encoding="utf-8")
    content = content.replace('"max_attempts": 1,', '"max_attempts": 0, "max_attempts": 1,', 1)
    path = tmp_path / "ambiguous.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_s2_config(path)
    result = run_cli("s2-validate", "--config", path)
    assert result.returncode == 2
    assert cli_json(result)["valid"] is False


def test_s2_canonicalization_revalidates_mutated_nested_contracts(synthetic_config: Path):
    config = load_s2_config(synthetic_config)
    invalid = config.task_budget.model_copy(update={"max_attempts": True})
    bypassed = config.model_copy(update={"task_budget": invalid})
    with pytest.raises(ValidationError):
        canonical_s2_config(bypassed)
    config.bundle.bars[0].__dict__["close"] = -1.0
    with pytest.raises(ValidationError):
        canonical_s2_config(config)


def test_cloud_validation_does_not_register_a_runtime_grant(cloud_config: Path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("configuration validation must not enroll a grant or dispatch")
    with monkeypatch.context() as controlled:
        controlled.setattr(AccessGuard, "__init__", forbidden)
        controlled.setattr(QuantConnectGateway, "__init__", forbidden)
        result = s2_cli.validate_command(cloud_config)
    assert result["valid"] is True
    assert result["runtime_grant_enrolled"] is result["research_ready"] is False
    assert result["platform_execution"] == "NOT_RUN"
    config = load_s2_config(cloud_config)
    with pytest.raises(ValueError, match="not a trusted runtime grant"):
        AccessGuard(config.policy).authorize(config.request, "cloud_dispatch")


def test_cloud_plan_is_only_bounded_control_metadata_without_transport(cloud_config: Path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("planning must not invoke remote transport or enroll runtime grants")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(QuantConnectGateway, "__init__", forbidden)
    monkeypatch.setattr(s2_cli.TaskLedger, "__init__", forbidden)
    monkeypatch.setattr(AccessGuard, "__init__", forbidden)
    monkeypatch.setattr(s2_cli, "git_provenance", lambda: {"commit": "b" * 40, "dirty": True})
    result = s2_cli.plan_command(cloud_config, 123)
    plan = QuantConnectCheckRequest.model_validate_json(json.dumps(result["plan"]))
    assert plan.data.assets == ["SPY", "AAPL"]
    assert plan.data.start.isoformat() == "2020-01-02" and plan.data.end.isoformat() == "2020-01-10"
    assert plan.source_commit == "b" * 40
    assert plan.output_policy == "verdicts_only" and plan.data_retention == "cloud_only"
    assert result["runtime_grant_enrolled"] is False and result["platform_execution"] == "NOT_RUN"
    serialized = json.dumps(result)
    for forbidden_key in ('"bars"', '"observations"', '"bundle"', '"source_sha256"', '"raw_prices"'):
        assert forbidden_key not in serialized


def test_s1_receipt_cannot_approve_s2(tmp_path: Path, approved_receipt_payload: dict, run_cli, cli_json):
    receipt = {**approved_receipt_payload, "stage": "S1"}
    path = write(tmp_path / "s1-receipt.json", receipt)
    result = run_cli("check-review", "--stage", "S2", "--receipt", path, "--commit", "a" * 40)
    assert result.returncode == 2
    assert cli_json(result)["valid"] is False


@pytest.mark.parametrize("fault", ["no_platform_pass", "no_evidence", "no_time"])
def test_s2_approval_needs_platform_pass_and_actual_review_evidence(approved_receipt_payload: dict, fault: str):
    receipt = {**approved_receipt_payload, "stage": "S2", "platform_status": "PASS"}
    if fault == "no_platform_pass":
        receipt["platform_status"] = "NOT_RUN"
    elif fault == "no_evidence":
        receipt["evidence"] = []
    else:
        receipt["reviewed_at"] = None
    with pytest.raises(ValidationError):
        S2ReviewReceipt.model_validate_json(json.dumps(receipt))


def test_s2_receipt_gate_remains_advisory_and_exact_commit_bound(tmp_path: Path, approved_receipt_payload: dict, run_cli, cli_json):
    receipt = {**approved_receipt_payload, "stage": "S2", "platform_status": "PASS"}
    path = write(tmp_path / "synthetic-s2-review.json", receipt)
    approved = run_cli("check-review", "--stage", "S2", "--receipt", path, "--commit", "a" * 40)
    assert approved.returncode == 0
    result = cli_json(approved)
    assert result["receipt_structurally_passed"] is True
    assert result["reviewer_authenticated"] is result["next_stage_authorized"] is result["merge_performed"] is False
    wrong_commit = run_cli("check-review", "--stage", "S2", "--receipt", path, "--commit", "b" * 40)
    assert wrong_commit.returncode == 3
    receipt.update(decision="PENDING", engineering_status="INCOMPLETE", platform_status="NOT_RUN",
                   evidence=[], reviewed_at=None)
    write(path, receipt)
    pending = run_cli("check-review", "--stage", "S2", "--receipt", path, "--commit", "a" * 40)
    assert pending.returncode == 3
    assert "independent_review_not_approved" in cli_json(pending)["reasons"]


def verification_payload() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {"implementation_commit": "a" * 40, "code_commit_end": "a" * 40,
        "working_tree_dirty_start": False, "working_tree_dirty_end": False,
        "generated_at_utc": now, "python": "SYNTHETIC_TEST Python", "platform": "SYNTHETIC_TEST host",
        "dependency_lock_sha256": "c" * 64, "developer_checks_passed": True,
        "checks": [{"name": name, "command": ["SYNTHETIC_TEST", name], "started_at_utc": now,
                    "duration_seconds": 0.0, "exit_code": 3 if name == "pending_review_gate" else 0,
                    "expected_exit_code": 3 if name == "pending_review_gate" else 0, "passed": True}
                   for name in S2_REQUIRED_CHECKS]}


def test_s2_verification_requires_complete_checks_and_truthful_clean_source():
    payload = verification_payload()
    assert S2Verification.model_validate_json(json.dumps(payload)).developer_checks_passed is True
    for change in ("duplicate_check", "dirty", "different_head", "false_exit"):
        broken = copy.deepcopy(payload)
        if change == "duplicate_check":
            broken["checks"][-1] = copy.deepcopy(broken["checks"][0])
        elif change == "dirty":
            broken["working_tree_dirty_end"] = True
        elif change == "different_head":
            broken["code_commit_end"] = "b" * 40
        else:
            broken["checks"][0]["exit_code"] = 1
        with pytest.raises(ValidationError):
            S2Verification.model_validate_json(json.dumps(broken))
    payload.update(working_tree_dirty_end=True, developer_checks_passed=False)
    assert S2Verification.model_validate_json(json.dumps(payload)).developer_checks_passed is False


@pytest.mark.parametrize("value", [0, True, "false"])
def test_s2_verification_next_stage_false_is_a_strict_boolean(value):
    payload = verification_payload()
    payload["next_stage_authorized"] = value
    with pytest.raises(ValidationError):
        S2Verification.model_validate_json(json.dumps(payload))


def test_synthetic_cli_run_binds_real_source_and_literal_reference(synthetic_run: Path, reference_checker, project_root: Path):
    report = reference_checker.check_results(synthetic_run)
    assert report == {"reference_passed": True, "provider": "SYNTHETIC", "universe_rows": 4,
                      "quality_rows": 8, "local_task_attempts": 1,
                      "platform_execution": "NOT_RUN", "independent_review": "PENDING"}
    result, record = read(synthetic_run / "result.json"), read(synthetic_run / "run.json")
    assert record["code_commit"] == git_provenance(project_root)["commit"]
    assert type(record["working_tree_dirty"]) is bool
    assert record["independent_review"] == "PENDING" and result["economic_evidence"] == "NOT_APPLICABLE"
    assert result["task"]["spec"]["source_commit"] == record["code_commit"]
    assert result["task"]["spec"]["response_class"] == "SYNTHETIC_TEST"
    assert result["usage"]["attempt_count"] == 1 and result["usage"]["confirmed_execution_count"] == 0
    assert result["task"]["attempts"][0]["platform_task_id"] is None
    assert (synthetic_run / "task_ledger.sqlite").is_file()
    assert len(list((synthetic_run / "metadata").iterdir())) == 1


def snapshot_files(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_repeat_synthetic_run_cannot_overwrite_any_evidence(synthetic_run: Path, synthetic_config: Path, run_cli):
    before = snapshot_files(synthetic_run)
    result = run_cli("s2-run-synthetic", "--config", synthetic_config,
                     "--output-dir", synthetic_run.parent, "--run-id", synthetic_run.name)
    assert result.returncode == 2
    assert snapshot_files(synthetic_run) == before


def test_synthetic_command_rejects_cloud_config_before_any_output(tmp_path: Path, cloud_config: Path, run_cli, cli_json):
    output = tmp_path / "must-not-exist"
    result = run_cli("s2-run-synthetic", "--config", cloud_config,
                     "--output-dir", output, "--run-id", "cloud-denied")
    assert result.returncode == 2
    assert cli_json(result)["valid"] is False
    assert not output.exists()


def test_synthetic_command_requires_real_git_provenance_before_output(tmp_path: Path, synthetic_config: Path, monkeypatch):
    monkeypatch.setattr(s2_cli, "git_provenance", lambda: {"commit": None, "dirty": True})
    target = tmp_path / "no-source"
    with pytest.raises(ValueError, match="real Git checkout"):
        s2_cli.synthetic_command(synthetic_config, target, "invalid")
    assert not target.exists()


def test_reference_checker_accepts_equivalent_utc_notation_without_rewriting_raw(synthetic_run: Path, reference_checker, project_root: Path):
    raw = project_root / "tests/fixtures/s2_raw.json"
    before_raw = raw.read_bytes()
    config = read(synthetic_run / "config.json")
    assert config["bundle"]["bars"][0]["available_at"].endswith("Z")
    assert read(raw)["bars"][0]["available_at"].endswith("+00:00")
    for bar in config["bundle"]["bars"]:
        bar["available_at"] = bar["available_at"].replace("Z", "+00:00")
    write(synthetic_run / "config.json", config)
    assert reference_checker.check_results(synthetic_run)["reference_passed"] is True
    assert raw.read_bytes() == before_raw


def test_reference_checker_rejects_price_tamper_after_hashes_are_consistent(synthetic_run: Path, reference_checker, project_root: Path, tmp_path: Path):
    isolated_root = tmp_path / "isolated-reference"
    for relative in ("configs/s2.example.json", "tests/fixtures/s2_raw.json", "tests/fixtures/s2_expected.json"):
        destination = isolated_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project_root / relative, destination)
    config = read(synthetic_run / "config.json")
    config["bundle"]["bars"][0]["close"] = 20.5  # Valid OHLC range; wrong original observation.
    write(synthetic_run / "config.json", config)
    write(isolated_root / "configs/s2.example.json", config)
    checked = load_s2_config(synthetic_run / "config.json")
    config_hash = hashlib.sha256(canonical_s2_config(checked)).hexdigest()
    result = read(synthetic_run / "result.json")
    result["config_sha256"] = config_hash
    write(synthetic_run / "result.json", result)
    record = read(synthetic_run / "run.json")
    record["config_sha256"] = config_hash
    record["result_sha256"] = hashlib.sha256((synthetic_run / "result.json").read_bytes()).hexdigest()
    write(synthetic_run / "run.json", record)
    with pytest.raises(ValueError, match="raw fields were not preserved"):
        reference_checker.check_results(synthetic_run, isolated_root)


def test_reference_checker_rejects_wrong_hand_answer_after_result_rehash(synthetic_run: Path, reference_checker):
    result = read(synthetic_run / "result.json")
    result["universe"]["rows"][2]["assets"] = ["SYN_A", "SYN_B"]
    write(synthetic_run / "result.json", result)
    record = read(synthetic_run / "run.json")
    record["result_sha256"] = hashlib.sha256((synthetic_run / "result.json").read_bytes()).hexdigest()
    write(synthetic_run / "run.json", record)
    with pytest.raises(ValueError, match="hand-calculated universe"):
        reference_checker.check_results(synthetic_run)


def test_reference_checker_leaves_sqlite_and_metadata_bytes_unchanged(synthetic_run: Path, reference_checker):
    before = snapshot_files(synthetic_run)
    assert reference_checker.check_results(synthetic_run)["reference_passed"] is True
    assert snapshot_files(synthetic_run) == before


def test_s2_schemas_have_their_own_complete_inventory(tmp_path: Path, run_cli, cli_json):
    output = tmp_path / "s2-schemas"
    generated = run_cli("schemas", "--stage", "S2", "--output-dir", output)
    assert generated.returncode == 0
    assert cli_json(generated)["schemas"] >= 8
    assert (output / "S2Config.schema.json").is_file()
    assert (output / "TaskRecord.schema.json").is_file()
    assert not (output / "S1ReviewReceipt.schema.json").exists()
    assert run_cli("schemas", "--stage", "S2", "--output-dir", output, "--check").returncode == 0
    assert run_cli("schemas", "--stage", "S1", "--output-dir", output, "--check").returncode == 2
