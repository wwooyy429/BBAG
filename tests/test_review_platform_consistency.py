"""S0 receipt semantics; synthetic approvals never authenticate a reviewer."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from bbag.contracts import ReviewReceipt


@pytest.mark.parametrize("platform", ["PASS", "FAIL", "NOT_RUN"])
def test_s0_approval_requires_not_applicable_platform(
    platform: str, approved_receipt_payload: dict[str, Any],
    write_json: Any, run_cli: Any, cli_json: Any,
) -> None:
    payload = dict(approved_receipt_payload, platform_status=platform)
    with pytest.raises(ValidationError, match="platform NOT_APPLICABLE"):
        ReviewReceipt.model_validate_json(json.dumps(payload))
    result = run_cli("check-review", "--receipt", write_json(payload), "--commit", "a" * 40)
    assert result.returncode == 2
    assert result.stdout == ""
    output = cli_json(result)
    assert output["valid"] is False
    assert output.get("receipt_structurally_passed") is not True
    assert "platform NOT_APPLICABLE" in output["errors"][0]["message"]


@pytest.mark.parametrize("platform", ["PASS", "FAIL", "NOT_RUN"])
@pytest.mark.parametrize("decision", ["PENDING", "CHANGES_REQUIRED"])
def test_legacy_nonapproved_platform_states_remain_readable_and_blocked(
    platform: str, decision: str, approved_receipt_payload: dict[str, Any],
    write_json: Any, run_cli: Any, cli_json: Any,
) -> None:
    payload = dict(
        approved_receipt_payload, decision=decision, platform_status=platform,
        engineering_status="INCOMPLETE" if decision == "PENDING" else "FAIL",
        blocking_findings=0 if decision == "PENDING" else 1,
        evidence=[], reviewed_at=None,
    )
    receipt = ReviewReceipt.model_validate_json(json.dumps(payload))
    assert receipt.platform_status == platform
    result = run_cli("check-review", "--receipt", write_json(payload), "--commit", "a" * 40)
    assert result.returncode == 3
    output = cli_json(result)
    assert output["receipt_structurally_passed"] is False
    assert "independent_review_not_approved" in output["reasons"]
    assert output["advisory_only"] is True
    assert output["reviewer_authenticated"] is False
    assert output["merge_performed"] is False
    assert output["next_stage_authorized"] is False


def test_valid_s0_approval_remains_advisory_only(
    approved_receipt_payload: dict[str, Any], write_json: Any, run_cli: Any, cli_json: Any,
) -> None:
    assert approved_receipt_payload["platform_status"] == "NOT_APPLICABLE"
    assert ReviewReceipt.model_validate_json(json.dumps(approved_receipt_payload)).decision == "APPROVED"
    result = run_cli("check-review", "--receipt", write_json(approved_receipt_payload), "--commit", "a" * 40)
    assert result.returncode == 0
    output = cli_json(result)
    assert output["receipt_structurally_passed"] is True
    assert output["advisory_only"] is True
    assert output["reviewer_authenticated"] is False
    assert output["reviewer_identity_requires_external_confirmation"] is True
    assert output["merge_performed"] is False
    assert output["next_stage_authorized"] is False
