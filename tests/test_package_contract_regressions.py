"""S0 audit regressions; synthetic evidence never represents real acceptance."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from test_provenance_boundaries import (
    copy_script_source,
    fixture_git,
    guarded_script,
    make_repository,
    make_synthetic_package_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("regression_packager", ROOT / "scripts/build_review_package.py")
assert SPEC is not None and SPEC.loader is not None
PACKAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGER)
FIXTURE_COMMIT = "b" * 40


def mutate(evidence: Path, fault: str) -> None:
    directory = evidence / "experiments/s0-demo"
    protocol_path = directory / "protocol.json"
    experiment_path = directory / "experiment.json"
    receipt_path = evidence / "review_request.json"
    protocol = json.loads(protocol_path.read_bytes())
    experiment = json.loads(experiment_path.read_bytes())
    receipt = json.loads(receipt_path.read_bytes())
    if fault == "missing_timestamp":
        del experiment["created_at"]
    elif fault == "naive_timestamp":
        experiment["created_at"] = "2026-10-31T03:00:00"
    elif fault == "invalid_protocol_type":
        protocol["factor"]["lookback_sessions"] = "bad"
    elif fault.startswith("budget:"):
        protocol["budget"][fault.split(":")[1]] = 1
    elif fault == "overlapping_dates":
        protocol["data_split"]["final_test"]["start"] = protocol["data_split"]["development"]["end"]
    elif fault == "unavailable_field":
        protocol["factor"]["required_fields"] = ["unknown_field"]
    elif fault == "extra_protocol_key":
        protocol["unexpected"] = True
    elif fault == "extra_experiment_key":
        experiment["unexpected"] = True
    elif fault == "experiment_boolean_sha":
        experiment["code_commit"] = False
    elif fault == "missing_reviewer":
        del receipt["reviewer"]
    elif fault == "receipt_boolean_blockers":
        receipt["blocking_findings"] = False
    elif fault == "receipt_extra_key":
        receipt["unexpected"] = True
    elif fault == "pending_with_evidence":
        receipt["evidence"] = ["synthetic-not-a-review"]
    elif fault == "pending_with_blocker":
        receipt["blocking_findings"] = 1
    elif fault != "noncanonical_protocol":
        raise AssertionError(f"Unknown test mutation: {fault}")
    protocol_raw = json.dumps(
        protocol, ensure_ascii=False, sort_keys=True,
        separators=None if fault == "noncanonical_protocol" else (",", ":"),
    ).encode("utf-8")
    protocol_path.write_bytes(protocol_raw)
    # Matching a new raw hash must never legitimize semantically invalid data.
    experiment["protocol_sha256"] = hashlib.sha256(protocol_raw).hexdigest()
    experiment_path.write_text(json.dumps(experiment), encoding="utf-8")
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")


@pytest.mark.parametrize("fault", [
    "missing_timestamp", "naive_timestamp", "invalid_protocol_type",
    "budget:max_model_calls", "budget:max_platform_jobs", "budget:max_optimization_jobs",
    "budget:max_spend_usd", "overlapping_dates", "unavailable_field", "extra_protocol_key",
    "extra_experiment_key", "experiment_boolean_sha", "missing_reviewer",
    "receipt_boolean_blockers", "receipt_extra_key", "pending_with_evidence",
    "pending_with_blocker", "noncanonical_protocol",
])
def test_packager_rejects_invalid_complete_contracts(tmp_path: Path, example_path: Path, fault: str) -> None:
    evidence = tmp_path / "SYNTHETIC_EVIDENCE_ONLY"
    make_synthetic_package_evidence(evidence, FIXTURE_COMMIT, example_path)
    mutate(evidence, fault)
    with pytest.raises(ValueError):
        PACKAGER.validate_evidence(evidence, FIXTURE_COMMIT)


@pytest.mark.parametrize("relative,key,value", [
    ("verification.json", "independent_review", "APPROVED"),
    ("review_request.json", "decision", "APPROVED"),
    ("experiments/s0-demo/protocol.json", "stage", "S1"),
    ("experiments/s0-demo/experiment.json", "code_commit", "a" * 40),
])
def test_packager_rejects_ambiguous_json_at_each_evidence_entry(
    tmp_path: Path, example_path: Path, relative: str, key: str, value: str,
) -> None:
    evidence = tmp_path / "SYNTHETIC_EVIDENCE_ONLY"
    make_synthetic_package_evidence(evidence, FIXTURE_COMMIT, example_path)
    path = evidence / relative
    path.write_text("{" + json.dumps(key) + ":" + json.dumps(value) + "," + path.read_text(encoding="utf-8")[1:], encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        PACKAGER.validate_evidence(evidence, FIXTURE_COMMIT)


def test_synthetic_valid_evidence_control(tmp_path: Path, example_path: Path) -> None:
    evidence = tmp_path / "SYNTHETIC_EVIDENCE_ONLY"
    make_synthetic_package_evidence(evidence, FIXTURE_COMMIT, example_path)
    _, receipt, counts = PACKAGER.validate_evidence(evidence, FIXTURE_COMMIT)
    assert counts == {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}
    assert receipt["decision"] == "PENDING"


@pytest.mark.parametrize("fault", ["missing_timestamp", "naive_timestamp", "budget:max_model_calls", "invalid_protocol_type"])
def test_contract_rejection_precedes_any_package_output(
    tmp_path: Path, project_root: Path, example_path: Path, fault: str,
) -> None:
    repository, commit = make_repository(tmp_path / "repository")
    copy_script_source(project_root, repository)
    (repository / ".git/info/exclude").write_text("src/\nscripts/\n", encoding="utf-8")
    assert fixture_git(repository, "status", "--porcelain") == ""
    evidence = tmp_path / "SYNTHETIC_EVIDENCE_ONLY"
    make_synthetic_package_evidence(evidence, commit, example_path)
    mutate(evidence, fault)
    output = tmp_path / "uncreated-output"
    result, _ = guarded_script(repository, "build_review_package.py", [
        "--evidence-dir", str(evidence), "--output-dir", str(output),
    ], tmp_path / "guard")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "Traceback" not in result.stderr
    assert not output.exists()
