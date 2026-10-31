"""Synthetic control-channel checks; no QuantConnect platform execution."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from bbag.s2_access import AccessGuard, AccessPolicy, DataRequest, canonical_policy_sha256
from bbag.s2_quantconnect import (
    QuantConnectCapabilities, QuantConnectCheckRequest, QuantConnectCheckResponse,
    QuantConnectDispatchError, QuantConnectGateway, canonical_qc_request, qc_request_sha256,
    normalize_qc_check_response,
)
from bbag.s2_tasks import TaskBudget, TaskLedger, TaskSpec


def request() -> QuantConnectCheckRequest:
    return QuantConnectCheckRequest(
        project_id=123, source_commit="a" * 40, config_sha256="b" * 64,
        data=DataRequest(provider="SYNTHETIC", start=date(2020, 1, 2), end=date(2020, 1, 4),
                         assets=["A"], fields=["open", "close", "volume"],
                         as_of=datetime(2020, 1, 10, 18, tzinfo=timezone.utc),
                         classification="DEVELOPMENT", output_mode="CONTROL_ONLY"),
        checks=["data_presence", "requested_bounds", "no_data_export"],
    )


def policy() -> AccessPolicy:
    return AccessPolicy(
        policy_id="synthetic-bridge-test", provider="SYNTHETIC", timezone="UTC",
        development={"start": date(2020, 1, 2), "end": date(2020, 1, 10)},
        sealed=[{"start": date(2020, 2, 1), "end": date(2020, 2, 10)}],
        allowed_assets=["A", "B"], allowed_fields=["open", "close", "volume"],
    )


def response_payload(plan: QuantConnectCheckRequest, **changes) -> dict:
    payload = dict(schema_version="1.0", operation="research_check", project_id=plan.project_id,
                   request_sha256=qc_request_sha256(plan), source_commit=plan.source_commit,
                   config_sha256=plan.config_sha256, status="COMPLETED", error_category="NONE",
                   check_results={name: True for name in plan.checks})
    payload.update(changes)
    return payload


def response_bytes(plan: QuantConnectCheckRequest, **changes) -> bytes:
    return json.dumps(response_payload(plan, **changes)).encode("utf-8")


class FixtureCallback:
    def __init__(self) -> None:
        self.requests: list[bytes] = []

    def __call__(self, raw: bytes) -> bytes:
        self.requests.append(raw)
        plan = QuantConnectCheckRequest.model_validate_json(raw)
        return response_bytes(plan)


def gateway(callback=None, access_policy: AccessPolicy | None = None) -> QuantConnectGateway:
    return QuantConnectGateway(QuantConnectCapabilities(project_ids=[123, 456]),
                              AccessGuard(access_policy or policy()), callback)


def test_fixture_callback_receives_canonical_control_plan_and_cannot_claim_platform_pass() -> None:
    callback = FixtureCallback()
    plan = request()
    result = gateway(callback).check(plan)
    assert callback.requests == [canonical_qc_request(plan)]
    assert result.response.check_results == {"data_presence": True, "requested_bounds": True, "no_data_export": True}
    assert result.transport_evidence == "CALLBACK_UNVERIFIED"
    assert result.platform_integration == "NOT_RUN"
    assert result.evidence_authentication == "NOT_AUTHENTICATED"
    assert result.market_data_export == "NOT_SUPPORTED"
    assert qc_request_sha256(plan) == hashlib.sha256(callback.requests[0]).hexdigest()


@pytest.mark.parametrize("change", [
    {"source_commit": "c" * 40}, {"config_sha256": "d" * 64}, {"project_id": 456},
    {"max_history_rows": 33}, {"checks": ["no_data_export"]},
])
def test_request_identity_binds_every_execution_parameter(change: dict) -> None:
    first = request()
    second = QuantConnectCheckRequest.model_validate({**first.model_dump(mode="python"), **change})
    assert canonical_qc_request(first) != canonical_qc_request(second)
    assert qc_request_sha256(first) != qc_request_sha256(second)


@pytest.mark.parametrize("change", [
    {"operation": "history_export"}, {"normalization": "adjusted"}, {"resolution": "minute"},
    {"data_retention": "local"}, {"output_policy": "raw_data"}, {"max_history_rows": 129},
    {"max_history_rows": True}, {"checks": ["data_presence"]},
    {"checks": ["no_data_export", "no_data_export"]},
])
def test_cloud_request_rejects_unimplemented_or_unbounded_modes(change: dict) -> None:
    with pytest.raises(ValidationError):
        QuantConnectCheckRequest.model_validate({**request().model_dump(mode="python"), **change})


@pytest.mark.parametrize("change", [
    {"output_mode": "LOCAL_DATA"}, {"output_mode": "IN_CLOUD_DATA"}, {"purpose": "DEVELOPMENT"},
    {"fields": ["open", "open"]}, {"fields": ["vwap"]},
    {"assets": ["A", "B", "C", "D", "E"]}, {"end": date(2020, 2, 3)},
])
def test_nested_data_request_cannot_bypass_control_only_shape(change: dict) -> None:
    payload = request().model_dump(mode="python")
    payload["data"].update(change)
    with pytest.raises(ValidationError):
        QuantConnectCheckRequest.model_validate(payload)


@pytest.mark.parametrize("change", [
    {"start": date(2020, 1, 1)}, {"end": date(2020, 1, 11)}, {"assets": ["UNREQUESTED_SID"]},
    {"classification": "UNKNOWN"}, {"classification": "SEALED_FINAL"},
    {"derived_from": ["SEALED_FINAL"]}, {"provider": "QUANTCONNECT_CLOUD"},
    {"as_of": datetime(2020, 1, 3, 23, tzinfo=timezone.utc)},
])
def test_access_rejection_precedes_callback_and_cache_lookup(change: dict) -> None:
    callback = FixtureCallback()
    bridge = gateway(callback)
    payload = request().model_dump(mode="python")
    payload["data"].update(change)
    plan = QuantConnectCheckRequest.model_validate(payload)
    class ForbiddenRead(dict):
        def get(self, *args):
            raise AssertionError("unauthorized cache lookup occurred before the guard")
    bridge._cache = ForbiddenRead()
    # A planted cache entry must not even be read for an unauthorized plan.
    bridge._cache[qc_request_sha256(plan)] = response_bytes(plan)
    with pytest.raises(ValueError):
        bridge.check(plan)
    with pytest.raises(ValueError):
        bridge.cached_result(plan)
    with pytest.raises(ValueError):
        bridge.dispatch(plan)
    assert callback.requests == []


def test_guard_is_rechecked_before_cached_result_and_metadata_release() -> None:
    callback = FixtureCallback()
    bridge = gateway(callback)
    plan = request()
    result = bridge.check(plan)
    revoked = policy().model_copy(update={"allowed_assets": ["B"]})
    bridge.guard = AccessGuard(revoked)
    with pytest.raises(ValueError):
        bridge.cached_result(plan)
    with pytest.raises(ValueError):
        bridge.release_metadata(plan, result)
    assert len(callback.requests) == 1


def test_exact_request_cache_does_not_reuse_prefix_and_cannot_be_mutated_by_returned_object() -> None:
    callback = FixtureCallback()
    bridge = gateway(callback)
    plan = request()
    result = bridge.check(plan)
    result.response.check_results["data_presence"] = False
    cached = bridge.check(plan)
    assert cached.response.check_results["data_presence"] is True
    assert cached.transport_evidence == "CACHE_REUSE_UNVERIFIED"
    shorter = plan.model_dump(mode="python")
    shorter["data"]["end"] = date(2020, 1, 3)
    bridge.check(QuantConnectCheckRequest.model_validate(shorter))
    assert len(callback.requests) == 2


def test_reserved_dispatch_invokes_callback_even_if_an_authorized_cached_result_exists() -> None:
    callback = FixtureCallback()
    bridge = gateway(callback)
    plan = request()
    bridge.check(plan)
    assert bridge.cached_result(plan) is not None and len(callback.requests) == 1
    dispatched = bridge.dispatch(plan)
    assert dispatched.transport_evidence == "CALLBACK_UNVERIFIED"
    assert len(callback.requests) == 2


@pytest.mark.parametrize("extra", [
    {"bars": [{"close": 12345.6789}]}, {"sid": "SECRET_SID"}, {"data_sha256": "e" * 64},
    {"stdout": "SECRET_SID 12345.6789"}, {"history": []}, {"plot": "data:image/png;base64,SECRET"},
])
def test_raw_or_reconstructable_provider_output_is_refused_without_echoing_it(extra: dict) -> None:
    plan = request()
    bridge = gateway(lambda _: response_bytes(plan, **extra))
    with pytest.raises(QuantConnectDispatchError) as rejected:
        bridge.check(plan)
    assert rejected.value.execution_status == "UNKNOWN"
    assert rejected.value.error_category == "MALFORMED_RESPONSE"
    assert "SECRET" not in str(rejected.value) and "12345" not in str(rejected.value)
    assert bridge._cache == {}


@pytest.mark.parametrize("raw", [
    b'{"status":"ERROR","status":"COMPLETED"}',
    b'{"check_results":{"no_data_export":true,"no_data_export":false}}',
    b'{"check_results":{"no_data_export":NaN}}', b'{"check_results":{"no_data_export":1e400}}',
    b'[]', b'not JSON', b'\xff', b' ' * 65_537,
])
def test_imported_metadata_rejects_ambiguous_malformed_nonfinite_or_oversized_json(raw: bytes) -> None:
    with pytest.raises(ValueError, match="verdict-only metadata"):
        gateway().accept_metadata(request(), raw)


@pytest.mark.parametrize("field,value", [
    ("project_id", 456), ("request_sha256", "c" * 64),
    ("source_commit", "d" * 40), ("config_sha256", "e" * 64),
    ("check_results", {"no_data_export": True}),
    ("check_results", {"data_presence": True, "requested_bounds": True, "no_data_export": 1}),
    ("check_results", {"data_presence": True, "requested_bounds": True, "no_data_export": "true"}),
])
def test_response_cannot_substitute_other_request_or_untyped_verdict(field: str, value) -> None:
    plan = request()
    with pytest.raises(ValueError):
        gateway().accept_metadata(plan, response_bytes(plan, **{field: value}))


def test_imported_success_is_a_claim_and_false_check_is_not_an_execution_error() -> None:
    plan = request()
    payload = response_payload(plan)
    payload["check_results"]["data_presence"] = False
    bridge = gateway()
    result = bridge.accept_metadata(plan, json.dumps(payload).encode())
    assert result.transport_evidence == "IMPORTED_UNVERIFIED"
    assert result.platform_integration == "NOT_RUN"
    assert result.response.status == "COMPLETED" and result.response.error_category == "NONE"
    assert result.response.check_results["data_presence"] is False
    # A supplied file cannot seed a later apparent callback execution/cache hit.
    assert bridge.cached_result(plan) is None
    released = json.loads(bridge.release_metadata(plan, result))
    assert set(released) == {"response", "transport_evidence", "platform_integration", "evidence_authentication", "market_data_export"}
    assert "bars" not in released["response"]


def test_completed_metadata_must_include_the_cloud_retention_verdict_even_without_a_gateway() -> None:
    payload = response_payload(request(), check_results={"data_presence": True})
    with pytest.raises(ValidationError, match="no_data_export"):
        QuantConnectCheckResponse.model_validate(payload)


@pytest.mark.parametrize("error", [TimeoutError("SECRET PRICE 12345"), RuntimeError("SECRET PRICE 12345")])
def test_callback_failure_is_unknown_without_retry_or_provider_error_text(error: Exception) -> None:
    calls = []
    def fail(raw: bytes) -> bytes:
        calls.append(raw)
        raise error
    bridge = gateway(fail)
    with pytest.raises(QuantConnectDispatchError) as rejected:
        bridge.check(request())
    assert len(calls) == 1 and bridge._cache == {}
    assert rejected.value.execution_status == "UNKNOWN"
    assert "SECRET" not in str(rejected.value)


@pytest.mark.parametrize("status,error_category", [("ACCEPTED", "NONE"), ("RUNNING", "NONE"), ("UNKNOWN", "TIMEOUT"), ("ERROR", "PROVIDER_ERROR")])
def test_unfinished_or_failed_submissions_are_not_cached(status: str, error_category: str) -> None:
    plan = request()
    bridge = gateway()
    result = bridge.accept_metadata(plan, response_bytes(plan, status=status, error_category=error_category, check_results={}))
    assert result.response.status == status
    assert bridge.cached_result(plan) is None


def test_real_cloud_configuration_requires_separate_runtime_grant_and_still_only_returns_controls() -> None:
    payload = policy().model_dump(mode="python")
    payload.update(provider="QUANTCONNECT_CLOUD", authorization="OWNER_AUTHORIZED",
                   approval_reference="synthetic-owner-record-not-real-approval", approved_by="Synthetic owner",
                   approved_at=datetime(2020, 1, 1, 18, tzinfo=timezone.utc))
    cloud_policy = AccessPolicy.model_validate(payload)
    plan_payload = request().model_dump(mode="python")
    plan_payload["data"]["provider"] = "QUANTCONNECT_CLOUD"
    plan = QuantConnectCheckRequest.model_validate(plan_payload)
    callback = FixtureCallback()
    denied = QuantConnectGateway(QuantConnectCapabilities(project_ids=[123]), AccessGuard(cloud_policy), callback)
    with pytest.raises(ValueError, match="trusted runtime grant"):
        denied.check(plan)
    assert callback.requests == []
    allowed = QuantConnectGateway(QuantConnectCapabilities(project_ids=[123]),
        AccessGuard(cloud_policy, trusted_policy_sha256s=frozenset([canonical_policy_sha256(cloud_policy)])), callback)
    result = allowed.check(plan)
    assert result.market_data_export == "NOT_SUPPORTED" and result.platform_integration == "NOT_RUN"


def test_construct_and_in_place_mutations_do_not_bypass_request_capabilities_or_guard() -> None:
    callback = FixtureCallback()
    bridge = gateway(callback)
    invalid = request().model_copy(update={"operation": "raw_export"})
    with pytest.raises(ValueError):
        bridge.check(invalid)
    invalid = request()
    invalid.data.fields.append("open")
    with pytest.raises(ValueError):
        bridge.check(invalid)
    bridge.capabilities.project_ids.append(123)
    with pytest.raises(ValueError):
        bridge.check(request())
    assert callback.requests == []


def test_normalized_completion_keeps_execution_success_separate_from_failed_check_and_unknown_cost() -> None:
    payload = response_payload(request())
    payload["check_results"]["data_presence"] = False
    normalized = normalize_qc_check_response(QuantConnectCheckResponse.model_validate(payload))
    assert normalized.status == "completed" and normalized.success is True
    assert normalized.check_passed is False
    assert normalized.platform_task_id is None and normalized.invocation_id is None and normalized.cell_index is None
    assert normalized.actual_cost_microusd is None
    assert normalized.request_sha256 == qc_request_sha256(request())


@pytest.mark.parametrize("status,category,expected", [
    ("ACCEPTED", "NONE", "accepted"), ("RUNNING", "NONE", "running"),
    ("ERROR", "PROVIDER_ERROR", "error"), ("UNKNOWN", "TIMEOUT", "UNKNOWN"),
])
def test_normalization_preserves_only_bounded_status_and_error_metadata(status: str, category: str, expected: str) -> None:
    source = QuantConnectCheckResponse.model_validate(response_payload(request(), status=status,
        error_category=category, check_results={}))
    normalized = normalize_qc_check_response(source)
    assert normalized.status == expected and normalized.error_category == category
    assert normalized.check_passed is None
    assert normalized.platform_task_id is None and normalized.actual_cost_microusd is None


def test_separately_supplied_execution_reference_is_preserved_without_becoming_authentication() -> None:
    source = QuantConnectCheckResponse.model_validate(response_payload(request()))
    normalized = normalize_qc_check_response(source, invocation_id="fixture-invocation-not-a-real-job",
                                              cell_index=0, actual_cost_microusd=0)
    assert normalized.platform_task_id is None
    assert normalized.invocation_id == "fixture-invocation-not-a-real-job" and normalized.cell_index == 0
    assert normalized.actual_cost_microusd == 0
    assert "evidence_authentication" not in normalized.model_dump()


def test_gateway_ledger_integration_persists_no_id_completion_as_unknown_and_does_not_resubmit(tmp_path: Path) -> None:
    plan = request()
    callback = FixtureCallback()
    bridge = gateway(callback)
    budget = TaskBudget(max_attempts=2, operation_limits={"research_check": 2})
    ledger = TaskLedger(tmp_path / "ledger.sqlite", tmp_path / "metadata", budget)
    spec = TaskSpec(correlation_id="fixture-cloud-check", operation="research_check", project_id=plan.project_id,
                    source_commit=plan.source_commit, config_sha256=plan.config_sha256,
                    request_sha256=qc_request_sha256(plan))
    dispatches = []
    def transport(_spec, _attempt):
        dispatches.append(_spec.request_sha256)
        return normalize_qc_check_response(bridge.dispatch(plan).response)
    record = ledger.submit(spec, transport)
    assert record.status == "UNKNOWN" and record.attempts[0].reported_status == "completed"
    assert record.attempts[0].check_passed is True
    assert record.attempts[0].raw_metadata[0].capture_kind == "LOCAL_NORMALIZED_METADATA"
    assert record.attempts[0].raw_metadata[0].evidence_authentication == "NOT_AUTHENTICATED"
    restarted = TaskLedger(tmp_path / "ledger.sqlite", tmp_path / "metadata", budget)
    restarted.submit(spec, transport)
    with pytest.raises(ValueError, match="reconciled"):
        restarted.submit(spec, transport, retry=True)
    assert len(dispatches) == len(callback.requests) == 1
