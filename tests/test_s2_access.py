"""Synthetic pre-I/O denials; a policy file alone is never a real-data grant."""
from datetime import date, datetime, timezone

import pytest

from bbag.contracts import TimeRange
from bbag.s2_access import AccessGuard, AccessPolicy, DataRequest, canonical_policy_sha256, derive_classification


AS_OF = datetime(2020, 1, 10, 23, tzinfo=timezone.utc)


def policy(**changes):
    payload = dict(policy_id="SYNTHETIC_SCOPE", provider="SYNTHETIC", development=TimeRange(start="2020-01-02", end="2020-01-10"),
        sealed=[TimeRange(start="2020-02-01", end="2020-02-10")], allowed_assets=["A", "B"], allowed_fields=["open", "close", "volume"])
    payload.update(changes)
    return AccessPolicy(**payload)


def request(**changes):
    payload = dict(provider="SYNTHETIC", start="2020-01-02", end="2020-01-10", assets=["A"], fields=["close"], as_of=AS_OF, classification="DEVELOPMENT")
    payload.update(changes)
    return DataRequest(**payload)


def cloud_policy():
    return policy(provider="QUANTCONNECT_CLOUD", authorization="OWNER_AUTHORIZED", approval_reference="SYNTHETIC_OWNER_RECORD_NOT_A_REAL_GRANT", approved_by="fixture", approved_at=AS_OF)


@pytest.mark.parametrize("operation", ["query", "batch", "cache_read", "cache_write", "quality", "export", "cloud_dispatch"])
def test_synthetic_development_explicitly_authorized(operation):
    accepted = AccessGuard(policy()).authorize(request(), operation)
    assert accepted.start == date(2020, 1, 2)


@pytest.mark.parametrize("operation", ["query", "batch", "cache_read", "cache_write", "quality", "export", "cloud_dispatch"])
@pytest.mark.parametrize("changes", [
    {"classification": "UNKNOWN"}, {"classification": "SEALED_FINAL"}, {"classification": "SEALED_DERIVATIVE"},
    {"derived_from": ["DEVELOPMENT", "SEALED_FINAL"]}, {"derived_from": ["UNKNOWN"]},
    {"start": "2020-01-01"}, {"end": "2020-01-11", "as_of": datetime(2020, 1, 12, tzinfo=timezone.utc)},
    {"assets": ["UNAUTHORIZED"]}, {"fields": ["high"]}, {"purpose": "DEVELOPMENT"},
    {"as_of": datetime(2020, 1, 9, tzinfo=timezone.utc)},
])
def test_denied_before_any_transport(operation, changes):
    calls = []
    with pytest.raises(ValueError):
        AccessGuard(policy()).authorize(request(**changes), operation)
        calls.append("transport")
    assert calls == []


@pytest.mark.parametrize("sources,expected", [
    (["DEVELOPMENT"], "DEVELOPMENT"), (["DEVELOPMENT", "SEALED_FINAL"], "SEALED_DERIVATIVE"),
    (["SEALED_DERIVATIVE"], "SEALED_DERIVATIVE"), (["DEVELOPMENT", "UNKNOWN"], "UNKNOWN"),
    ([], "UNKNOWN"), ("DEVELOPMENT", "UNKNOWN"), ([{}], "UNKNOWN"), ([1], "UNKNOWN"),
])
def test_derivative_classification_inherits_taint(sources, expected):
    assert derive_classification(sources) == expected


def test_artifact_actual_classification_cannot_be_replaced_by_safe_request():
    guard = AccessGuard(policy())
    for classification, ancestry in [("SEALED_FINAL", []), ("DEVELOPMENT", ["SEALED_FINAL"]), ("UNKNOWN", [])]:
        with pytest.raises(ValueError):
            guard.authorize_artifact(request(), classification=classification, derived_from=ancestry)


def test_whole_batch_authorized_before_first_callback():
    calls = []
    with pytest.raises(ValueError):
        approved = AccessGuard(policy()).authorize_batch([request(), request(assets=["C"])])
        calls.extend(approved)
    assert calls == []


def test_forged_owner_policy_is_not_runtime_authority():
    forged = cloud_policy()
    with pytest.raises(ValueError, match="trusted runtime grant"):
        AccessGuard(forged).authorize(request(provider="QUANTCONNECT_CLOUD", output_mode="CONTROL_ONLY"), "cloud_dispatch")
    approved = AccessGuard(forged, trusted_policy_sha256s=frozenset({canonical_policy_sha256(forged)}))
    assert approved.authorize(request(provider="QUANTCONNECT_CLOUD", output_mode="CONTROL_ONLY"), "cloud_dispatch").provider == "QUANTCONNECT_CLOUD"


@pytest.mark.parametrize("operation", ["query", "quality", "export", "cache_read", "cache_write", "cloud_dispatch"])
def test_trusted_cloud_grant_still_denies_local_market_data(operation):
    p = cloud_policy()
    guard = AccessGuard(p, trusted_policy_sha256s=frozenset({canonical_policy_sha256(p)}))
    with pytest.raises(ValueError, match="cannot be imported/exported"):
        guard.authorize(request(provider="QUANTCONNECT_CLOUD", output_mode="LOCAL_DATA"), operation)


@pytest.mark.parametrize("operation", ["query", "batch", "quality", "export"])
def test_control_only_channel_never_authorizes_data_methods(operation):
    p = cloud_policy()
    guard = AccessGuard(p, trusted_policy_sha256s=frozenset({canonical_policy_sha256(p)}))
    with pytest.raises(ValueError):
        guard.authorize(request(provider="QUANTCONNECT_CLOUD", output_mode="CONTROL_ONLY"), operation)


def test_cloud_internal_processing_is_location_bound_and_export_remains_denied():
    p = cloud_policy()
    registry = frozenset({canonical_policy_sha256(p)})
    req = request(provider="QUANTCONNECT_CLOUD", output_mode="IN_CLOUD_DATA")
    with pytest.raises(ValueError, match="inside QuantConnect cloud"):
        AccessGuard(p, trusted_policy_sha256s=registry).authorize(req)
    cloud = AccessGuard(p, trusted_policy_sha256s=registry, execution_location="QUANTCONNECT_CLOUD")
    assert cloud.authorize(req).output_mode == "IN_CLOUD_DATA"
    with pytest.raises(ValueError):
        cloud.authorize(req, "export")


def test_grant_digest_binds_exact_scope_and_policy_copy_is_private():
    p = cloud_policy()
    digest = canonical_policy_sha256(p)
    guard = AccessGuard(p, trusted_policy_sha256s=frozenset({digest}))
    p.allowed_assets.append("C")
    guard.policy.allowed_assets.append("C")
    assert guard.policy.allowed_assets == ["A", "B"]
    with pytest.raises(ValueError):
        AccessGuard(p, trusted_policy_sha256s=frozenset({digest})).authorize(request(provider="QUANTCONNECT_CLOUD", output_mode="CONTROL_ONLY"), "cloud_dispatch")


@pytest.mark.parametrize("changes", [{"assets": ["A", "A"]}, {"fields": ["close", "close"]}, {"start": "2020-01-11"}, {"as_of": datetime(2020, 1, 10)}, {"unexpected": True}, {"assets": [1]}])
def test_request_schema_rejects_ambiguous_or_unknown_parameters(changes):
    with pytest.raises(ValueError):
        request(**changes)


@pytest.mark.parametrize("changes", [
    {"sealed": [TimeRange(start="2020-01-10", end="2020-02-01")]},
    {"sealed": [TimeRange(start="2020-02-01", end="2020-02-10"), TimeRange(start="2020-02-10", end="2020-02-12")]},
    {"provider": "QUANTCONNECT_CLOUD"}, {"allowed_assets": ["A", "A"]}, {"max_query_days": True},
    {"timezone": "No/Such_Zone"}, {"approved_by": "not_an_owner_grant"},
])
def test_policy_rejects_inconsistent_or_fake_engineering_grants(changes):
    with pytest.raises(ValueError):
        policy(**changes)


def test_mutation_and_wrong_type_fail_closed():
    req = request()
    req.assets.append("C")
    guard = AccessGuard(policy())
    with pytest.raises(ValueError):
        guard.authorize(req)
    for bad in [{}, None, "request"]:
        with pytest.raises(ValueError):
            guard.authorize(bad)
    with pytest.raises(ValueError):
        guard.authorize(request(), "unknown")
    with pytest.raises(ValueError):
        AccessGuard(policy(), execution_location=[])


def test_workload_bound_is_not_a_permission_to_expand_range():
    guard = AccessGuard(policy(max_query_days=2))
    with pytest.raises(ValueError, match="workload"):
        guard.authorize(request())
    assert guard.authorize(request(end="2020-01-03")).end == date(2020, 1, 3)
