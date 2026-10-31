"""Hand-built raw daily fixtures; no real platform data or economic evidence."""
from datetime import date, datetime, timedelta, timezone
from copy import deepcopy
import hashlib
import json

import pytest

from bbag.contracts import TimeRange
from bbag.s2_access import AccessGuard, AccessPolicy, DataRequest, STANDARD_FIELDS, canonical_policy_sha256
from bbag.s2_data import (
    CorporateAction, DailyBar, LiquidityRule, QualityRule, StandardDataBundle,
    RawDailyInput, action_entitlement, canonical_request_sha256, data_quality, historical_universe, standardize_daily_json, to_s1_panel,
)


DAYS = [date(2020, 1, value) for value in (2, 3, 6, 7, 8, 9, 10)]


def at(day, hour=21, minute=1):
    return datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=hour, minutes=minute)


def request(**changes):
    payload = dict(provider="SYNTHETIC", start=DAYS[0], end=DAYS[-1], assets=["SID_A", "SID_B"], fields=list(STANDARD_FIELDS), as_of=at(DAYS[-1]), classification="DEVELOPMENT")
    payload.update(changes)
    return DataRequest(**payload)


def guard():
    return AccessGuard(AccessPolicy(policy_id="SYNTHETIC_RAW_DAILY", provider="SYNTHETIC", development=TimeRange(start=DAYS[0], end=DAYS[-1]),
        sealed=[TimeRange(start="2020-02-01", end="2020-03-01")], allowed_assets=["SID_A", "SID_B"], allowed_fields=list(STANDARD_FIELDS)))


def payload():
    req = request()
    sessions = [dict(session=day, open_at=at(day, 14, 30), close_at=at(day, 21, 0), signal_at=at(day)) for day in DAYS]
    closes = [10.0, 11.0, 12.0, 6.0, 6.5, 7.0, 7.5]
    bars = [dict(sid=sid, session=day, open=value, high=value + 1.0, low=value - 1.0, close=value,
        volume=100.0, available_at=at(day, 21, 0)) for index, day in enumerate(DAYS) for sid, value in [("SID_A", closes[index]), ("SID_B", 20.0)]
        if sid != "SID_B" or day < date(2020, 1, 9)]
    return dict(data_id="SYNTHETIC_DAILY_ONLY", calendar_id="EXPLICIT_SYNTHETIC_USA_SESSIONS", calendar_version="fixture-v1", request=req,
        provenance=dict(provider="SYNTHETIC", provider_data_version="fixture-v1", source_sha256="a" * 64, request_sha256=canonical_request_sha256(req),
            retrieved_at=at(DAYS[-1], 23, 0), classification="DEVELOPMENT"), sessions=sessions,
        identities=[dict(sid="SID_A", listed_from=DAYS[0], known_at=at(date(2020, 1, 1))),
            dict(sid="SID_B", listed_from=DAYS[0], known_at=at(date(2020, 1, 1)), delisted_on=date(2020, 1, 9), delisting_known_at=at(date(2020, 1, 8)))],
        tickers=[dict(sid="SID_A", ticker="OLD", valid_from=DAYS[0], valid_to=date(2020, 1, 6), known_at=at(date(2020, 1, 1)), valid_to_known_at=at(date(2020, 1, 6))),
            dict(sid="SID_A", ticker="NEW", valid_from=date(2020, 1, 7), known_at=at(date(2020, 1, 6))),
            dict(sid="SID_B", ticker="B", valid_from=DAYS[0], known_at=at(date(2020, 1, 1)))],
        membership=[dict(sid=sid, effective_on=DAYS[0], member=True, known_at=at(date(2020, 1, 1))) for sid in ["SID_A", "SID_B"]],
        corporate_actions=[dict(sid="SID_A", kind="SPLIT", effective_on=date(2020, 1, 7), known_at=at(date(2020, 1, 7), 12, 0), shares_after_per_before=2.0),
            dict(sid="SID_A", kind="DIVIDEND", effective_on=date(2020, 1, 8), known_at=at(date(2020, 1, 9), 12, 0), cash_per_share=1.0)], bars=bars)


def bundle(**changes):
    value = payload()
    value.update(changes)
    return StandardDataBundle.model_validate(value)


def test_explicit_calendar_sid_rename_delisting_and_raw_values():
    data = bundle()
    panel = to_s1_panel(data, request(), guard())
    assert [item.session for item in panel.sessions] == DAYS  # weekend is not synthesized
    assert panel.assets == ["SID_A", "SID_B"]
    assert panel.source == "synthetic_fixture"
    values = {(item.session, item.asset): item.values["close"] for item in panel.observations}
    assert values[(date(2020, 1, 6), "SID_A")] == 12.0
    assert values[(date(2020, 1, 7), "SID_A")] == 6.0  # raw split discontinuity preserved
    assert (date(2020, 1, 9), "SID_B") not in values
    rows = historical_universe(data, request(), guard()).rows
    assert [item.assets for item in rows] == [["SID_A", "SID_B"]] * 5 + [["SID_A"]] * 2


def test_asof_filters_signals_and_late_available_bars():
    raw = payload()
    late = next(item for item in raw["bars"] if item["session"] == DAYS[1] and item["sid"] == "SID_A")
    late["available_at"] = at(DAYS[2], 15, 0)
    data = StandardDataBundle.model_validate(raw)
    req = request(end=DAYS[1], as_of=at(DAYS[1]))
    panel = to_s1_panel(data, req, guard())
    assert len(panel.sessions) == 2
    assert [(item.session, item.asset) for item in panel.observations] == [(DAYS[0], "SID_A"), (DAYS[0], "SID_B"), (DAYS[1], "SID_B")]
    before_signal = request(end=DAYS[1], as_of=at(DAYS[1], 21, 0))
    assert [item.session for item in to_s1_panel(data, before_signal, guard()).sessions] == [DAYS[0]]


def test_future_bar_and_metadata_perturbation_does_not_change_past():
    data = bundle()
    req = request(end=DAYS[2], as_of=at(DAYS[2]))
    raw = payload()
    for bar in raw["bars"]:
        if bar["session"] > DAYS[2]:
            for field in ("open", "high", "low", "close"):
                bar[field] *= 100.0
    raw["membership"].append(dict(sid="SID_A", effective_on=DAYS[1], member=False, known_at=at(DAYS[-1])))
    changed = StandardDataBundle.model_validate(raw)
    assert to_s1_panel(data, req, guard()) == to_s1_panel(changed, req, guard())
    assert historical_universe(data, req, guard()) == historical_universe(changed, req, guard())
    assert data_quality(data, req, guard()) == data_quality(changed, req, guard())


def test_future_effective_membership_and_late_known_removal():
    raw = payload()
    raw["membership"].append(dict(sid="SID_A", effective_on=DAYS[3], member=False, known_at=at(DAYS[4])))
    result = historical_universe(StandardDataBundle.model_validate(raw), request(), guard())
    assert result.rows[2].assets == ["SID_A", "SID_B"]
    assert result.rows[3].assets == ["SID_A", "SID_B"]  # removal was not known at this signal
    assert result.rows[4].assets == ["SID_B"]
    assert result.rows[5].assets == []  # B delisted, A removed


def test_future_rename_end_does_not_silently_remove_past_member():
    raw = payload()
    raw["tickers"][0]["valid_to_known_at"] = at(DAYS[-1])
    raw["tickers"][1]["known_at"] = at(DAYS[-1])
    rows = historical_universe(StandardDataBundle.model_validate(raw), request(), guard()).rows
    assert rows[3].assets == ["SID_A", "SID_B"]
    assert rows[4].assets == ["SID_A", "SID_B"]


def test_listing_knowledge_and_no_implicit_default_membership():
    raw = payload()
    raw["identities"][0]["known_at"] = at(DAYS[2])
    raw["membership"] = [item for item in raw["membership"] if item["sid"] == "SID_A"]
    rows = historical_universe(StandardDataBundle.model_validate(raw), request(), guard()).rows
    assert rows[0].assets == []
    assert rows[2].assets == ["SID_A"]


def test_lagged_liquidity_hand_calculation_and_deterministic_ties():
    data = bundle()
    rule = LiquidityRule(lookback_sessions=2, minimum_mean_raw_dollar_volume=1000.0, max_assets=1)
    rows = historical_universe(data, request(), guard(), liquidity=rule).rows
    assert rows[0].assets == rows[1].assets == []
    assert rows[2].assets == ["SID_B"]  # preceding A=1050; B=2000
    raw = payload()
    for bar in raw["bars"]:
        if bar["session"] < DAYS[2]:
            bar.update(open=20.0, high=21.0, low=19.0, close=20.0)
        elif bar["session"] == DAYS[2] and bar["sid"] == "SID_A":
            bar.update(open=1e9, high=1e9, low=1e9, close=1e9)
    tied = historical_universe(StandardDataBundle.model_validate(raw), request(), guard(), liquidity=rule)
    assert tied.rows[2].assets == ["SID_A"]  # tie broken by SID, huge same-day value irrelevant


def test_liquidity_full_window_missing_or_late_data_excludes_asset():
    raw = payload()
    raw["bars"] = [item for item in raw["bars"] if not (item["session"] == DAYS[0] and item["sid"] == "SID_B")]
    a = next(item for item in raw["bars"] if item["session"] == DAYS[1] and item["sid"] == "SID_A")
    a["available_at"] = at(DAYS[3])
    rows = historical_universe(StandardDataBundle.model_validate(raw), request(), guard(), liquidity=LiquidityRule()).rows
    assert rows[2].assets == []
    assert rows[3].assets == ["SID_B", "SID_A"]  # earlier delayed value is known now


def test_quality_expected_coverage_missing_stale_zero_and_raw_action_jump():
    raw = payload()
    raw["bars"] = [item for item in raw["bars"] if not (item["sid"] == "SID_A" and item["session"] in DAYS[:2])]
    next(item for item in raw["bars"] if item["sid"] == "SID_A" and item["session"] == DAYS[4])["volume"] = 0.0
    report = data_quality(StandardDataBundle.model_validate(raw), request(), guard(), rule=QualityRule(maximum_absolute_raw_close_change=0.4))
    assert report.expected_bars == 12  # 7 A + 5 B; delisting is not missing coverage
    assert report.available_bars == report.complete_bars == 10
    assert report.coverage == pytest.approx(10 / 12)
    rows = {(item.session, item.sid): item for item in report.rows}
    assert rows[(DAYS[0], "SID_A")].missing_fields == list(STANDARD_FIELDS)
    assert rows[(DAYS[1], "SID_A")].stale
    assert rows[(DAYS[3], "SID_A")].raw_close_jump
    assert rows[(DAYS[3], "SID_A")].known_action_on_session
    assert rows[(DAYS[4], "SID_A")].zero_volume
    assert not rows[(DAYS[4], "SID_A")].known_action_on_session  # dividend not known then


def test_quality_staleness_uses_session_distance_not_calendar_days():
    raw = payload()
    raw["bars"] = [item for item in raw["bars"] if not (item["sid"] == "SID_A" and item["session"] in DAYS[2:4])]
    rows = data_quality(StandardDataBundle.model_validate(raw), request(), guard()).rows
    a = {item.session: item for item in rows if item.sid == "SID_A"}
    assert not a[DAYS[2]].stale  # Fri to Mon is one calendar session
    assert a[DAYS[3]].stale


def test_single_corporate_action_entitlements_without_adjusting_prices():
    data = bundle()
    split, dividend = data.corporate_actions
    assert action_entitlement(split, 5.0, as_of=at(DAYS[3])).model_dump() == {"shares_after": 10.0, "cash_distribution": 0.0}
    assert action_entitlement(dividend, 10.0, as_of=at(DAYS[5])).model_dump() == {"shares_after": 10.0, "cash_distribution": 10.0}
    for action, decision in [(split, at(DAYS[2])), (dividend, at(DAYS[4]))]:
        with pytest.raises(ValueError, match="not yet known and effective"):
            action_entitlement(action, 5.0, as_of=decision)


@pytest.mark.parametrize("changes", [{"open": -1.0}, {"volume": -1.0}, {"close": float("nan")}, {"high": 5.0}, {"low": 20.0}, {"open": "10.0"}, {"adjusted_close": 10.0}])
def test_raw_bar_schema_rejects_invalid_fields_ranges_and_nonfinite(changes):
    value = payload()["bars"][0]
    value.update(changes)
    with pytest.raises(ValueError):
        DailyBar.model_validate(value)


@pytest.mark.parametrize("mutate", [
    lambda raw: raw["bars"].append(deepcopy(raw["bars"][0])),
    lambda raw: raw["bars"][0].update(available_at=at(DAYS[0], 20, 59)),
    lambda raw: raw["bars"][0].update(available_at=at(DAYS[-1], 23, 1)),
    lambda raw: raw["bars"][0].update(sid="UNREQUESTED"),
    lambda raw: raw["bars"][0].update(session=date(2020, 1, 4)),
    lambda raw: raw["provenance"].update(request_sha256="b" * 64),
    lambda raw: raw["provenance"].update(normalization="adjusted"),
    lambda raw: raw["sessions"][0].update(open_at=at(DAYS[0], 23, 30), close_at=at(DAYS[1], 1, 0), signal_at=at(DAYS[1], 1, 1)),
    lambda raw: raw["sessions"].reverse(),
    lambda raw: raw["tickers"][0].update(valid_to=date(2020, 1, 7)),
    lambda raw: raw["tickers"][0].update(valid_to_known_at=None),
    lambda raw: raw["identities"][1].update(delisted_on=DAYS[1]),
])
def test_bundle_rejects_identity_calendar_availability_and_provenance_inconsistency(mutate):
    raw = payload()
    mutate(raw)
    with pytest.raises(ValueError):
        StandardDataBundle.model_validate(raw)


@pytest.mark.parametrize("function", [to_s1_panel, historical_universe, data_quality])
def test_public_data_operations_guard_before_touching_invalid_payload(function):
    denied = request(start="2020-02-01", end="2020-02-02", as_of=at(date(2020, 2, 3)))
    with pytest.raises(ValueError, match="development interval"):
        function(object(), denied, guard())


@pytest.mark.parametrize("function", [to_s1_panel, historical_universe, data_quality])
def test_artifact_taint_and_mutated_typed_inputs_are_revalidated(function):
    data = bundle()
    data.provenance.derived_from.append("SEALED_FINAL")
    with pytest.raises(ValueError):
        function(data, request(), guard())
    req = request()
    req.assets.append("UNAUTHORIZED")
    with pytest.raises(ValueError):
        function(bundle(), req, guard())


def test_cloud_raw_bundle_does_not_project_into_synthetic_s1_even_in_cloud():
    raw = payload()
    req = request(provider="QUANTCONNECT_CLOUD", output_mode="IN_CLOUD_DATA")
    raw["request"] = req
    raw["provenance"].update(provider="QUANTCONNECT_CLOUD", request_sha256=canonical_request_sha256(req))
    data = StandardDataBundle.model_validate(raw)
    p = AccessPolicy(policy_id="FAKE_CLOUD_SCOPE", provider="QUANTCONNECT_CLOUD", authorization="OWNER_AUTHORIZED", development=TimeRange(start=DAYS[0], end=DAYS[-1]),
        allowed_assets=req.assets, allowed_fields=req.fields, approval_reference="synthetic-only", approved_by="fixture", approved_at=at(DAYS[-1]))
    registry = frozenset({canonical_policy_sha256(p)})
    with pytest.raises(ValueError):
        data_quality(data, req, AccessGuard(p, trusted_policy_sha256s=registry))
    cloud = AccessGuard(p, trusted_policy_sha256s=registry, execution_location="QUANTCONNECT_CLOUD")
    with pytest.raises(ValueError, match="synthetic fixtures only"):
        to_s1_panel(data, req, cloud)
    assert data_quality(data, req, cloud).expected_bars == 12  # simulated cloud-local pure function only


def test_rule_or_api_unknown_types_fail_closed():
    for rule in [{"lookback_sessions": 0}, {"lag_sessions": True}, {"max_assets": 33}, {"missing": "skip"}]:
        with pytest.raises(ValueError):
            LiquidityRule.model_validate(rule)
    for function, kwargs in [(historical_universe, {"liquidity": {}}), (data_quality, {"rule": {}})]:
        with pytest.raises(ValueError):
            function(bundle(), request(), guard(), **kwargs)
    with pytest.raises(ValueError):
        to_s1_panel(bundle(), request(), object())
    with pytest.raises(ValueError):
        to_s1_panel({}, request(), guard())


@pytest.mark.parametrize("changes", [dict(kind="SPLIT", shares_after_per_before=0.0), dict(kind="DIVIDEND", cash_per_share=-1.0), dict(kind="DIVIDEND", shares_after_per_before=2.0, cash_per_share=1.0)])
def test_corporate_action_metadata_rejects_ambiguous_math(changes):
    value = dict(sid="SID_A", effective_on=DAYS[0], known_at=at(DAYS[0]))
    value.update(changes)
    with pytest.raises(ValueError):
        CorporateAction.model_validate(value)


def test_price_volume_missingness_and_liquidity_overflow_are_explicit():
    raw = payload()
    for bar in raw["bars"]:
        if bar["sid"] == "SID_A":
            bar.update(open=1e308, high=1e308, low=1e308, close=1e308, volume=1e308)
    data = StandardDataBundle.model_validate(raw)
    assert historical_universe(data, request(), guard(), liquidity=LiquidityRule()).rows[2].assets == ["SID_B"]
    raw["bars"][0]["close"] = None
    report = data_quality(StandardDataBundle.model_validate(raw), request(), guard())
    assert report.rows[0].missing_fields == ["close"]


def raw_bytes():
    raw = payload()
    envelope = RawDailyInput.model_validate({key: raw[key] for key in ("sessions", "identities", "tickers", "membership", "corporate_actions", "bars")})
    return envelope.model_dump_json().encode("utf-8")


def normalize(source, **changes):
    args = dict(request=request(), guard=guard(), data_id="SYNTHETIC_BYTES_ONLY", provider_data_version="fixture-v1", retrieved_at=at(DAYS[-1], 23, 0),
        calendar_id="EXPLICIT_SYNTHETIC_USA_SESSIONS", calendar_version="fixture-v1")
    args.update(changes)
    return standardize_daily_json(source, **args)


def test_normalization_exact_source_bytes_hash_and_raw_field_traceability():
    source = raw_bytes()
    data = normalize(source)
    assert data.provenance.source_sha256 == hashlib.sha256(source).hexdigest()
    assert data.provenance.request_sha256 == canonical_request_sha256(request())
    assert [item.model_dump(mode="json") for item in data.bars] == json.loads(source)["bars"]
    assert data.bars[6].close == 6.0  # raw split discontinuity is not repaired
    spaced = json.dumps(json.loads(source), indent=2).encode("utf-8")
    assert normalize(spaced).bars == data.bars
    assert normalize(spaced).provenance.source_sha256 != data.provenance.source_sha256


@pytest.mark.parametrize("source", [b'{"bars":[],"bars":[]}', b'{"bars":[{"close":1,"close":2}]}', b'{"bars":[{"close":NaN}]}', b'{"bars":[{"close":1e999}]}', b'[]', b'\xff', b'{}', b'{"unknown":1}'])
def test_normalizer_rejects_duplicate_nonfinite_or_invalid_input(source):
    with pytest.raises(ValueError):
        normalize(source)


def test_normalizer_authorizes_before_decoding_or_hashing_and_has_no_cloud_local_path():
    req = request(classification="SEALED_FINAL")
    with pytest.raises(ValueError, match="sealed"):
        normalize(b'not JSON', request=req)


def test_quality_freshness_uses_requested_fields_instead_of_unrequested_close():
    raw = payload()
    req = request(fields=["volume"])
    raw["request"] = req
    raw["provenance"]["request_sha256"] = canonical_request_sha256(req)
    for bar in raw["bars"]:
        for field in ("open", "high", "low", "close"):
            bar[field] = None
    report = data_quality(StandardDataBundle.model_validate(raw), req, guard())
    assert report.coverage == 1.0
    assert not any(item.stale for item in report.rows)
    with pytest.raises(ValueError):
        QualityRule(raw_change_is_economic_signal=0)
