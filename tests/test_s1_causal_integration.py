"""Cross-service causal checks with independent synthetic arithmetic answers."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction

import pytest
from pydantic import ValidationError

from bbag.contracts import FactorSpec
from bbag.factors import compute_factors
from bbag.labels import compute_labels
from bbag.s1_contracts import AuditLimits, LabelRule, MarketPanel, NumericPolicy


DAY = date(2026, 2, 2)


def instant(index: int, hour: int, minute: int = 0) -> datetime:
    day = DAY + timedelta(days=index)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def integration_panel() -> MarketPanel:
    opens = {"A": [9.0, 12.0, 18.0, 22.0, 24.0, 40.0],
             "B": [20.0, 30.0, 24.0, 20.0, 30.0, 15.0],
             "BENCH": [100.0, 110.0, 121.0, 133.1, 146.41, 161.051]}
    closes = {"A": [10.0, 20.0, 15.0, 30.0, 25.0, 50.0],
              "B": [30.0, 15.0, 15.0, 10.0, 20.0, 10.0],
              "BENCH": [10000.0, 1.0, 10000.0, 1.0, 10000.0, 1.0]}
    return MarketPanel.model_validate({
        "data_id": "SYNTHETIC_CROSS_SERVICE_HAND_ANSWERS", "timezone": "UTC",
        "assets": ["A", "B", "BENCH"], "fields": ["open", "close"],
        "sessions": [dict(session=DAY + timedelta(days=i), open_at=instant(i, 9),
                          close_at=instant(i, 16), signal_at=instant(i, 16, 5)) for i in range(6)],
        "observations": [dict(session=DAY + timedelta(days=i), asset=asset,
                              available_at=instant(i, 16),
                              values={"open": opens[asset][i], "close": closes[asset][i]})
                         for i in range(6) for asset in sorted(opens)],
    })


def spec(expression: str, lookback: int) -> FactorSpec:
    return FactorSpec(factor_id="INDEPENDENT_CROSS_SERVICE_FIXTURE", hypothesis="Synthetic engineering only",
                      formula=expression, required_fields=["close"], lookback_sessions=lookback,
                      expected_direction="undetermined")


def values(result, asset: str):
    return [row.value for row in result.rows if row.asset == asset]


def label_at(result, index: int, asset: str = "A"):
    return next(row for row in result.rows if row.session == DAY + timedelta(days=index) and row.asset == asset)


def test_future_quote_changes_affect_labels_but_never_past_factors() -> None:
    panel = integration_panel()
    changed = panel.model_dump(mode="python")
    for observation in changed["observations"]:
        if observation["session"] == DAY + timedelta(days=4) and observation["asset"] == "A":
            observation["values"] = {"open": 44.0, "close": 400.0}
    revised = MarketPanel.model_validate(changed)
    factor = spec("return(close,1)", 2)
    cutoff = instant(2, 16, 5)
    before = compute_factors(panel, factor, AuditLimits(), ["A", "B"], cutoff)
    after = compute_factors(revised, factor, AuditLimits(), ["A", "B"], cutoff)
    assert before.rows == after.rows
    assert values(before, "A") == [None, 1.0, -0.25]
    assert values(before, "B") == [None, -0.5, 0.0]
    rule = LabelRule(horizon_sessions=1)
    early_before = compute_labels(panel, rule, ["A", "B"], cutoff)
    early_after = compute_labels(revised, rule, ["A", "B"], cutoff)
    assert label_at(early_before, 2).status == label_at(early_after, 2).status == "NOT_MATURED"
    assert label_at(early_before, 2).value is label_at(early_after, 2).value is None
    mature_before = compute_labels(panel, rule, ["A", "B"], instant(5, 17))
    mature_after = compute_labels(revised, rule, ["A", "B"], instant(5, 17))
    # Signal on index 2 enters at 22 and exits at 24 (1/11); revised exit 44 gives 100%.
    assert label_at(mature_before, 2).value == pytest.approx(float(Fraction(1, 11)), abs=1e-12)
    assert label_at(mature_after, 2).value == pytest.approx(1.0, abs=1e-12)


def test_both_services_wait_for_formed_signals_with_full_future_input_present() -> None:
    panel = integration_panel()
    factor = spec("close", 1)
    before = instant(0, 16, 5) - timedelta(microseconds=1)
    assert compute_factors(panel, factor, AuditLimits(), ["A", "B"], before).rows == []
    assert compute_labels(panel, LabelRule(horizon_sessions=1), ["A", "B"], before).rows == []
    exact = instant(0, 16, 5)
    assert values(compute_factors(panel, factor, AuditLimits(), ["A", "B"], exact), "A") == [10.0]
    labels = compute_labels(panel, LabelRule(horizon_sessions=1), ["A", "B"], exact)
    assert len(labels.rows) == 2
    assert all(row.status == "NOT_MATURED" and row.value is None for row in labels.rows)


def test_fixed_factor_universe_excludes_benchmark_and_does_not_depend_on_label_calls() -> None:
    panel = integration_panel()
    factor = spec("cs_rank(close)", 1)
    before = compute_factors(panel, factor, AuditLimits(), ["B", "A"], instant(5, 17))
    assert values(before, "A") == [0.0, 1.0, 0.5, 1.0, 1.0, 1.0]
    assert values(before, "B") == [1.0, 0.0, 0.5, 0.0, 0.0, 0.0]
    original_data = deepcopy(panel.model_dump(mode="python"))
    labels = compute_labels(panel, LabelRule(horizon_sessions=1, return_type="excess_price_return", benchmark_asset="BENCH"),
                            ["A", "B"], instant(5, 17))
    # A's first target is 12->18 (+50%), benchmark 110->121 (+10%).
    assert label_at(labels, 0).value == pytest.approx(0.40, abs=1e-12)
    assert panel.model_dump(mode="python") == original_data
    after = compute_factors(panel, factor, AuditLimits(), ["A", "B"], instant(5, 17), mode="incremental")
    assert after.rows == before.rows


def test_late_history_uses_current_decision_cutoff_without_rewriting_old_rank() -> None:
    payload = integration_panel().model_dump(mode="python")
    for observation in payload["observations"]:
        if observation["session"] == DAY and observation["asset"] == "A":
            observation["available_at"] = instant(1, 16, 5)
    panel = MarketPanel.model_validate(payload)
    current_rank = compute_factors(panel, spec("cs_rank(close)", 1), AuditLimits(), ["A", "B"], instant(2, 17))
    assert values(current_rank, "A") == [None, 1.0, 0.5]
    assert values(current_rank, "B") == [0.5, 0.0, 0.5]
    delayed_rank = compute_factors(panel, spec("delay(cs_rank(close),1)", 2), AuditLimits(), ["A", "B"], instant(2, 17))
    # The late day-0 value has arrived by day-1's decision, so delayed historical
    # rank is now knowable. It must not backfill the previously formed day-0 row.
    assert values(delayed_rank, "A") == [None, 0.0, 1.0]
    assert values(delayed_rank, "B") == [None, 1.0, 0.0]
    assert values(current_rank, "A")[0] is None


def test_batch_incremental_and_truncation_match_independent_window_answers() -> None:
    panel = integration_panel()
    cutoff = instant(3, 16, 5)
    factor = spec("rolling_mean(return(close,1),2)", 3)
    full = compute_factors(panel, factor, AuditLimits(), ["A", "B"], cutoff)
    prefix_payload = panel.model_dump(mode="python")
    prefix_payload["sessions"] = prefix_payload["sessions"][:4]
    prefix_payload["observations"] = [row for row in prefix_payload["observations"] if row["session"] <= DAY + timedelta(days=3)]
    prefix = compute_factors(MarketPanel.model_validate(prefix_payload), factor, AuditLimits(), ["A", "B"], cutoff, mode="incremental")
    assert prefix.rows == full.rows
    for actual, expected in zip(values(full, "A"), [None, None, Fraction(3, 8), Fraction(3, 8)]):
        assert actual is None if expected is None else actual == pytest.approx(float(expected), abs=1e-12)
    for actual, expected in zip(values(full, "B"), [None, None, Fraction(-1, 4), Fraction(-1, 6)]):
        assert actual is None if expected is None else actual == pytest.approx(float(expected), abs=1e-12)


@pytest.mark.parametrize("expression", ["add(cs_rank(0),close)", "add(rolling_mean(0,2),close)"])
def test_series_type_audit_cannot_be_bypassed_by_a_field_in_another_branch(expression: str) -> None:
    with pytest.raises(ValueError, match="field-dependent"):
        compute_factors(integration_panel(), spec(expression, 2), AuditLimits(), ["A", "B"], instant(5, 17))


def test_work_budget_counts_formed_signals_and_fixed_universe_not_future_panel() -> None:
    panel = integration_panel()
    cutoff = instant(1, 16, 5)
    # One AST field node * one history slot * two target assets * two signals=4.
    with pytest.raises(ValueError, match="work-unit limit"):
        compute_factors(panel, spec("close", 1), AuditLimits(max_work_units=3), ["A", "B"], cutoff)
    accepted = compute_factors(panel, spec("close", 1), AuditLimits(max_work_units=4), ["A", "B"], cutoff)
    assert len(accepted.rows) == 4
    assert values(accepted, "A") == [10.0, 20.0]
    assert values(accepted, "B") == [30.0, 15.0]


def test_cross_service_contracts_reject_bool_parameters_and_naive_cutoffs() -> None:
    with pytest.raises(ValidationError):
        AuditLimits(max_work_units=True)
    with pytest.raises(ValidationError):
        NumericPolicy(std_ddof=False)
    with pytest.raises(ValidationError):
        LabelRule(horizon_sessions=True)
    panel = integration_panel()
    naive = datetime(2026, 2, 2, 17)
    with pytest.raises(ValueError):
        compute_factors(panel, spec("close", 1), AuditLimits(), ["A", "B"], naive)
    with pytest.raises(ValueError):
        compute_labels(panel, LabelRule(horizon_sessions=1), ["A", "B"], naive)
