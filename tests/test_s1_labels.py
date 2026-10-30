"""Independent arithmetic and timing expectations for synthetic S1 labels."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

from pydantic import ValidationError
import pytest

from bbag.labels import compute_labels
from bbag.s1_contracts import LabelRule, MarketPanel


START = date(2026, 1, 5)


def moment(index: int, hour: int, minute: int = 0) -> datetime:
    day = START + timedelta(days=index)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def panel_payload() -> dict:
    prices = {"A": [80.0, 100.0, 110.0, 121.0, 133.1],
              "B": [10.0, 50.0, 55.0, 60.0, 75.0],
              "BENCH": [190.0, 200.0, 204.0, 208.08, 212.2416]}
    return {
        "data_id": "independent-synthetic-label-fixture",
        "source": "synthetic_fixture",
        "timezone": "UTC",
        "sessions": [dict(session=START + timedelta(days=i), open_at=moment(i, 9),
                          close_at=moment(i, 16), signal_at=moment(i, 16, 5)) for i in range(5)],
        "assets": ["A", "B", "BENCH"],
        "fields": ["open", "close"],
        "observations": [dict(session=START + timedelta(days=i), asset=asset,
                              available_at=moment(i, 16), values={"open": value, "close": value + 1.0})
                         for asset, values in prices.items() for i, value in enumerate(values)],
    }


def make_panel() -> MarketPanel:
    return MarketPanel.model_validate(panel_payload())


def target(result, index: int = 0, asset: str = "A"):
    return next(row for row in result.rows if row.session == START + timedelta(days=index) and row.asset == asset)


def test_next_open_holding_period_and_independent_price_answer() -> None:
    result = compute_labels(make_panel(), LabelRule(horizon_sessions=1), ["A"], moment(4, 17))
    first = target(result)
    # Signal Monday evening, entry Tuesday 100, exit Wednesday 110: +10%.
    assert first.entry_at == moment(1, 9)
    assert first.exit_at == moment(2, 9)
    assert first.mature_at == moment(2, 16)
    assert first.status == "MATURED"
    assert first.value == pytest.approx(0.10, abs=1e-12)
    assert [row.status for row in result.rows] == ["MATURED"] * 3 + ["INSUFFICIENT_FUTURE"] * 2
    assert target(result, 3).entry_at == moment(4, 9)
    assert target(result, 3).exit_at is None
    assert target(result, 4).entry_at is None


def test_excess_return_subtracts_same_period_benchmark() -> None:
    rule = LabelRule(horizon_sessions=1, return_type="excess_price_return", benchmark_asset="BENCH")
    result = compute_labels(make_panel(), rule, ["A"], moment(4, 17))
    # Stock 100->110 is +10%; benchmark 200->204 is +2%.
    assert target(result).value == pytest.approx(0.08, abs=1e-12)
    assert target(result).entry_at == moment(1, 9)
    assert target(result).exit_at == moment(2, 9)


def test_horizon_two_uses_overlapping_open_to_open_intervals() -> None:
    result = compute_labels(make_panel(), LabelRule(horizon_sessions=2), ["A"], moment(4, 17))
    first, second = target(result), target(result, 1)
    assert (first.entry_at, first.exit_at) == (moment(1, 9), moment(3, 9))
    assert (second.entry_at, second.exit_at) == (moment(2, 9), moment(4, 9))
    assert first.entry_at < second.entry_at < first.exit_at < second.exit_at
    # 100->121 and 110->133.1 both give +21%, independently of rebalance.
    assert first.value == pytest.approx(0.21, abs=1e-12)
    assert second.value == pytest.approx(0.21, abs=1e-12)
    assert sum(row.status == "MATURED" for row in result.rows) == 2


def test_future_prices_stay_unavailable_until_exact_maturity() -> None:
    before = compute_labels(make_panel(), LabelRule(horizon_sessions=1), ["A"], moment(2, 15, 59))
    exact = compute_labels(make_panel(), LabelRule(horizon_sessions=1), ["A"], moment(2, 16))
    assert target(before).status == "NOT_MATURED"
    assert target(before).value is None
    assert target(before).mature_at == moment(2, 16)
    assert target(exact).status == "MATURED"
    assert target(exact).value == pytest.approx(0.10)


def test_late_entry_and_benchmark_availability_postpone_maturity() -> None:
    payload = panel_payload()
    for row in payload["observations"]:
        if row["session"] == START + timedelta(days=1) and row["asset"] == "A":
            row["available_at"] = moment(3, 18)
        if row["session"] == START + timedelta(days=2) and row["asset"] == "BENCH":
            row["available_at"] = moment(4, 18)
    panel = MarketPanel.model_validate(payload)
    price = compute_labels(panel, LabelRule(horizon_sessions=1), ["A"], moment(3, 17))
    assert target(price).mature_at == moment(3, 18)
    assert target(price).status == "NOT_MATURED"
    excess_rule = LabelRule(horizon_sessions=1, return_type="excess_price_return", benchmark_asset="BENCH")
    excess = compute_labels(panel, excess_rule, ["A"], moment(4, 17))
    assert target(excess).mature_at == moment(4, 18)
    assert target(excess).value is None
    mature = compute_labels(panel, excess_rule, ["A"], moment(4, 18))
    assert target(mature).value == pytest.approx(0.08)


def test_equivalent_timezone_cutoffs_and_stable_output_order() -> None:
    cutoff = moment(4, 17)
    first = compute_labels(make_panel(), LabelRule(horizon_sessions=1), ["B", "A"], cutoff)
    second = compute_labels(make_panel(), LabelRule(horizon_sessions=1), ["A", "B"], cutoff.astimezone(timezone(timedelta(hours=8))))
    assert first.model_dump() == second.model_dump()
    assert [(row.session, row.asset) for row in first.rows] == sorted((row.session, row.asset) for row in first.rows)


def test_before_first_signal_has_no_formed_targets() -> None:
    result = compute_labels(make_panel(), LabelRule(horizon_sessions=1), ["A"], moment(0, 16))
    assert result.rows == []


def test_exact_signal_boundary_includes_only_formed_targets() -> None:
    result = compute_labels(make_panel(), LabelRule(horizon_sessions=1), ["A"], moment(0, 16, 5))
    assert len(result.rows) == 1
    assert result.rows[0].status == "NOT_MATURED"
    assert result.rows[0].value is None


@pytest.mark.parametrize("missing_kind", ["absent_observation", "null_price", "zero_price", "negative_price"])
def test_missing_and_invalid_future_input_never_leak_before_maturity(missing_kind: str) -> None:
    payload = panel_payload()
    index = next(i for i, row in enumerate(payload["observations"])
                 if row["asset"] == "A" and row["session"] == START + timedelta(days=1))
    if missing_kind == "absent_observation":
        payload["observations"].pop(index)
    else:
        payload["observations"][index]["values"]["open"] = {"null_price": None, "zero_price": 0.0, "negative_price": -1.0}[missing_kind]
    panel = MarketPanel.model_validate(payload)
    before = compute_labels(panel, LabelRule(horizon_sessions=1), ["A"], moment(0, 17))
    after = compute_labels(panel, LabelRule(horizon_sessions=1), ["A"], moment(4, 17))
    assert target(before).status == "NOT_MATURED"
    assert target(before).value is None
    assert target(after).status == "MISSING_INPUT"
    assert target(after).value is None


def test_finite_inputs_with_overflow_return_missing() -> None:
    payload = panel_payload()
    for row in payload["observations"]:
        if row["asset"] == "A" and row["session"] == START + timedelta(days=1):
            row["values"]["open"] = 1e-300
        if row["asset"] == "A" and row["session"] == START + timedelta(days=2):
            row["values"]["open"] = 1e300
    result = compute_labels(MarketPanel.model_validate(payload), LabelRule(horizon_sessions=1), ["A"], moment(4, 17))
    assert target(result).status == "MISSING_INPUT"
    assert target(result).value is None


def test_missing_quote_does_not_preempt_a_known_later_availability() -> None:
    payload = panel_payload()
    payload["observations"] = [row for row in payload["observations"]
                               if not (row["asset"] == "A" and row["session"] == START + timedelta(days=1))]
    result = compute_labels(MarketPanel.model_validate(payload), LabelRule(horizon_sessions=1), ["A"], moment(2, 10))
    # The scheduled exit has occurred, but its common-bar input is not published
    # until 16:00. Do not classify the future label's missing data at 10:00.
    assert target(result).status == "NOT_MATURED"
    assert target(result).value is None


def test_all_required_quotes_absent_remains_explicit_missing_after_exit() -> None:
    payload = panel_payload()
    payload["observations"] = [row for row in payload["observations"] if row["asset"] != "A"]
    result = compute_labels(MarketPanel.model_validate(payload), LabelRule(horizon_sessions=1), ["A"], moment(4, 17))
    assert target(result).status == "MISSING_INPUT"
    assert target(result).mature_at is None
    assert target(result).value is None


def test_future_price_changes_may_change_mature_labels() -> None:
    before = make_panel()
    changed = panel_payload()
    for row in changed["observations"]:
        if row["asset"] == "A" and row["session"] == START + timedelta(days=2):
            row["values"]["open"] = 130.0
    original = compute_labels(before, LabelRule(horizon_sessions=1), ["A"], moment(4, 17))
    revised = compute_labels(MarketPanel.model_validate(changed), LabelRule(horizon_sessions=1), ["A"], moment(4, 17))
    assert target(original).value == pytest.approx(0.10)
    assert target(revised).value == pytest.approx(0.30)


@pytest.mark.parametrize("universe", [[], ["A", "A"], ["UNKNOWN"], [True], "A"])
def test_invalid_target_universe_rejected(universe) -> None:
    with pytest.raises(ValueError):
        compute_labels(make_panel(), LabelRule(horizon_sessions=1), universe, moment(4, 17))


@pytest.mark.parametrize("cutoff", [datetime(2026, 1, 9, 17), "2026-01-09T17:00:00+00:00", START, True])
def test_cutoff_requires_an_aware_datetime(cutoff) -> None:
    with pytest.raises(ValueError):
        compute_labels(make_panel(), LabelRule(horizon_sessions=1), ["A"], cutoff)


def test_unknown_label_field_and_benchmark_are_configuration_errors() -> None:
    with pytest.raises(ValueError, match="price_field"):
        compute_labels(make_panel(), LabelRule(horizon_sessions=1, price_field="absent"), ["A"], moment(4, 17))
    unknown = LabelRule(horizon_sessions=1, return_type="excess_price_return", benchmark_asset="UNKNOWN")
    with pytest.raises(ValueError, match="benchmark_asset"):
        compute_labels(make_panel(), unknown, ["A"], moment(4, 17))
    own = LabelRule(horizon_sessions=1, return_type="excess_price_return", benchmark_asset="A")
    with pytest.raises(ValueError, match="outside"):
        compute_labels(make_panel(), own, ["A"], moment(4, 17))


def test_inplace_data_mutation_is_revalidated_before_computation() -> None:
    panel = make_panel()
    panel.observations[0].values["open"] = float("nan")
    with pytest.raises(ValidationError):
        compute_labels(panel, LabelRule(horizon_sessions=1), ["A"], moment(4, 17))


def test_inplace_duplicate_calendar_is_revalidated() -> None:
    panel = make_panel()
    panel.sessions.append(deepcopy(panel.sessions[-1]))
    with pytest.raises(ValidationError):
        compute_labels(panel, LabelRule(horizon_sessions=1), ["A"], moment(4, 17))


def test_non_contract_inputs_rejected_explicitly() -> None:
    with pytest.raises(ValueError, match="validated S1 contracts"):
        compute_labels({}, LabelRule(horizon_sessions=1), ["A"], moment(4, 17))
