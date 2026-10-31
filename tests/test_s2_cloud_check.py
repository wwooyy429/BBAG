"""Synthetic callback inputs only; no SDK, platform permission or task usage."""
from datetime import date, datetime, timezone
import json

import pytest
from pydantic import ValidationError

from bbag.contracts import TimeRange
from bbag.s2_access import AccessGuard, AccessPolicy, DataRequest, STANDARD_FIELDS, canonical_policy_sha256
from bbag.s2_cloud_check import CloudCheckRejected, CloudStandardizationReceipt, run_cloud_data_check
from bbag.s2_cloud_data import (CloudAliasBinding, CloudDailySnapshot, CloudDataPlan,
    CloudMetadataCoverage, CloudResolution, cloud_plan_binding)
from bbag.s2_data import CalendarSession, DailyBar, MembershipEvent, SecurityIdentity, TickerMapping


def instant(day, hour=0):
    return datetime(2020, 1, day, hour, tzinfo=timezone.utc)


@pytest.fixture
def synthetic():
    policy = AccessPolicy(policy_id="SYNTHETIC_MOCK_NOT_REAL_AUTHORITY", authorization="OWNER_AUTHORIZED",
        provider="QUANTCONNECT_CLOUD", development=TimeRange(start=date(2020, 1, 2), end=date(2020, 1, 10)),
        allowed_assets=["SPY", "AAPL"], allowed_fields=list(STANDARD_FIELDS),
        approval_reference="SYNTHETIC_TEST_ONLY", approved_by="SYNTHETIC_TEST", approved_at=instant(1))
    digest = canonical_policy_sha256(policy)
    guard = AccessGuard(policy, trusted_policy_sha256s=frozenset({digest}), execution_location="QUANTCONNECT_CLOUD")
    request = DataRequest(provider="QUANTCONNECT_CLOUD", start=date(2020, 1, 2), end=date(2020, 1, 2),
        assets=["SPY"], fields=list(STANDARD_FIELDS), as_of=instant(10, 22),
        classification="DEVELOPMENT", output_mode="IN_CLOUD_DATA")
    plan = CloudDataPlan(project_id=38698074, source_commit="a"*40, config_sha256="b"*64,
        owner_policy_sha256=digest, request=request)
    binding = cloud_plan_binding(plan)
    sid = "SYNTHETIC_SID_DO_NOT_EXPORT"
    resolution = CloudResolution(binding=binding, identity_data_version="SYNTHETIC_V1",
        aliases=[CloudAliasBinding(alias="SPY", sid=sid, valid_from=date(2020, 1, 2), valid_to=date(2020, 1, 10), known_at=instant(1))],
        identities=[SecurityIdentity(sid=sid, listed_from=date(2020, 1, 1), known_at=instant(1))],
        tickers=[TickerMapping(sid=sid, ticker="SPY", valid_from=date(2020, 1, 1), known_at=instant(1))],
        membership=[MembershipEvent(sid=sid, effective_on=date(2020, 1, 1), member=True, known_at=instant(1))],
        corporate_actions=[], coverage=CloudMetadataCoverage(identity_knowledge="PROVIDER_DOCUMENTED",
            membership="PROVIDED_NONEMPTY", corporate_actions="NOT_PROVIDED"))
    session = CalendarSession(session=date(2020, 1, 2), open_at=datetime(2020, 1, 2, 14, 30, tzinfo=timezone.utc),
        close_at=instant(2, 21), signal_at=instant(2, 21))
    snapshot = CloudDailySnapshot(binding=binding, provider_data_version="SYNTHETIC_BAR_V1", calendar_id="SYNTHETIC_XNYS",
        calendar_version="SYNTHETIC_1", retrieved_at=instant(11), sessions=[session],
        availability_basis="BAR_END_PROXY", availability=[],
        bars=[DailyBar(sid=sid, session=date(2020, 1, 2), open=100.0, high=102.0, low=99.0,
            close=101.0, volume=123456.0, available_at=instant(2, 21))])
    return plan, policy, guard, resolution, snapshot


def run(inputs, **kwargs):
    plan, _, guard, resolution, snapshot = inputs
    return run_cloud_data_check(plan, guard=guard, resolver=lambda *_: resolution,
        fetcher=lambda *_: snapshot, **kwargs)


def test_fixed_control_boundary_and_unverified_provider_truth(synthetic):
    result = run(synthetic)
    assert result.outcome == "CHECKS_COMPLETED"
    assert result.checks.requested_field_coverage == result.checks.membership_quality_coverage == "PASS"
    assert result.checks.corporate_action_records == "UNVERIFIED"
    assert result.checks.provider_disclosure_revision_truth == result.checks.provider_identity_history_truth == "UNVERIFIED"
    assert result.platform_integration == "NOT_RUN" and result.independent_review == "PENDING"
    raw = result.model_dump_json()
    for forbidden in ("SYNTHETIC_SID_DO_NOT_EXPORT", "123456", "source_sha256", '"bars":', '"available_bars":', '"coverage":'):
        assert forbidden not in raw


@pytest.mark.parametrize("location,trusted", [("LOCAL", True), ("QUANTCONNECT_CLOUD", False)])
def test_invalid_runtime_context_stops_before_first_callback(synthetic, location, trusted):
    plan, policy, _, _, _ = synthetic
    guard = AccessGuard(policy, trusted_policy_sha256s=frozenset({canonical_policy_sha256(policy)}) if trusted else frozenset(),
        execution_location=location)
    def never(*_):
        pytest.fail("provider callback must not run")
    result = run_cloud_data_check(plan, guard=guard, resolver=never, fetcher=never)
    assert result.outcome == "ERROR" and result.checks is None


def test_logs_and_exception_text_do_not_escape(synthetic, capsys):
    plan, _, guard, _, _ = synthetic
    def bad(*_):
        print("SECRET_MARKET_DATA")
        raise ValueError("SECRET_MARKET_DATA")
    result = run_cloud_data_check(plan, guard=guard, resolver=bad, fetcher=bad)
    assert result.error_category == "STANDARDIZATION_REJECTED"
    assert "SECRET" not in result.model_dump_json()
    assert capsys.readouterr() == ("", "")


def test_no_empty_input_false_pass(synthetic):
    plan, policy, guard, resolution, snapshot = synthetic
    resolution = resolution.model_copy(update={"membership": [], "coverage": CloudMetadataCoverage(
        identity_knowledge="PROVIDER_DOCUMENTED", membership="PROVIDED_EMPTY", corporate_actions="PROVIDED_EMPTY")})
    snapshot = snapshot.model_copy(update={"bars": []})
    result = run((plan, policy, guard, resolution, snapshot))
    assert result.checks.requested_field_coverage == "FAIL"
    assert result.checks.membership_quality_coverage == result.checks.membership_records == "UNVERIFIED"
    assert result.checks.corporate_action_records == "UNVERIFIED"


def test_unbound_forged_plan_is_closed_and_never_calls(synthetic):
    plan, _, guard, _, _ = synthetic
    def never(*_):
        pytest.fail("callback must not run")
    bad = plan.model_copy(update={"request": plan.request.model_copy(update={"assets": ["SECRET_OUTSIDE"]})})
    with pytest.raises(CloudCheckRejected, match="^INVALID_REQUEST$"):
        run_cloud_data_check(bad, guard=guard, resolver=never, fetcher=never)


@pytest.mark.parametrize("mutation", ["raw", "truth", "numeric", "contradictory"])
def test_receipt_import_rejects_extra_truth_claims_and_inconsistent_states(synthetic, mutation):
    payload = json.loads(run(synthetic).model_dump_json())
    if mutation == "raw":
        payload["raw_prices"] = [1]
    elif mutation == "truth":
        payload["checks"]["provider_disclosure_revision_truth"] = "PASS"
    elif mutation == "numeric":
        payload["checks"]["requested_field_coverage"] = 1
    else:
        payload["outcome"] = "ERROR"
    with pytest.raises(ValidationError):
        CloudStandardizationReceipt.model_validate_json(json.dumps(payload))
