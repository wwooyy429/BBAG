"""Synthetic receipt checks cannot authenticate a reviewer or authorize a merge."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest


def assert_advisory(output: dict[str, Any]) -> None:
    assert output["advisory_only"] is True
    assert output["reviewer_authenticated"] is False
    assert output["merge_performed"] is False
    assert output["next_stage_authorized"] is False


def test_missing_review_remains_pending(tmp_path: Path, run_cli: Any, cli_json: Any) -> None:
    result = run_cli("check-review", "--receipt", tmp_path / "missing.json", "--commit", "a" * 40)
    assert result.returncode == 3
    output = cli_json(result)
    assert output["receipt_structurally_passed"] is False
    assert_advisory(output)


def test_pending_receipt_does_not_pass(
    pending_receipt_payload: dict[str, Any], write_json: Any, run_cli: Any, cli_json: Any
) -> None:
    receipt = write_json(pending_receipt_payload)
    previous = receipt.read_bytes()
    result = run_cli("check-review", "--receipt", receipt, "--commit", "a" * 40)
    assert result.returncode == 3
    output = cli_json(result)
    assert output["receipt_structurally_passed"] is False
    assert_advisory(output)
    assert receipt.read_bytes() == previous


def test_changes_required_does_not_pass(
    approved_receipt_payload: dict[str, Any], write_json: Any, run_cli: Any, cli_json: Any
) -> None:
    payload = copy.deepcopy(approved_receipt_payload)
    payload.update(decision="CHANGES_REQUIRED", engineering_status="FAIL", blocking_findings=1)
    result = run_cli("check-review", "--receipt", write_json(payload), "--commit", "a" * 40)
    assert result.returncode == 3
    output = cli_json(result)
    assert output["receipt_structurally_passed"] is False
    assert_advisory(output)


def test_approval_for_different_commit_is_stale(
    approved_receipt_payload: dict[str, Any], write_json: Any, run_cli: Any, cli_json: Any
) -> None:
    result = run_cli("check-review", "--receipt", write_json(approved_receipt_payload),
                     "--commit", "b" * 40)
    assert result.returncode == 3
    output = cli_json(result)
    assert output["receipt_structurally_passed"] is False
    assert "review_commit_mismatch" in output["reasons"]
    assert_advisory(output)


def test_structural_approval_is_advisory_not_external_acceptance(
    approved_receipt_payload: dict[str, Any], write_json: Any, run_cli: Any, cli_json: Any
) -> None:
    receipt = write_json(approved_receipt_payload)
    previous = receipt.read_bytes()
    result = run_cli("check-review", "--receipt", receipt, "--commit", "a" * 40)
    assert result.returncode == 0, result.stderr
    output = cli_json(result)
    assert output["receipt_structurally_passed"] is True
    assert output["reviewer_identity_requires_external_confirmation"] is True
    assert_advisory(output)
    assert receipt.read_bytes() == previous


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("engineering_status", "INCOMPLETE"),
        ("blocking_findings", 1),
        ("blocking_findings", False),
        ("evidence", []),
        ("evidence", [""]),
        ("reviewed_at", None),
        ("reviewed_at", "2026-10-31T08:00:00"),
        ("audited_commit", "short"),
        ("stage", "S1"),
        ("reviewer", ""),
    ],
)
def test_malformed_approval_cannot_look_like_acceptance(
    approved_receipt_payload: dict[str, Any], write_json: Any,
    run_cli: Any, cli_json: Any, field: str, value: Any
) -> None:
    approved_receipt_payload[field] = value
    result = run_cli("check-review", "--receipt", write_json(approved_receipt_payload),
                     "--commit", "a" * 40)
    assert result.returncode == 2
    assert cli_json(result)["valid"] is False


def test_unknown_receipt_fields_are_rejected(
    approved_receipt_payload: dict[str, Any], write_json: Any, run_cli: Any, cli_json: Any
) -> None:
    approved_receipt_payload["automatically_merge"] = True
    result = run_cli("check-review", "--receipt", write_json(approved_receipt_payload),
                     "--commit", "a" * 40)
    assert result.returncode == 2
    assert cli_json(result)["valid"] is False


def test_abbreviated_expected_commit_is_not_accepted(
    approved_receipt_payload: dict[str, Any], write_json: Any, run_cli: Any, cli_json: Any
) -> None:
    result = run_cli("check-review", "--receipt", write_json(approved_receipt_payload), "--commit", "abc123")
    assert result.returncode == 2
    assert cli_json(result)["valid"] is False


def test_advisory_check_cannot_mark_new_experiment_approved(
    tmp_path: Path, approved_receipt_payload: dict[str, Any], example_path: Path,
    write_json: Any, run_cli: Any, cli_json: Any
) -> None:
    gate = run_cli("check-review", "--receipt", write_json(approved_receipt_payload),
                   "--commit", "a" * 40)
    assert gate.returncode == 0
    initialized = run_cli("init-experiment", "--config", example_path,
                          "--output-dir", tmp_path / "runs", "--run-id", "still-pending")
    assert initialized.returncode == 0, initialized.stderr
    record = cli_json(initialized)["record"]
    assert record["review_status"] == "PENDING"
    assert record["platform_status"] == "NOT_RUN"
    assert record["economic_status"] == "NOT_EVALUATED"
