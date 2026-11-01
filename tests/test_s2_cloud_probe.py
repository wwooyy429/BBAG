"""Synthetic SDK doubles only; these tests are not platform execution/PASS."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from bbag.s2_access import AccessPolicy, DataRequest, STANDARD_FIELDS, canonical_policy_sha256
from bbag.s2_quantconnect import ALL_CHECKS, QuantConnectCheckRequest, QuantConnectCheckResponse, qc_request_sha256


SPEC = importlib.util.spec_from_file_location("synthetic_cloud_probe", Path(__file__).resolve().parents[1] / "cloud/s2_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def inputs() -> tuple[dict, dict, str]:
    policy = AccessPolicy(policy_id="synthetic-unit-grant-never-real-approval", provider="QUANTCONNECT_CLOUD",
        authorization="OWNER_AUTHORIZED", development={"start": date(2020, 1, 2), "end": date(2020, 1, 10)},
        allowed_assets=["SPY", "AAPL"], allowed_fields=list(STANDARD_FIELDS), timezone="America/New_York",
        approval_reference="SYNTHETIC_TEST_ONLY", approved_by="Synthetic unit-test owner",
        approved_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    plan = QuantConnectCheckRequest(project_id=123, source_commit="a" * 40, config_sha256="b" * 64,
        data=DataRequest(provider="QUANTCONNECT_CLOUD", start=date(2020, 1, 2), end=date(2020, 1, 10),
            assets=["SPY", "AAPL"], fields=list(STANDARD_FIELDS), classification="DEVELOPMENT",
            output_mode="CONTROL_ONLY", as_of=datetime(2020, 1, 10, 22, tzinfo=timezone.utc)), checks=list(ALL_CHECKS))
    return plan.model_dump(mode="json"), policy.model_dump(mode="json"), canonical_policy_sha256(policy)


class FakeSymbol:
    def __init__(self, ticker: str):
        self.id = "SYNTHETIC_SECRET_SID_" + ticker


class FakeHours:
    def get_next_market_open(self, when, extended):
        assert extended is False
        while when.weekday() >= 5:
            when += timedelta(days=1)
        return when.replace(hour=9, minute=30)

    def get_next_market_close(self, when, extended):
        assert extended is False
        return when.replace(hour=16, minute=0)


class FakeHistory:
    def __init__(self, owner):
        self.owner = owner

    def __getitem__(self, bar_type):
        self.owner.events.append(("history_type", bar_type))
        return self

    def __call__(self, symbols, start, end, resolution, **kwargs):
        self.owner.events.append(("history", symbols, start, end, resolution, kwargs))
        if self.owner.error is not None:
            raise self.owner.error
        packets = []
        day = start
        # QuantBook's start date is the latest accessible history date, not the
        # beginning of the history query. Model this platform behavior explicitly.
        ceiling = min(end, self.owner.research_clock)
        while day < ceiling:
            if day.weekday() < 5:
                packets.append({symbol.id: SimpleNamespace(symbol=symbol,
                    time=day.replace(hour=9, minute=30), end_time=day.replace(hour=16),
                    open=12345.25, high=12346.0, low=12344.0, close=12345.5, volume=777.0,
                    is_fill_forward=False) for symbol in symbols})
            day += timedelta(days=1)
        if self.owner.change is not None:
            self.owner.change(packets)
        return packets


class FakeBook:
    def __init__(self):
        self.events = []
        self.settings = SimpleNamespace(daily_precise_end_time=False)
        self.history = FakeHistory(self)
        self.error = None
        self.change = None
        self.raw = "RAW"
        self.research_clock = datetime.max
        self.research_end = datetime.max

    def set_time_zone(self, zone):
        self.events.append(("timezone", zone))

    def set_start_date(self, *args):
        self.research_clock = datetime(*args)
        self.events.append(("start", args))

    def set_end_date(self, *args):
        self.research_end = datetime(*args)
        self.events.append(("end", args))

    def add_equity(self, ticker, resolution, **kwargs):
        assert any(event[0] == "start" for event in self.events)
        self.events.append(("add", ticker, resolution, kwargs))
        return SimpleNamespace(symbol=FakeSymbol(ticker), subscriptions=[SimpleNamespace(data_normalization_mode=self.raw)],
                               exchange=SimpleNamespace(hours=FakeHours()))


@pytest.fixture
def sdk(monkeypatch):
    book = FakeBook()
    factories = []
    def factory():
        factories.append("SYNTHETIC_SDK_FACTORY")
        return book
    def fake_sdk():
        return factory, SimpleNamespace(DAILY="DAILY"), SimpleNamespace(RAW="RAW"), SimpleNamespace(USA="USA"), object
    monkeypatch.setattr(probe, "_sdk", fake_sdk)
    return book, factories


def assert_no_market_output(result: dict, capsys) -> None:
    response = QuantConnectCheckResponse.model_validate_json(json.dumps(result))
    assert set(result) == set(response.model_dump())
    encoded = json.dumps(result)
    assert "SYNTHETIC_SECRET_SID" not in encoded and "12345" not in encoded and "777.0" not in encoded
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_synthetic_sdk_uses_exact_date_range_raw_daily_no_fill_and_only_emits_verdicts(sdk, capsys) -> None:
    book, factories = sdk
    plan, policy, digest = inputs()
    result = probe.run_cloud_check(plan, policy, digest)
    assert result["status"] == "COMPLETED" and result["check_results"] == dict.fromkeys(ALL_CHECKS, True)
    assert factories == ["SYNTHETIC_SDK_FACTORY"]
    assert book.settings.daily_precise_end_time is True
    history = next(event for event in book.events if event[0] == "history")
    assert history[2:5] == (datetime(2020, 1, 2), datetime(2020, 1, 11), "DAILY")
    assert history[5] == {"fill_forward": False, "extended_market_hours": False, "data_normalization_mode": "RAW"}
    adds = [event for event in book.events if event[0] == "add"]
    assert [event[1] for event in adds] == ["SPY", "AAPL"]
    assert all(event[3] == {"market": "USA", "fill_forward": False, "extended_market_hours": False,
                         "data_normalization_mode": "RAW"} for event in adds)
    assert result["request_sha256"] == qc_request_sha256(QuantConnectCheckRequest.model_validate_json(json.dumps(plan)))
    assert plan["data"]["output_mode"] == "CONTROL_ONLY"
    assert_no_market_output(result, capsys)


def test_quantbook_research_clock_is_history_ceiling_not_query_start(sdk) -> None:
    book, _ = sdk
    result = probe.run_cloud_check(*inputs())
    # The old Jan 2 midnight clock clips the authorized Jan 2..10 request to
    # nothing. Jan 11 midnight makes Jan 10 available without widening history.
    assert result["check_results"]["data_presence"] is True
    assert book.research_clock == datetime(2020, 1, 11)
    assert book.research_end == datetime(2020, 1, 12)
    history = next(event for event in book.events if event[0] == "history")
    assert history[2:4] == (datetime(2020, 1, 2), datetime(2020, 1, 11))
    starts = [index for index, event in enumerate(book.events) if event[0] == "start"]
    adds = [index for index, event in enumerate(book.events) if event[0] == "add"]
    assert max(starts) < min(adds)


@pytest.mark.parametrize("change", [
    {"start": "2020-01-01"}, {"end": "2020-01-11"}, {"assets": ["NVDA"]},
    {"provider": "SYNTHETIC"}, {"fields": ["close"]}, {"classification": "UNKNOWN"},
    {"classification": "SEALED_FINAL"}, {"derived_from": ["SEALED_DERIVATIVE"]},
])
def test_validly_shaped_but_unapproved_request_is_denied_before_sdk_factory(sdk, capsys, change: dict) -> None:
    book, factories = sdk
    plan, policy, digest = inputs()
    plan["data"].update(change)
    result = probe.run_cloud_check(plan, policy, digest)
    assert result["status"] == "ERROR" and result["error_category"] == "ACCESS_DENIED"
    assert result["check_results"] == {} and factories == [] and book.events == []
    assert_no_market_output(result, capsys)


@pytest.mark.parametrize("digest", ["0" * 64, "A" * 64, "not-a-digest", None, True])
def test_untrusted_policy_hash_is_denied_without_provider_io(sdk, capsys, digest) -> None:
    book, factories = sdk
    plan, policy, _ = inputs()
    result = probe.run_cloud_check(plan, policy, digest)
    assert result["error_category"] == "ACCESS_DENIED" and factories == [] and book.events == []
    assert_no_market_output(result, capsys)


def test_even_a_self_consistent_trusted_policy_cannot_expand_this_probe_current_owner_scope(sdk, capsys) -> None:
    _, factories = sdk
    plan, policy, _ = inputs()
    policy["development"]["end"] = "2020-01-31"
    digest = canonical_policy_sha256(AccessPolicy.model_validate_json(json.dumps(policy)))
    result = probe.run_cloud_check(plan, policy, digest)
    assert result["error_category"] == "ACCESS_DENIED" and factories == []
    assert_no_market_output(result, capsys)


@pytest.mark.parametrize("mutation", ["extra_market_data", "raw_output", "duplicate_fields", "naive_cutoff", "bad_source", "uppercase_source"])
def test_unbound_invalid_plan_has_only_closed_exception_and_no_fabricated_identifiers(sdk, capsys, mutation: str) -> None:
    _, factories = sdk
    plan, policy, digest = inputs()
    if mutation == "extra_market_data": plan["bars"] = [{"price": "SECRET 12345"}]
    elif mutation == "raw_output": plan["data"]["output_mode"] = "LOCAL_DATA"
    elif mutation == "duplicate_fields": plan["data"]["fields"].append("close")
    elif mutation == "naive_cutoff": plan["data"]["as_of"] = "2020-01-10T22:00:00"
    elif mutation == "bad_source": plan["source_commit"] = "SECRET_SOURCE_VALUE"
    elif mutation == "uppercase_source": plan["source_commit"] = "A" * 40
    with pytest.raises(probe.CloudProbeRejected) as rejected:
        probe.run_cloud_check(plan, policy, digest)
    assert str(rejected.value) == "INVALID_REQUEST" and factories == []
    assert capsys.readouterr().out == ""


def test_mutating_original_plan_during_factory_cannot_change_the_validated_query(monkeypatch, sdk) -> None:
    book, _ = sdk
    plan, policy, digest = inputs()
    original_sdk = probe._sdk
    def mutating_sdk():
        plan["data"]["end"] = "2020-01-31"
        return original_sdk()
    monkeypatch.setattr(probe, "_sdk", mutating_sdk)
    result = probe.run_cloud_check(plan, policy, digest)
    assert result["status"] == "COMPLETED"
    assert next(event for event in book.events if event[0] == "history")[3] == datetime(2020, 1, 11)


def test_unsolicited_python_sdk_output_is_discarded_inside_cloud(monkeypatch, sdk, capsys) -> None:
    original_sdk = probe._sdk
    def noisy_sdk():
        print("SYNTHETIC_SECRET_SID 12345.25")
        print("SECRET PROVIDER DIAGNOSTIC", file=sys.stderr)
        return original_sdk()
    monkeypatch.setattr(probe, "_sdk", noisy_sdk)
    result = probe.run_cloud_check(*inputs())
    assert result["status"] == "COMPLETED"
    assert_no_market_output(result, capsys)


@pytest.mark.parametrize("bridge_shape", ["dotnet_values", "dotnet_pairs"])
def test_typed_multi_symbol_history_handles_readable_dotnet_collection_shapes(sdk, bridge_shape: str) -> None:
    book, _ = sdk
    class Pairs:
        def __init__(self, packet):
            self.packet = packet
        def __iter__(self):
            return iter(SimpleNamespace(Value=bar) for bar in self.packet.values())
    def change(packets):
        for index, packet in enumerate(packets):
            packets[index] = (SimpleNamespace(Values=list(packet.values()))
                              if bridge_shape == "dotnet_values" else Pairs(packet))
    book.change = change
    result = probe.run_cloud_check(*inputs())
    assert result["status"] == "COMPLETED" and all(result["check_results"].values())


@pytest.mark.parametrize("fault,flag", [
    ("bad_close", "point_in_time"), ("forward_filled", "point_in_time"),
    ("duplicate", "point_in_time"), ("outside_date", "requested_bounds"),
    ("unknown_sid", "identity_consistency"), ("nonfinite_price", "field_availability"),
    ("negative_volume", "field_availability"), ("missing_asset", "data_presence"),
])
def test_bad_actual_observation_cannot_become_a_true_verdict_or_leave_cloud(sdk, capsys, fault: str, flag: str) -> None:
    book, _ = sdk
    plan, policy, digest = inputs()
    def change(packets):
        first = next(iter(packets[0].values()))
        if fault == "bad_close": first.end_time = first.end_time.replace(hour=17)
        elif fault == "forward_filled": first.is_fill_forward = True
        elif fault == "duplicate": packets.insert(1, deepcopy(packets[0]))
        elif fault == "outside_date": first.time = first.time.replace(day=1); first.end_time = first.end_time.replace(day=1)
        elif fault == "unknown_sid": first.symbol.id = "SYNTHETIC_SECRET_SID_UNREQUESTED"
        elif fault == "nonfinite_price": first.close = float("inf")
        elif fault == "negative_volume": first.volume = -777.0
        elif fault == "missing_asset":
            for packet in packets:
                for key in list(packet):
                    if key.endswith("AAPL"): del packet[key]
    book.change = change
    result = probe.run_cloud_check(plan, policy, digest)
    assert result["status"] == "COMPLETED" and result["check_results"][flag] is False
    assert result["check_results"]["no_data_export"] is True
    assert_no_market_output(result, capsys)


def test_declared_cutoff_before_last_close_does_not_claim_point_in_time_success(sdk) -> None:
    plan, policy, digest = inputs()
    plan["data"]["as_of"] = "2020-01-10T19:00:00+00:00"
    result = probe.run_cloud_check(plan, policy, digest)
    assert result["status"] == "COMPLETED" and result["check_results"]["point_in_time"] is False


def test_adjusted_subscription_fails_normalization_check_even_when_raw_was_requested(sdk) -> None:
    book, _ = sdk
    book.raw = "ADJUSTED"
    result = probe.run_cloud_check(*inputs())
    assert result["check_results"]["normalization_raw"] is False


def test_row_limit_stops_processing_with_bounded_error_and_no_partial_truth_claim(sdk, capsys) -> None:
    plan, policy, digest = inputs()
    plan["max_history_rows"] = 1
    result = probe.run_cloud_check(plan, policy, digest)
    assert result["status"] == "ERROR" and result["error_category"] == "PROVIDER_ERROR" and result["check_results"] == {}
    assert_no_market_output(result, capsys)


@pytest.mark.parametrize("error,category", [(RuntimeError("SECRET_SID PRICE 12345"), "PROVIDER_ERROR"), (TimeoutError("SECRET PRICE"), "TIMEOUT")])
def test_provider_exception_is_sanitized_without_printing_or_returning_it(sdk, capsys, error, category) -> None:
    book, _ = sdk
    book.error = error
    result = probe.run_cloud_check(*inputs())
    assert result["status"] == "ERROR" and result["error_category"] == category
    assert_no_market_output(result, capsys)


def test_missing_platform_sdk_is_capability_unavailable_and_never_platform_pass(monkeypatch, capsys) -> None:
    def unavailable():
        raise ModuleNotFoundError("SECRET PROVIDER DETAILS")
    monkeypatch.setattr(probe, "_sdk", unavailable)
    result = probe.run_cloud_check(*inputs())
    assert result["status"] == "ERROR" and result["error_category"] == "CAPABILITY_UNAVAILABLE"
    assert_no_market_output(result, capsys)
