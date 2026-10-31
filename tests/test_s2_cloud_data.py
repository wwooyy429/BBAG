"""SYNTHETIC mock SDK/metadata only; no real grant, task, data or provider I/O."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from bbag.contracts import TimeRange
from bbag.s2_access import AccessGuard, AccessPolicy, DataRequest, STANDARD_FIELDS, canonical_policy_sha256
from bbag.s2_cloud_data import (
    CloudAliasBinding, CloudDataPlan, CloudDailySnapshot, CloudMappingGuard, CloudResolution,
    CloudSnapshotContext, SDKHistory, build_cloud_bundle, cloud_plan_binding,
    extract_sdk_daily_snapshot, revalidate_cloud_result,
)
from bbag.s2_data import data_quality, historical_universe, to_s1_panel


def at(day, hour=21, minute=0):
    return datetime(2020, 1, day, hour, minute, tzinfo=timezone.utc)


def fixture(*, membership=True):
    policy = AccessPolicy(policy_id="SYNTHETIC_CLOUD_GUARD_DECLARATION_NOT_REAL_PERMISSION",
        authorization="OWNER_AUTHORIZED", provider="QUANTCONNECT_CLOUD", timezone="America/New_York",
        development=TimeRange(start="2020-01-02", end="2020-01-10"), allowed_assets=["SPY", "AAPL"],
        allowed_fields=list(STANDARD_FIELDS), approval_reference="SYNTHETIC_UNIT_FIXTURE",
        approved_by="SYNTHETIC_UNIT_TEST", approved_at=at(1), max_query_days=9)
    digest = canonical_policy_sha256(policy)
    guard = AccessGuard(policy, trusted_policy_sha256s=frozenset([digest]), execution_location="QUANTCONNECT_CLOUD")
    request = DataRequest(provider="QUANTCONNECT_CLOUD", start="2020-01-02", end="2020-01-03",
        assets=["SPY", "AAPL"], fields=list(STANDARD_FIELDS), as_of=at(3, 22),
        classification="DEVELOPMENT", output_mode="IN_CLOUD_DATA")
    plan = CloudDataPlan(project_id=38698074, source_commit="a" * 40, config_sha256="b" * 64,
        owner_policy_sha256=digest, request=request)
    binding = cloud_plan_binding(plan)
    aliases = [dict(alias=a, sid="SYNTHETIC_SID_" + a, valid_from="2020-01-02", valid_to="2020-01-10", known_at=at(1))
               for a in request.assets]
    resolution = CloudResolution(binding=binding, identity_data_version="SYNTHETIC_EXPLICIT_IDENTITY_V1", aliases=aliases,
        identities=[dict(sid=r["sid"], listed_from="2019-01-01", known_at=at(1)) for r in aliases],
        tickers=[dict(sid=r["sid"], ticker=r["alias"], valid_from="2019-01-01", known_at=at(1)) for r in aliases],
        membership=[dict(sid=r["sid"], effective_on="2020-01-02", member=True, known_at=at(1)) for r in aliases] if membership else [],
        corporate_actions=[], coverage=dict(identity_knowledge="PROVIDER_DOCUMENTED",
            membership="PROVIDED_NONEMPTY" if membership else "NOT_PROVIDED", corporate_actions="NOT_PROVIDED"))
    context = CloudSnapshotContext(binding=binding, provider_data_version="SYNTHETIC_RAW_V1", calendar_id="SYNTHETIC_EXPLICIT_CALENDAR",
        calendar_version="SYNTHETIC_CALENDAR_V1", retrieved_at=at(3, 23),
        sessions=[dict(session=f"2020-01-0{d}", open_at=at(d, 14, 30), close_at=at(d), signal_at=at(d, 21, 1)) for d in (2, 3)],
        availability_basis="BAR_END_PROXY", availability=[])
    bars = [dict(sid=r["sid"], session=f"2020-01-0{d}", open=p, high=p + 2, low=p - 1, close=p + 1,
                 volume=100.0, available_at=at(d)) for d in (2, 3) for r, p in zip(aliases, (10.0, 20.0))]
    snapshot = CloudDailySnapshot(**context.model_dump(mode="python"), bars=bars)
    return plan, guard, resolution, context, snapshot


def run(plan, guard, resolution, snapshot):
    return build_cloud_bundle(plan, guard=guard, resolver=lambda _: resolution, fetcher=lambda *_: snapshot)


def native_bars(snapshot):
    return [SimpleNamespace(symbol=SimpleNamespace(id=row.sid), time=at(row.session.day, 14, 30),
        end_time=at(row.session.day), is_fill_forward=False,
        **{name: getattr(row, name) for name in STANDARD_FIELDS}) for row in snapshot.bars]


def test_original_alias_policy_unchanged_and_raw_values_trace_to_sid_bundle():
    plan, guard, resolution, context, snapshot = fixture()
    before = guard.policy.model_dump(mode="json")
    calls = []
    result = build_cloud_bundle(plan, guard=guard,
        resolver=lambda p: calls.append(("resolver", p.request.assets)) or resolution,
        fetcher=lambda p, r: calls.append(("fetcher", [x.sid for x in r.aliases])) or snapshot)
    assert [c[0] for c in calls] == ["resolver", "fetcher"]
    assert result.bundle.provenance.provider == "QUANTCONNECT_CLOUD"
    assert result.bundle.request.assets == ["SYNTHETIC_SID_SPY", "SYNTHETIC_SID_AAPL"]
    assert [(b.open, b.close, b.volume) for b in result.bundle.bars] == [(10.0, 11.0, 100.0), (20.0, 21.0, 100.0)] * 2
    assert result.mapping_guard.policy.model_dump(mode="json") == guard.policy.model_dump(mode="json") == before
    assert guard.policy.allowed_assets == ["SPY", "AAPL"]
    assert data_quality(result.bundle, result.bundle.request, result.mapping_guard).expected_bars == 4
    assert [r.assets for r in historical_universe(result.bundle, result.bundle.request, result.mapping_guard).rows] == [["SYNTHETIC_SID_AAPL", "SYNTHETIC_SID_SPY"]] * 2
    assert result.availability_basis == "BAR_END_PROXY"
    assert plan.status == "DRAFT" and plan.purpose == "ENGINEERING_TEST"
    assert result.platform_integration == "NOT_RUN" and result.resolution.coverage.disclosure_revision == "UNVERIFIED"
    with pytest.raises(ValueError):
        to_s1_panel(result.bundle, result.bundle.request, result.mapping_guard)


@pytest.mark.parametrize("changes", [
    {"start": "2020-01-01"}, {"end": "2020-01-11"}, {"assets": ["MSFT"]}, {"assets": ["SPY", "SPY"]},
    {"fields": ["close"]}, {"classification": "SEALED_FINAL"}, {"derived_from": ["SEALED_FINAL"]},
    {"output_mode": "LOCAL_DATA"}, {"provider": "SYNTHETIC"}, {"purpose": "DEVELOPMENT"},
])
def test_alias_scope_rejected_before_any_provider_callback(changes):
    plan, guard, resolution, _, snapshot = fixture()
    bad = plan.model_copy(update={"request": plan.request.model_copy(update=changes)})
    calls = []
    with pytest.raises(ValueError):
        build_cloud_bundle(bad, guard=guard, resolver=lambda *_: calls.append(1), fetcher=lambda *_: calls.append(2))
    assert not calls


@pytest.mark.parametrize("fault", ["local", "untrusted", "wrong_digest"])
def test_cloud_location_and_original_trusted_policy_required_before_io(fault):
    plan, guard, resolution, _, snapshot = fixture()
    if fault == "local":
        guard = AccessGuard(guard.policy, trusted_policy_sha256s=frozenset([guard.policy_sha256]))
    elif fault == "untrusted":
        guard = AccessGuard(guard.policy, execution_location="QUANTCONNECT_CLOUD")
    else:
        plan = plan.model_copy(update={"owner_policy_sha256": "c" * 64})
    calls = []
    with pytest.raises(ValueError):
        build_cloud_bundle(plan, guard=guard, resolver=lambda *_: calls.append(1), fetcher=lambda *_: calls.append(2))
    assert calls == []


@pytest.mark.parametrize("target", ["resolution", "snapshot"])
@pytest.mark.parametrize("key,value", [("project_id", 999), ("source_commit", "c" * 40), ("config_sha256", "c" * 64),
                                      ("request_sha256", "c" * 64), ("owner_policy_sha256", "c" * 64)])
def test_every_provider_binding_dimension_must_match_original_plan(target, key, value):
    plan, guard, resolution, _, snapshot = fixture()
    bad = (resolution if target == "resolution" else snapshot).model_copy(update={"binding":
        cloud_plan_binding(plan).model_copy(update={key: value})})
    with pytest.raises(ValueError):
        run(plan, guard, bad if target == "resolution" else resolution, bad if target == "snapshot" else snapshot)


@pytest.mark.parametrize("fault", ["alias_as_sid", "duplicate_alias", "duplicate_sid", "crossed_ticker", "short_interval", "future_mapping", "extra_identity", "missing_identity", "unverified", "missing_known_at"])
def test_resolver_identity_and_knowledge_fail_closed_before_fetch(fault):
    plan, guard, resolution, _, snapshot = fixture()
    raw = resolution.model_dump(mode="python")
    if fault == "alias_as_sid": raw["aliases"][0]["sid"] = "SPY"
    elif fault == "duplicate_alias": raw["aliases"][1]["alias"] = "SPY"
    elif fault == "duplicate_sid": raw["aliases"][1]["sid"] = raw["aliases"][0]["sid"]
    elif fault == "crossed_ticker": raw["tickers"][0]["ticker"] = "AAPL"
    elif fault == "short_interval": raw["aliases"][0]["valid_to"] = date(2020, 1, 2)
    elif fault == "future_mapping": raw["aliases"][0]["known_at"] = at(4)
    elif fault == "extra_identity": raw["identities"].append(dict(raw["identities"][0], sid="SYNTHETIC_OTHER"))
    elif fault == "missing_identity": raw["identities"].pop()
    elif fault == "unverified": raw["coverage"]["identity_knowledge"] = "UNVERIFIED"
    elif fault == "missing_known_at": del raw["identities"][0]["known_at"]
    calls = []
    # model_construct deliberately bypasses initial validation; the boundary revalidates.
    bad = CloudResolution.model_construct(**raw)
    with pytest.raises(ValueError):
        build_cloud_bundle(plan, guard=guard, resolver=lambda _: bad, fetcher=lambda *_: calls.append(1))
    assert calls == []


@pytest.mark.parametrize("operation", ["cache_read", "cache_write", "export", "cloud_dispatch", "batch", "unknown"])
def test_sid_mapping_context_is_a_narrow_view_not_a_new_grant(operation):
    plan, guard, resolution, _, snapshot = fixture()
    result = run(plan, guard, resolution, snapshot)
    with pytest.raises(ValueError):
        result.mapping_guard.authorize(result.bundle.request, operation)
    with pytest.raises(AttributeError):
        result.mapping_guard._origin = AccessGuard(guard.policy)


@pytest.mark.parametrize("changes", [{"assets": ["SPY"]}, {"assets": ["SYNTHETIC_OTHER"]}, {"start": "2020-01-01"},
    {"end": "2020-01-06"}, {"as_of": at(4)}, {"output_mode": "CONTROL_ONLY"}, {"derived_from": ["DEVELOPMENT"]}])
def test_sid_selection_must_remain_a_subset_of_original_alias_request(changes):
    plan, guard, resolution, _, snapshot = fixture()
    result = run(plan, guard, resolution, snapshot)
    with pytest.raises(ValueError):
        result.mapping_guard.authorize(result.bundle.request.model_copy(update=changes), "query")


def test_future_perturbation_and_truncation_leave_past_quality_and_membership_unchanged():
    plan, guard, resolution, _, snapshot = fixture()
    a = run(plan, guard, resolution, snapshot)
    changed = snapshot.model_dump(mode="python")
    for row in changed["bars"]:
        if row["session"] == date(2020, 1, 3):
            for field in ("open", "high", "low", "close"): row[field] *= 10
    metadata = resolution.model_dump(mode="python")
    metadata["membership"].append(dict(sid="SYNTHETIC_SID_SPY", effective_on="2020-01-03", member=False, known_at=at(3, 20)))
    b = run(plan, guard, CloudResolution.model_validate(metadata), CloudDailySnapshot.model_validate(changed))
    request = a.bundle.request.model_copy(update={"end": date(2020, 1, 2), "as_of": at(2, 22)})
    assert data_quality(a.bundle, request, a.mapping_guard) == data_quality(b.bundle, request, b.mapping_guard)
    assert historical_universe(a.bundle, request, a.mapping_guard) == historical_universe(b.bundle, request, b.mapping_guard)
    small_plan = plan.model_copy(update={"request": plan.request.model_copy(update={"end": date(2020, 1, 2), "as_of": at(2, 22)})})
    small_resolution = resolution.model_copy(update={"binding": cloud_plan_binding(small_plan)})
    small_snapshot = snapshot.model_copy(update={"binding": cloud_plan_binding(small_plan), "sessions": snapshot.sessions[:1], "bars": snapshot.bars[:2]})
    c = run(small_plan, guard, small_resolution, small_snapshot)
    assert [(r.session, r.sid, r.close) for r in c.bundle.bars] == [(r.session, r.sid, r.close) for r in a.bundle.bars[:2]]


def test_real_missing_bar_is_not_filled_and_missing_event_knowledge_is_not_invented():
    plan, guard, resolution, _, snapshot = fixture()
    snapshot = snapshot.model_copy(update={"bars": snapshot.bars[:-1]})
    result = run(plan, guard, resolution, snapshot)
    assert len(result.bundle.bars) == 3
    rows = data_quality(result.bundle, result.bundle.request, result.mapping_guard).rows
    assert [(r.session, r.sid) for r in rows if not r.present] == [(date(2020, 1, 3), "SYNTHETIC_SID_AAPL")]
    plan, guard, resolution, _, snapshot = fixture(membership=False)
    result = run(plan, guard, resolution, snapshot)
    assert result.bundle.membership == [] and result.resolution.coverage.membership == "NOT_PROVIDED"
    assert result.bundle.corporate_actions == [] and result.resolution.coverage.corporate_actions == "NOT_PROVIDED"


def test_result_revalidation_rejects_model_copy_and_returned_list_pollution():
    plan, guard, resolution, _, snapshot = fixture()
    result = run(plan, guard, resolution, snapshot)
    assert revalidate_cloud_result(plan, result, guard=guard).bundle == result.bundle
    polluted = result.bundle.model_copy(update={"bars": result.bundle.bars[:-1]})
    with pytest.raises(ValueError): revalidate_cloud_result(plan, replace(result, bundle=polluted), guard=guard)
    result.resolution.aliases[0].sid = "SYNTHETIC_CHANGED"
    with pytest.raises(ValueError): revalidate_cloud_result(plan, result, guard=guard)
    # Mapping guard held immutable captured resolution, not the caller's list.
    assert result.mapping_guard.authorize(result.bundle.request).assets == result.bundle.request.assets
    other = AccessGuard(guard.policy, trusted_policy_sha256s=frozenset([guard.policy_sha256]), execution_location="QUANTCONNECT_CLOUD")
    with pytest.raises(ValueError): revalidate_cloud_result(plan, run(plan, guard, resolution, snapshot), guard=other)


def test_native_sdk_extraction_keeps_raw_fields_timezone_and_explicit_proxy():
    plan, guard, resolution, context, snapshot = fixture()
    native = native_bars(snapshot)
    for row in native:
        row.open = Decimal(str(row.open))
        row.time = row.time.astimezone(__import__('zoneinfo').ZoneInfo("America/New_York")).replace(tzinfo=None)
        row.end_time = row.end_time.astimezone(__import__('zoneinfo').ZoneInfo("America/New_York")).replace(tzinfo=None)
    calls = []
    extracted = extract_sdk_daily_snapshot(plan, resolution, context, guard=guard,
        history_factory=lambda *_: calls.append("SYNTHETIC_MOCK_ONLY") or SDKHistory(native, "raw", False, False))
    assert calls == ["SYNTHETIC_MOCK_ONLY"] and extracted.bars == snapshot.bars


@pytest.mark.parametrize("fault", ["adjusted", "fill_forward", "extended", "bad_sid", "bad_time", "nan", "bool", "string", "rows"])
def test_native_sdk_settings_fields_and_boundaries_are_not_silently_coerced(fault):
    plan, guard, resolution, context, snapshot = fixture()
    native = native_bars(snapshot)
    settings = ["raw", False, False]
    if fault == "adjusted": settings[0] = "adjusted"
    elif fault == "fill_forward": native[0].is_fill_forward = True
    elif fault == "extended": settings[2] = True
    elif fault == "bad_sid": native[0].symbol.id = "SPY"
    elif fault == "bad_time": native[0].end_time = at(2, 20)
    elif fault == "nan": native[0].close = float("nan")
    elif fault == "bool": native[0].volume = True
    elif fault == "string": native[0].close = "11"
    elif fault == "rows": plan = plan.model_copy(update={"max_history_rows": 1})
    with pytest.raises(ValueError):
        extract_sdk_daily_snapshot(plan, resolution, context, guard=guard, history_factory=lambda *_: SDKHistory(native, *settings))


def test_documented_availability_requires_exact_explicit_provider_records():
    plan, guard, resolution, context, snapshot = fixture()
    available = [dict(session=b.session, sid=b.sid, available_at=b.available_at) for b in snapshot.bars]
    context = context.model_copy(update={"availability_basis": "PROVIDER_DOCUMENTED", "availability": available})
    extracted = extract_sdk_daily_snapshot(plan, resolution, context, guard=guard,
        history_factory=lambda *_: SDKHistory(native_bars(snapshot), "raw", False, False))
    assert extracted.availability_basis == "PROVIDER_DOCUMENTED"
    with pytest.raises(ValueError):
        extract_sdk_daily_snapshot(plan, resolution, context.model_copy(update={"availability": []}), guard=guard,
            history_factory=lambda *_: SDKHistory(native_bars(snapshot), "raw", False, False))


@pytest.mark.parametrize("fault", ["duplicate", "extra", "nan", "infinity", "overflow", "bool", "utf16"])
def test_strict_provider_json_uses_shared_duplicate_nonfinite_and_utf8_hooks(fault):
    plan, guard, resolution, _, snapshot = fixture()
    raw = snapshot.model_dump_json().encode()
    if fault == "duplicate": raw = raw.replace(b'{', b'{"normalization":"raw",', 1)
    elif fault == "extra": raw = raw[:-1] + b',"unexpected":1}'
    elif fault == "nan": raw = raw.replace(b'"open":10.0', b'"open":NaN', 1)
    elif fault == "infinity": raw = raw.replace(b'"open":10.0', b'"open":Infinity', 1)
    elif fault == "overflow": raw = raw.replace(b'"open":10.0', b'"open":1e400', 1)
    elif fault == "bool": raw = raw.replace(b'"open":10.0', b'"open":true', 1)
    elif fault == "utf16": raw = raw.decode().encode("utf-16")
    with pytest.raises(ValueError): run(plan, guard, resolution, raw)


def test_json_resolver_and_snapshot_bytes_have_same_semantics_without_local_export():
    plan, guard, resolution, _, snapshot = fixture()
    result = run(plan, guard, resolution.model_dump_json().encode(), snapshot.model_dump_json().encode())
    assert len(result.bundle.bars) == 4 and result.resolution.coverage.independent_platform_review == "PENDING"


@pytest.mark.parametrize("project", [True, 38698074.0, "38698074", 999])
def test_recorded_project_bound_requires_exact_int_not_another_project(project):
    plan, guard, _, _, _ = fixture()
    calls = []
    with pytest.raises(ValueError):
        build_cloud_bundle(plan.model_copy(update={"project_id": project}), guard=guard,
            resolver=lambda *_: calls.append(1), fetcher=lambda *_: calls.append(2))
    assert calls == []


@pytest.mark.parametrize("fault", ["late_delisting_knowledge", "late_ticker_end_knowledge", "duplicate_membership", "swapped_sids"])
def test_all_resolver_boundary_failures_precede_price_fetch(fault):
    plan, guard, resolution, _, snapshot = fixture()
    raw = resolution.model_dump(mode="python")
    if fault == "late_delisting_knowledge":
        raw["identities"][0].update(delisted_on=date(2020, 1, 8), delisting_known_at=at(4))
    elif fault == "late_ticker_end_knowledge":
        raw["tickers"][0].update(valid_to=date(2020, 1, 8), valid_to_known_at=at(4))
    elif fault == "duplicate_membership": raw["membership"].append(deepcopy(raw["membership"][0]))
    elif fault == "swapped_sids":
        raw["aliases"][0]["sid"], raw["aliases"][1]["sid"] = raw["aliases"][1]["sid"], raw["aliases"][0]["sid"]
    calls = []
    with pytest.raises(ValueError):
        build_cloud_bundle(plan, guard=guard, resolver=lambda _: CloudResolution.model_validate(raw),
            fetcher=lambda *_: calls.append(1))
    assert calls == []


@pytest.mark.parametrize("fault", ["outside_day", "wrong_market_date", "early_retrieval", "unknown_version"])
def test_known_calendar_version_clock_errors_precede_sdk_factory(fault):
    plan, guard, resolution, context, snapshot = fixture()
    raw = context.model_dump(mode="python")
    if fault == "outside_day": raw["sessions"][0]["session"] = date(2020, 1, 1)
    elif fault == "wrong_market_date":
        raw["sessions"][0].update(open_at=at(2, 1), close_at=at(2, 2), signal_at=at(2, 3))
    elif fault == "early_retrieval": raw["retrieved_at"] = at(2)
    elif fault == "unknown_version": raw["provider_data_version"] = "UNKNOWN"
    calls = []
    with pytest.raises(ValueError):
        extract_sdk_daily_snapshot(plan, resolution, context.model_copy(update=raw), guard=guard,
            history_factory=lambda *_: calls.append(1))
    assert calls == []


def test_sdk_daily_period_may_start_before_market_open_but_must_end_at_supplied_close():
    plan, guard, resolution, context, snapshot = fixture()
    native = native_bars(snapshot)
    for row in native:
        row.time = row.time.astimezone(__import__('zoneinfo').ZoneInfo("America/New_York")).replace(hour=0, minute=0)
    extracted = extract_sdk_daily_snapshot(plan, resolution, context, guard=guard,
        history_factory=lambda *_: SDKHistory(native, "raw", False, False))
    assert extracted.bars == snapshot.bars
