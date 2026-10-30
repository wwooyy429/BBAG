"""Adversarial contract tests for the offline S0 boundary."""

from __future__ import annotations

import copy
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from bbag.contracts import (
    CONTRACT_MODELS,
    DataSplitSpec,
    ExperimentRecord,
    ReleaseManifest,
    ResearchProtocol,
    canonical_protocol_bytes,
    load_protocol,
)


def parse_protocol(payload: dict[str, Any]) -> ResearchProtocol:
    return ResearchProtocol.model_validate_json(json.dumps(payload, allow_nan=True))


def replace(payload: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    changed = copy.deepcopy(payload)
    container = changed
    for key in path[:-1]:
        container = container[key]
    container[path[-1]] = value
    return changed


def split() -> DataSplitSpec:
    return DataSplitSpec.model_validate_json(
        json.dumps(
            {
                "development": {"start": "2020-01-01", "end": "2020-12-31"},
                "final_test": {"start": "2021-01-01", "end": "2021-12-31"},
            }
        )
    )


def test_example_is_pending_offline_engineering_only(example_path: Path) -> None:
    protocol = load_protocol(example_path)
    assert protocol.stage == "S0"
    assert protocol.purpose == "ENGINEERING_TEST"
    assert protocol.status == "DRAFT"
    assert protocol.pending_decisions
    for field in ("max_model_calls", "max_platform_jobs", "max_optimization_jobs", "max_spend_usd"):
        assert getattr(protocol.budget, field) == 0
    assert set(protocol.factor.required_fields) <= set(protocol.data.fields)


@pytest.mark.parametrize(
    "field",
    [
        "protocol_id", "title", "data", "factor", "label", "execution",
        "evaluation", "budget", "data_split", "pending_decisions",
    ],
)
def test_required_protocol_fields_cannot_be_missing(protocol_payload: dict[str, Any], field: str) -> None:
    del protocol_payload[field]
    with pytest.raises(ValidationError):
        parse_protocol(protocol_payload)


@pytest.mark.parametrize(
    "section",
    [None, "data", "factor", "label", "execution", "evaluation", "budget", "data_split"],
)
def test_unknown_fields_are_rejected(protocol_payload: dict[str, Any], section: str | None) -> None:
    container = protocol_payload if section is None else protocol_payload[section]
    container["undeclared_option"] = True
    with pytest.raises(ValidationError):
        parse_protocol(protocol_payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("stage",), "S1"),
        (("purpose",), "ECONOMIC_VALIDATION"),
        (("status",), "APPROVED"),
        (("factor", "lookback_sessions"), "5"),
        (("factor", "lookback_sessions"), True),
        (("factor", "lookback_sessions"), 0),
        (("factor", "expected_direction"), "buy"),
        (("label", "horizon_sessions"), False),
        (("label", "horizon_sessions"), 0),
        (("execution", "rebalance_sessions"), True),
        (("execution", "rebalance_sessions"), 0),
        (("execution", "allow_short"), 1),
        (("execution", "entry_rule"), "same_session_close"),
        (("execution", "signal_rule"), "before_data_available"),
        (("label", "entry_rule"), "same_session_close"),
        (("data", "fields"), "close"),
        (("data", "source"), "live_brokerage"),
        (("evaluation", "final_results_policy"), "send_to_generation_memory"),
        (("evaluation", "train_sessions"), -1),
        (("budget", "max_candidates"), False),
        (("budget", "max_runtime_seconds"), True),
    ],
)
def test_illegal_types_ranges_and_s0_rules(
    protocol_payload: dict[str, Any], path: tuple[str, ...], value: Any
) -> None:
    with pytest.raises(ValidationError):
        parse_protocol(replace(protocol_payload, path, value))


@pytest.mark.parametrize(
    "field",
    ["max_candidates", "max_model_calls", "max_platform_jobs", "max_optimization_jobs",
     "max_runtime_seconds", "max_spend_usd"],
)
def test_negative_budgets_are_rejected(protocol_payload: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError):
        parse_protocol(replace(protocol_payload, ("budget", field), -1))


@pytest.mark.parametrize(
    "field",
    ["max_model_calls", "max_platform_jobs", "max_optimization_jobs", "max_spend_usd"],
)
@pytest.mark.parametrize("value", [True, False, 0.0, "0", 1])
def test_zero_external_budget_is_strict_literal_zero(
    protocol_payload: dict[str, Any], field: str, value: Any
) -> None:
    with pytest.raises(ValidationError):
        parse_protocol(replace(protocol_payload, ("budget", field), value))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("execution", "max_position_weight"), 1.01),
        (("execution", "max_position_weight"), -0.01),
        (("execution", "gross_exposure_limit"), -1.0),
        (("execution", "commission_bps"), -0.1),
        (("execution", "slippage_bps"), -0.1),
        (("evaluation", "thresholds"), {"rank_ic": 1.01}),
        (("evaluation", "thresholds"), {"coverage": 1.01}),
        (("evaluation", "thresholds"), {"coverage": -0.01}),
    ],
)
def test_financial_and_metric_ranges(
    protocol_payload: dict[str, Any], path: tuple[str, ...], value: Any
) -> None:
    with pytest.raises(ValidationError):
        parse_protocol(replace(protocol_payload, path, value))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    "path",
    [
        ("execution", "max_position_weight"),
        ("execution", "commission_bps"),
        ("evaluation", "thresholds", "rank_ic"),
    ],
)
def test_nonfinite_numeric_values_are_rejected(
    protocol_payload: dict[str, Any], path: tuple[str, ...], value: float
) -> None:
    changed = copy.deepcopy(protocol_payload)
    if path[:2] == ("evaluation", "thresholds"):
        changed["evaluation"]["thresholds"] = {"rank_ic": 0.0}
    with pytest.raises(ValidationError):
        parse_protocol(replace(changed, path, value))


def test_factor_cannot_require_an_undeclared_data_field(protocol_payload: dict[str, Any]) -> None:
    protocol_payload["factor"]["required_fields"] = ["future_return_not_in_data"]
    with pytest.raises(ValidationError):
        parse_protocol(protocol_payload)


def test_prediction_and_rebalance_horizons_are_distinct(protocol_payload: dict[str, Any]) -> None:
    protocol_payload["label"]["horizon_sessions"] = 5
    protocol_payload["execution"]["rebalance_sessions"] = 1
    protocol = parse_protocol(protocol_payload)
    assert protocol.label.horizon_sessions == 5
    assert protocol.execution.rebalance_sessions == 1


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("development", "start"), "2020-02-30"),
        (("development", "start"), "2020/01/01"),
        (("development", "start"), "2020-01-01T00:00:00Z"),
        (("development", "start"), True),
        (("development", "end"), "2019-12-31"),
        (("final_test", "start"), "2020-12-31"),
        (("final_test", "start"), "2020-06-01"),
        (("final_test", "end"), "2020-01-01"),
    ],
)
def test_invalid_dates_and_overlapping_splits(path: tuple[str, ...], value: Any) -> None:
    payload = split().model_dump(mode="json")
    payload[path[0]][path[1]] = value
    with pytest.raises(ValidationError):
        DataSplitSpec.model_validate_json(json.dumps(payload))


def test_development_range_allows_inclusive_boundaries() -> None:
    dates = split()
    assert dates.check_development_range("2020-01-01", "2020-12-31") is None
    assert dates.check_development_range(date(2020, 6, 1), date(2020, 6, 1)) is None


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2019-12-31", "2020-01-01"),
        ("2020-12-31", "2021-01-01"),
        ("2021-01-01", "2021-01-02"),
        ("2020-02-02", "2020-02-01"),
        ("2020-02-30", "2020-03-01"),
        (True, "2020-03-01"),
        (datetime(2020, 1, 1, tzinfo=timezone.utc), date(2020, 1, 2)),
    ],
)
def test_development_range_rejects_invalid_or_sealed_requests(start: Any, end: Any) -> None:
    with pytest.raises(ValueError):
        split().check_development_range(start, end)


def test_canonical_protocol_is_order_independent_utf8(protocol_payload: dict[str, Any]) -> None:
    protocol_payload["title"] = "独立契约测试"
    original = parse_protocol(protocol_payload)
    reordered = parse_protocol(dict(reversed(list(protocol_payload.items()))))
    content = canonical_protocol_bytes(original)
    assert content == canonical_protocol_bytes(reordered)
    assert "独立契约测试".encode("utf-8") in content
    assert not content.endswith(b"\n")
    assert json.loads(content) == original.model_dump(mode="json")
    assert content == json.dumps(
        json.loads(content), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def test_canonical_protocol_revalidates_mutated_models(protocol_payload: dict[str, Any]) -> None:
    protocol = parse_protocol(protocol_payload)
    unsafe = protocol.model_copy(update={"stage": "S7"})
    with pytest.raises((ValidationError, ValueError)):
        canonical_protocol_bytes(unsafe)


@pytest.mark.parametrize("kind", ["undeclared_field", "nonfinite_threshold"])
def test_canonical_protocol_revalidates_nested_mutation(
    protocol_payload: dict[str, Any], kind: str
) -> None:
    protocol = parse_protocol(protocol_payload)
    if kind == "undeclared_field":
        protocol.factor.required_fields.append("undeclared_after_validation")
    else:
        protocol.evaluation.thresholds["rank_ic"] = float("nan")
    with pytest.raises((ValidationError, ValueError)):
        canonical_protocol_bytes(protocol)


def test_load_protocol_rejects_duplicate_json_keys(
    tmp_path: Path, protocol_payload: dict[str, Any]
) -> None:
    text = json.dumps(protocol_payload)
    ambiguous = '{"stage": "S1",' + text[1:]
    path = tmp_path / "ambiguous.json"
    path.write_text(ambiguous, encoding="utf-8")
    with pytest.raises(ValueError):
        load_protocol(path)


def test_load_protocol_rejects_malformed_or_unknown_input(tmp_path: Path, protocol_payload: dict[str, Any]) -> None:
    invalid_json = tmp_path / "broken.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        load_protocol(invalid_json)
    protocol_payload["unregistered"] = "option"
    invalid_json.write_text(json.dumps(protocol_payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_protocol(invalid_json)


def test_contract_registry_contains_core_public_contracts() -> None:
    assert {
        "DataSpec", "FactorSpec", "LabelSpec", "ExecutionSpec",
        "EvaluationSpec", "ExperimentRecord", "ReleaseManifest", "ResearchProtocol",
    } <= set(CONTRACT_MODELS)


def experiment_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0", "run_id": "development-test",
        "stage": "S0", "purpose": "ENGINEERING_TEST",
        "protocol_sha256": "a" * 64, "code_commit": "b" * 40,
        "working_tree_dirty": True, "created_at": "2026-10-31T08:00:00+08:00",
        "validation_status": "VALIDATED", "platform_status": "NOT_RUN",
        "economic_status": "NOT_EVALUATED", "review_status": "PENDING",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code_commit", "short-sha"),
        ("protocol_sha256", "not-a-digest"),
        ("working_tree_dirty", "false"),
        ("created_at", "2026-10-31T08:00:00"),
        ("review_status", "APPROVED"),
        ("platform_status", "PASS"),
        ("economic_status", "SUPPORT"),
        ("stage", "S1"),
    ],
)
def test_experiment_record_cannot_invent_provenance_or_acceptance(field: str, value: Any) -> None:
    payload = experiment_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        ExperimentRecord.model_validate_json(json.dumps(payload))


def test_experiment_record_accepts_explicit_unknown_commit() -> None:
    payload = experiment_payload()
    payload["code_commit"] = None
    payload["working_tree_dirty"] = True
    record = ExperimentRecord.model_validate_json(json.dumps(payload))
    assert record.code_commit is None
    assert record.review_status == "PENDING"


def test_experiment_record_cannot_label_unknown_source_as_clean() -> None:
    payload = experiment_payload()
    payload["code_commit"] = None
    payload["working_tree_dirty"] = False
    with pytest.raises(ValidationError, match="unknown source commit"):
        ExperimentRecord.model_validate_json(json.dumps(payload))


def release_payload() -> dict[str, Any]:
    return {
        "release_id": "development-release",
        "code_commit": "a" * 40,
        "config_sha256": "b" * 64,
        "artifacts": [{"path": "src/bbag/contracts.py", "sha256": "c" * 64}],
        "review_status": "PENDING",
    }


@pytest.mark.parametrize("path", ["../secret.txt", "/absolute.txt", "C:/secret.txt", "src/../secret.txt", ""])
def test_release_artifact_paths_cannot_escape(path: str) -> None:
    payload = release_payload()
    payload["artifacts"][0]["path"] = path
    with pytest.raises(ValidationError):
        ReleaseManifest.model_validate_json(json.dumps(payload))


def test_release_manifest_requires_unique_artifacts_and_full_commit() -> None:
    payload = release_payload()
    ReleaseManifest.model_validate_json(json.dumps(payload))
    payload["artifacts"].append(copy.deepcopy(payload["artifacts"][0]))
    with pytest.raises(ValidationError):
        ReleaseManifest.model_validate_json(json.dumps(payload))
    payload = release_payload()
    payload["code_commit"] = "abc123"
    with pytest.raises(ValidationError):
        ReleaseManifest.model_validate_json(json.dumps(payload))
