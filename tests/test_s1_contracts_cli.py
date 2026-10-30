"""S1 contract/CLI integration; fixtures confer no research or review approval."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from bbag.s1_contracts import NumericPolicy, S1Config, canonical_s1_bytes, load_s1_config


@pytest.fixture
def s1_path(project_root: Path) -> Path:
    return project_root / "configs/s1.example.json"


@pytest.fixture
def s1_payload(s1_path: Path) -> dict:
    return json.loads(s1_path.read_bytes())


def write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_s1_example_retains_engineering_draft_and_stable_hash(s1_path: Path) -> None:
    config = load_s1_config(s1_path)
    assert config.stage == "S1" and config.status == "DRAFT"
    assert config.purpose == "ENGINEERING_TEST" and config.pending_decisions
    assert canonical_s1_bytes(config) == canonical_s1_bytes(S1Config.model_validate_json(config.model_dump_json()))


@pytest.mark.parametrize("fault", [
    "extra", "boolean_window", "naive_asof", "universe", "benchmark_in_universe",
    "duplicate_observation", "observation_extra_field", "observation_missing_field",
    "observation_early", "wrong_session_date", "signal_after_next_open", "unordered_sessions",
    "duplicate_asset", "extra_nested", "empty_pending",
])
def test_s1_configuration_rejects_inconsistent_inputs(s1_payload: dict, fault: str) -> None:
    p = copy.deepcopy(s1_payload)
    if fault == "extra": p["unexpected"] = True
    elif fault == "boolean_window": p["factor"]["lookback_sessions"] = True
    elif fault == "naive_asof": p["as_of"] = "2024-01-11T21:05:00"
    elif fault == "universe": p["universe"] = ["MISSING"]
    elif fault == "benchmark_in_universe": p["universe"].append("BM")
    elif fault == "duplicate_observation": p["panel"]["observations"].append(copy.deepcopy(p["panel"]["observations"][0]))
    elif fault == "observation_extra_field": p["panel"]["observations"][0]["values"]["extra"] = 1
    elif fault == "observation_missing_field": del p["panel"]["observations"][0]["values"]["open"]
    elif fault == "observation_early": p["panel"]["observations"][0]["available_at"] = "2024-01-02T20:59:59Z"
    elif fault == "wrong_session_date": p["panel"]["sessions"][0]["open_at"] = "2024-01-01T14:30:00Z"
    elif fault == "signal_after_next_open": p["panel"]["sessions"][0]["signal_at"] = "2024-01-03T14:30:00Z"
    elif fault == "unordered_sessions": p["panel"]["sessions"][0:2] = p["panel"]["sessions"][1::-1]
    elif fault == "duplicate_asset": p["panel"]["assets"].append("A")
    elif fault == "extra_nested": p["audit_limits"]["future_access"] = True
    elif fault == "empty_pending": p["pending_decisions"] = []
    with pytest.raises(ValidationError):
        S1Config.model_validate_json(json.dumps(p))


@pytest.mark.parametrize("value", [False, True, 0.0, "0", 1])
def test_ddof_is_strict_integer_zero(value: object) -> None:
    with pytest.raises(ValidationError):
        NumericPolicy.model_validate({"std_ddof": value})


def test_s1_loader_rejects_ambiguous_json_before_model(tmp_path: Path, s1_path: Path) -> None:
    body = s1_path.read_text(encoding="utf-8")
    path = tmp_path / "ambiguous.json"
    path.write_text('{"stage":"S0",' + body[1:], encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_s1_config(path)


def test_data_field_dictionary_agrees_with_expression_identifiers(s1_payload: dict) -> None:
    s1_payload["panel"]["fields"].append("class")
    for observation in s1_payload["panel"]["observations"]:
        observation["values"]["class"] = 1.0
    with pytest.raises(ValidationError, match="language keywords"):
        S1Config.model_validate_json(json.dumps(s1_payload))


def test_cli_audit_and_separate_schemas(s1_path: Path, project_root: Path, run_cli, cli_json) -> None:
    audit = run_cli("s1-audit", "--config", s1_path)
    assert audit.returncode == 0
    result = cli_json(audit)
    assert result["audit"] == {"normalized_expression": "cs_rank(neg(return(close,2)))",
        "fields": ["close"], "lookback_sessions": 3, "node_count": 5, "max_depth": 4, "parameter_count": 1}
    assert result["research_ready"] is False
    for stage, path in [("S0", project_root / "contracts"), ("S1", project_root / "contracts/s1")]:
        assert run_cli("schemas", "--stage", stage, "--output-dir", path, "--check").returncode == 0


def test_saved_cli_runs_match_independent_hand_rational_answers(
    tmp_path: Path, s1_path: Path, project_root: Path, run_cli,
) -> None:
    for mode in ("batch", "incremental"):
        result = run_cli("s1-factors", "--config", s1_path, "--output-dir", tmp_path, "--run-id", mode, "--mode", mode)
        assert result.returncode == 0, result.stderr
    result = run_cli("s1-labels", "--config", s1_path, "--output-dir", tmp_path, "--run-id", "labels")
    assert result.returncode == 0, result.stderr
    spec = importlib.util.spec_from_file_location("s1_reference_checker", project_root / "scripts/check_stage1_results.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = project_root / "tests/fixtures/s1_expected.json"
    assert module.check_results(expected, tmp_path / "batch", tmp_path / "incremental", tmp_path / "labels")["reference_passed"]
    old_bytes = {p.name: p.read_bytes() for p in (tmp_path / "batch").iterdir()}
    repeat = run_cli("s1-factors", "--config", s1_path, "--output-dir", tmp_path, "--run-id", "batch")
    assert repeat.returncode == 2
    assert {p.name: p.read_bytes() for p in (tmp_path / "batch").iterdir()} == old_bytes
    bad = json.loads((tmp_path / "batch/result.json").read_bytes())
    bad["rows"][6]["value"] = 0.75
    write(tmp_path / "batch/result.json", bad)
    with pytest.raises(ValueError):
        module.check_results(expected, tmp_path / "batch", tmp_path / "incremental", tmp_path / "labels")


@pytest.mark.parametrize("formula", ["delay(close,-1)", "close[1]", "__import__('os')", "lead(close,1)"])
def test_invalid_expression_has_structured_cli_error(tmp_path: Path, s1_payload: dict, run_cli, cli_json, formula: str) -> None:
    s1_payload["factor"]["formula"] = formula
    path = write(tmp_path / "bad.json", s1_payload)
    result = run_cli("s1-factors", "--config", path, "--output-dir", tmp_path / "no-output", "--run-id", "invalid")
    assert result.returncode == 2
    assert cli_json(result)["valid"] is False
    assert not (tmp_path / "no-output").exists()


def test_s0_review_cannot_be_reused_as_s1(tmp_path: Path, approved_receipt_payload: dict, run_cli, cli_json) -> None:
    path = write(tmp_path / "receipt.json", approved_receipt_payload)
    rejected = run_cli("check-review", "--stage", "S1", "--receipt", path, "--commit", "a" * 40)
    assert rejected.returncode == 2
    receipt = {**approved_receipt_payload, "stage": "S1"}
    write(path, receipt)
    approved = run_cli("check-review", "--stage", "S1", "--receipt", path, "--commit", "a" * 40)
    assert approved.returncode == 0
    assert cli_json(approved)["reviewer_authenticated"] is False
    assert cli_json(approved)["next_stage_authorized"] is False
    receipt.update(decision="PENDING", engineering_status="INCOMPLETE", reviewed_at=None, evidence=[])
    write(path, receipt)
    pending = run_cli("check-review", "--stage", "S1", "--receipt", path, "--commit", "a" * 40)
    assert pending.returncode == 3
