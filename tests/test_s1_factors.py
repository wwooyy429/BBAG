"""Independent hand calculations and causality checks for synthetic S1 factors."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo

import pytest

from bbag.contracts import FactorSpec
from bbag.factors import compute_factors
from bbag.s1_contracts import AuditLimits, MarketPanel


ASSETS = ["A", "B", "C"]
CLOSES: dict[str, list[float | None]] = {
    "A": [10.0, 20.0, 10.0, 40.0],
    "B": [20.0, 10.0, 10.0, 20.0],
    "C": [30.0, 30.0, 30.0, 30.0],
}


def make_panel(closes: dict[str, list[float | None]] | None = None) -> MarketPanel:
    values = closes or CLOSES
    count = len(next(iter(values.values())))
    days = [date(2024, 1, 2) + timedelta(days=index) for index in range(count)]
    sessions = [{
        "session": day,
        "open_at": datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=14, minutes=30),
        "close_at": datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=21),
        "signal_at": datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=21, minutes=1),
    } for day in days]
    observations = [{
        "session": session["session"], "asset": asset,
        "available_at": session["close_at"] + timedelta(seconds=30),
        "values": {"open": values[asset][index], "close": values[asset][index]},
    } for index, session in enumerate(sessions) for asset in sorted(values)]
    return MarketPanel.model_validate({
        "data_id": "SYNTHETIC_HAND_CALCULATION_ONLY",
        "timezone": "America/New_York",
        "sessions": sessions, "assets": sorted(values),
        "fields": ["open", "close"], "observations": observations,
    })


def factor(formula: str, lookback: int = 1, fields: list[str] | None = None) -> FactorSpec:
    return FactorSpec(
        factor_id="SYNTHETIC_ENGINEERING_FACTOR", hypothesis="Hand calculation fixture; no economic approval",
        formula=formula, required_fields=fields or ["close"], lookback_sessions=lookback,
        expected_direction="undetermined",
    )


def compute(panel: MarketPanel, formula: str, lookback: int = 1, *, mode: str = "batch"):
    return compute_factors(panel, factor(formula, lookback), AuditLimits(), ASSETS, panel.sessions[-1].signal_at, mode=mode)


def by_asset(result, asset: str) -> list[float | None]:
    return [row.value for row in result.rows if row.asset == asset]


@pytest.mark.parametrize("formula,lookback,asset,expected", [
    ("close", 1, "A", [10.0, 20.0, 10.0, 40.0]),
    ("neg(return(close,1))", 2, "A", [None, -1.0, 0.5, -3.0]),
    ("return(close,1)", 2, "B", [None, -0.5, 0.0, 1.0]),
    ("delay(close,1)", 2, "A", [None, 10.0, 20.0, 10.0]),
    ("rolling_mean(close,2)", 2, "A", [None, 15.0, 15.0, 25.0]),
    ("rolling_std(close,2)", 2, "A", [None, 5.0, 5.0, 15.0]),
    ("ts_rank(close,2)", 2, "A", [None, 1.0, 0.0, 1.0]),
    ("ts_rank(close,2)", 2, "B", [None, 0.0, 0.5, 1.0]),
    ("ts_rank(close,2)", 2, "C", [None, 0.5, 0.5, 0.5]),
    ("delay(rolling_mean(close,2),1)", 3, "A", [None, None, 15.0, 15.0]),
    ("cs_rank(close)", 1, "A", [0.0, 0.5, 0.25, 1.0]),
    ("cs_rank(close)", 1, "B", [0.5, 0.0, 0.25, 0.0]),
    ("cs_rank(close)", 1, "C", [1.0, 1.0, 1.0, 0.5]),
    ("(close-10)/(close-10)", 1, "A", [None, 1.0, None, 1.0]),
    ("mul(add(close,2),0.5)", 1, "A", [6.0, 11.0, 6.0, 21.0]),
])
@pytest.mark.parametrize("mode", ["batch", "incremental"])
def test_operators_match_independent_hand_answers(formula, lookback, asset, expected, mode) -> None:
    actual = by_asset(compute(make_panel(), formula, lookback, mode=mode), asset)
    assert actual == expected


@pytest.mark.parametrize("formula", ["rolling_std(close,1)", "ts_rank(close,1)"])
def test_one_value_windows_have_explicit_semantics(formula: str) -> None:
    expected = [0.0] * 4 if "std" in formula else [0.5] * 4
    assert by_asset(compute(make_panel(), formula), "A") == expected


def test_missing_cells_and_absent_observations_do_not_compress_sessions() -> None:
    panel = make_panel({**CLOSES, "B": [20.0, 10.0, None, 20.0]})
    result = compute(panel, "rolling_mean(close,2)", 2)
    assert by_asset(result, "B") == [None, 15.0, None, None]
    panel.observations = [row for row in panel.observations if not (row.asset == "B" and row.session == panel.sessions[2].session)]
    assert by_asset(compute(panel, "return(close,1)", 2), "B") == [None, -0.5, None, None]
    assert by_asset(compute(panel, "delay(close,1)", 2), "B") == [None, 20.0, 10.0, None]


def test_cross_section_uses_only_requested_universe_and_available_values() -> None:
    panel = make_panel({**CLOSES, "B": [20.0, 10.0, None, 20.0]})
    result = compute_factors(panel, factor("cs_rank(close)"), AuditLimits(), ["B", "A"], panel.sessions[-1].signal_at)
    assert by_asset(result, "A") == [0.0, 1.0, 0.5, 1.0]
    assert by_asset(result, "B") == [1.0, 0.0, None, 0.0]
    assert [row.asset for row in result.rows] == ["A", "B"] * 4


def test_zero_denominator_and_computed_overflow_are_missing_not_nonfinite() -> None:
    panel = make_panel({"A": [0.0, 1e308, 1e308, 1e308], "B": CLOSES["B"], "C": CLOSES["C"]})
    assert by_asset(compute(panel, "return(close,1)", 2), "A") == [None, None, 0.0, 0.0]
    assert by_asset(compute(panel, "div(close,0)"), "A") == [None] * 4
    assert by_asset(compute(panel, "mul(close,1000000000000)"), "A") == [0.0, None, None, None]
    # A constant extreme window has a finite mean and exactly zero dispersion.
    assert by_asset(compute(panel, "rolling_mean(close,2)", 2), "A") == [None, 5e307, 1e308, 1e308]
    assert by_asset(compute(panel, "rolling_std(close,2)", 2), "A") == [None, 5e307, 0.0, 0.0]
    for row in compute(panel, "rolling_std(close,2)", 2).rows:
        assert row.value is None or math.isfinite(row.value)


@pytest.mark.parametrize("mode", ["batch", "incremental"])
def test_late_historical_quote_is_available_only_after_arrival(mode: str) -> None:
    panel = make_panel()
    previous = next(row for row in panel.observations if row.asset == "A" and row.session == panel.sessions[0].session)
    previous.available_at = panel.sessions[1].signal_at
    assert by_asset(compute(panel, "close", mode=mode), "A") == [None, 20.0, 10.0, 40.0]
    # At day 2 the delayed day-1 quote has arrived, even though it was unknown at day-1 signal.
    assert by_asset(compute(panel, "delay(close,1)", 2, mode=mode), "A") == [None, 10.0, 20.0, 10.0]
    assert by_asset(compute(panel, "return(close,1)", 2, mode=mode), "A") == [None, 1.0, -0.5, 3.0]


@pytest.mark.parametrize("formula,lookback", [
    ("return(close,1)", 2), ("rolling_mean(close,2)", 2),
    ("delay(cs_rank(close),1)", 2), ("rolling_std(add(close,2),3)", 3),
])
def test_future_perturbation_and_prefix_input_preserve_past_outputs(formula: str, lookback: int) -> None:
    panel = make_panel()
    cutoff = panel.sessions[2].signal_at
    expected = compute_factors(panel, factor(formula, lookback), AuditLimits(), ASSETS, cutoff)
    payload = panel.model_dump(mode="python")
    for observation in payload["observations"]:
        if observation["session"] > panel.sessions[2].session:
            observation["values"] = {"open": 999999.0, "close": -123456.0}
    changed = MarketPanel.model_validate(payload)
    actual = compute_factors(changed, factor(formula, lookback), AuditLimits(), ASSETS, cutoff)
    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
    payload["sessions"] = payload["sessions"][:3]
    payload["observations"] = [row for row in payload["observations"] if row["session"] <= panel.sessions[2].session]
    prefix = MarketPanel.model_validate(payload)
    incremental = compute_factors(prefix, factor(formula, lookback), AuditLimits(), ASSETS, cutoff, mode="incremental")
    assert incremental.rows == expected.rows


def test_batch_and_incremental_match_each_signal_not_latest_snapshot() -> None:
    panel = make_panel()
    panel.observations[0].available_at = panel.sessions[2].signal_at
    batch = compute(panel, "rolling_mean(return(close,1),2)", 3)
    incremental = compute(panel, "rolling_mean(return(close,1),2)", 3, mode="incremental")
    assert batch.rows == incremental.rows
    assert by_asset(batch, "A") == [None, None, 0.25, 1.25]
    assert by_asset(batch, "B") == [None, None, -0.25, 0.5]


def test_asof_is_inclusive_and_never_uses_unformed_signal() -> None:
    panel = make_panel()
    first = panel.sessions[0].signal_at
    before = compute_factors(panel, factor("close"), AuditLimits(), ASSETS, first - timedelta(microseconds=1))
    exact = compute_factors(panel, factor("close"), AuditLimits(), ASSETS, first)
    assert before.rows == []
    assert [row.value for row in exact.rows] == [10.0, 20.0, 30.0]
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_factors(panel, factor("close"), AuditLimits(), ASSETS, first.replace(tzinfo=None))


def test_revalidate_nested_mutation_and_audit_before_computation() -> None:
    panel = make_panel()
    panel.observations[0].values["close"] = float("nan")
    with pytest.raises(ValueError):
        compute(panel, "close")
    panel = make_panel()
    with pytest.raises(ValueError):
        compute_factors(panel, factor("return(close,2)", 1), AuditLimits(), ASSETS, panel.sessions[-1].signal_at)
    with pytest.raises(ValueError):
        compute_factors(panel, factor("open", fields=["close"]), AuditLimits(), ASSETS, panel.sessions[-1].signal_at)


@pytest.mark.parametrize("universe", [[], ["A", "A"], ["UNKNOWN"], [True], "A"])
def test_invalid_universe_is_rejected(universe) -> None:
    panel = make_panel()
    with pytest.raises(ValueError):
        compute_factors(panel, factor("close"), AuditLimits(), universe, panel.sessions[-1].signal_at)


def test_observation_and_universe_order_does_not_change_output_order() -> None:
    panel = make_panel()
    expected = compute_factors(panel, factor("cs_rank(close)"), AuditLimits(), ASSETS, panel.sessions[-1].signal_at)
    payload = panel.model_dump(mode="python")
    payload["observations"].reverse()
    actual = compute_factors(MarketPanel.model_validate(payload), factor("cs_rank(close)"), AuditLimits(), list(reversed(ASSETS)), panel.sessions[-1].signal_at)
    assert actual.rows == expected.rows


def test_invalid_mode_is_rejected() -> None:
    panel = make_panel()
    with pytest.raises(ValueError, match="mode"):
        compute(panel, "close", mode="unsupported")


@pytest.mark.parametrize("invalid_contract", ["panel", "factor", "limits"])
def test_public_entry_rejects_wrong_contract_objects_with_value_error(invalid_contract: str) -> None:
    panel = make_panel()
    arguments = {
        "panel": panel, "factor": factor("close"), "limits": AuditLimits(),
        "universe": ASSETS, "as_of": panel.sessions[-1].signal_at,
    }
    arguments[invalid_contract] = object()
    with pytest.raises(ValueError, match="contract"):
        compute_factors(**arguments)


def test_work_unit_budget_is_checked_before_evaluating_rows() -> None:
    panel = make_panel()
    # One field node * one history slot * three assets * four signals = 12.
    with pytest.raises(ValueError, match="work-unit limit"):
        compute_factors(panel, factor("close"), AuditLimits(max_work_units=11), ASSETS, panel.sessions[-1].signal_at)
    result = compute_factors(panel, factor("close"), AuditLimits(max_work_units=12), ASSETS, panel.sessions[-1].signal_at)
    assert len(result.rows) == 12


@pytest.mark.parametrize("mode", ["batch", "incremental"])
def test_dst_fold_uses_real_time_for_arrival_and_signal_cutoffs(mode: str) -> None:
    zone = ZoneInfo("America/New_York")
    day = date(2024, 11, 3)
    panel = MarketPanel.model_validate({
        "data_id": "SYNTHETIC_DST_FOLD_ONLY", "timezone": "America/New_York",
        "sessions": [{"session": day,
            "open_at": datetime(2024, 11, 3, 0, 30, tzinfo=zone),
            "close_at": datetime(2024, 11, 3, 1, 15, fold=0, tzinfo=zone),
            "signal_at": datetime(2024, 11, 3, 1, 30, fold=0, tzinfo=zone)}],
        "assets": ["A"], "fields": ["close"],
        "observations": [{"session": day, "asset": "A",
            "available_at": datetime(2024, 11, 3, 1, 20, fold=1, tzinfo=zone),
            "values": {"close": 123.0}}],
    })
    # 01:25 in the second fold is after 01:30 in the first fold. The row exists,
    # but its quote arrived after that historical signal and cannot backfill it.
    late_asof = datetime(2024, 11, 3, 1, 25, fold=1, tzinfo=zone)
    result = compute_factors(panel, factor("close"), AuditLimits(), ["A"], late_asof, mode=mode)
    assert len(result.rows) == 1 and result.rows[0].value is None
    early_asof = datetime(2024, 11, 3, 1, 25, fold=0, tzinfo=zone)
    assert compute_factors(panel, factor("close"), AuditLimits(), ["A"], early_asof, mode=mode).rows == []
