"""Developer regressions for S1-001; synthetic inputs confer no review approval.

The five-session counterexample reproduces the audit's semantic facts: A's
entry/exit opens are 20/20, closes are 20/40, and volumes are 300/900. It is
rebuilt here, rather than consuming or modifying independent audit artifacts.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from bbag.labels import compute_labels
from bbag.s1_contracts import LabelRule, MarketPanel, S1Config, load_s1_config


START = date(2026, 2, 2)


def instant(index: int, hour: int, minute: int = 0) -> datetime:
    day = START + timedelta(days=index)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def synthetic_panel() -> MarketPanel:
    # The benchmark independently gives 50/40 - 1 = 1/4 for the first label.
    opens = {"A": [20.0] * 5, "BENCH": [30.0, 40.0, 50.0, 60.0, 70.0]}
    closes = [10.0, 20.0, 40.0, 80.0, 160.0]
    volumes = [100.0, 300.0, 900.0, 2700.0, 8100.0]
    return MarketPanel.model_validate({
        "data_id": "S1-001-developer-synthetic-quote-event-regression",
        "source": "synthetic_fixture", "timezone": "UTC",
        "sessions": [dict(session=START + timedelta(days=i), open_at=instant(i, 9),
                          close_at=instant(i, 16), signal_at=instant(i, 16, 10))
                     for i in range(5)],
        "assets": ["A", "BENCH"], "fields": ["open", "close", "volume"],
        "observations": [dict(session=START + timedelta(days=i), asset=asset,
                              available_at=instant(i, 16),
                              values={"open": prices[i], "close": closes[i], "volume": volumes[i]})
                         for asset, prices in opens.items() for i in range(5)],
    })


def label_rule(return_type: str = "price_return") -> LabelRule:
    return LabelRule(horizon_sessions=1, return_type=return_type,
                     benchmark_asset="BENCH" if return_type == "excess_price_return" else None)


def synthetic_config(return_type: str = "price_return") -> S1Config:
    return S1Config(
        panel=synthetic_panel(), universe=["A"], label=label_rule(return_type), as_of=instant(4, 17),
        factor={"factor_id": "quote-event-regression", "hypothesis": "Synthetic engineering fixture",
                "formula": "close", "required_fields": ["close"], "lookback_sessions": 1,
                "expected_direction": "undetermined"},
        pending_decisions=["Synthetic prices only; no economic or platform conclusion"],
    )


def first_label(panel: MarketPanel, rule: LabelRule, cutoff: datetime):
    result = compute_labels(panel, rule, ["A"], cutoff)
    return next(row for row in result.rows if row.session == START and row.asset == "A")


@pytest.mark.parametrize("field", ["close", "volume", "high", "low", "vwap", "open_price", "Open", " open", "open "])
def test_rule_rejects_every_unsupported_quote_without_normalization(field: str) -> None:
    payload = {"horizon_sessions": 1, "price_field": field}
    with pytest.raises(ValidationError) as rejected:
        LabelRule.model_validate(payload)
    assert any(error["loc"] == ("price_field",) for error in rejected.value.errors())
    assert payload["price_field"] == field


@pytest.mark.parametrize("field", ["close", "volume"])
def test_exact_audit_fields_are_rejected_by_config_loader(tmp_path: Path, field: str) -> None:
    payload = synthetic_config().model_dump(mode="json")
    payload["label"]["price_field"] = field
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    original = source.read_bytes()
    with pytest.raises(ValidationError) as rejected:
        load_s1_config(source)
    assert any(error["loc"] == ("label", "price_field") for error in rejected.value.errors())
    assert source.read_bytes() == original


def forged_rule(field: str, route: str, return_type: str = "price_return") -> LabelRule:
    valid = label_rule(return_type)
    if route == "model_construct":
        return LabelRule.model_construct(**{**valid.model_dump(), "price_field": field})
    if route == "model_copy":
        return valid.model_copy(update={"price_field": field})
    object.__setattr__(valid, "price_field", field)
    return valid


@pytest.mark.parametrize("field", ["close", "volume"])
@pytest.mark.parametrize("route", ["model_construct", "model_copy", "object_setattr"])
@pytest.mark.parametrize("return_type", ["price_return", "excess_price_return"])
def test_direct_service_revalidates_forged_rule_for_both_return_types(field: str, route: str, return_type: str) -> None:
    rule = forged_rule(field, route, return_type)
    with pytest.raises(ValidationError) as rejected:
        compute_labels(synthetic_panel(), rule, ["A"], instant(4, 17))
    assert any(error["loc"] == ("price_field",) for error in rejected.value.errors())
    assert rule.price_field == field


@pytest.mark.parametrize("field", ["close", "volume"])
def test_nested_config_revalidates_constructed_label_rule(field: str) -> None:
    payload = synthetic_config().model_dump(mode="python")
    payload["label"] = forged_rule(field, "model_construct")
    with pytest.raises(ValidationError) as rejected:
        S1Config.model_validate(payload)
    assert any(error["loc"] == ("label", "price_field") for error in rejected.value.errors())


@pytest.mark.parametrize("field", ["close", "volume"])
def test_invalid_assignment_cannot_change_supported_open_rule(field: str) -> None:
    rule = label_rule()
    with pytest.raises(ValidationError):
        rule.price_field = field
    assert rule.price_field == "open"
    assert first_label(synthetic_panel(), rule, instant(4, 17)).value == 0.0


@pytest.mark.parametrize("field", ["close", "volume"])
def test_cli_rejects_audit_counterexample_without_success_directory(tmp_path: Path, run_cli, cli_json, field: str) -> None:
    payload = synthetic_config().model_dump(mode="json")
    payload["label"]["price_field"] = field
    source = tmp_path / "input.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "runs"
    result = run_cli("s1-labels", "--config", source, "--output-dir", output, "--run-id", field)
    assert result.returncode == 2, (result.stdout, result.stderr)
    error = cli_json(result)
    assert error["valid"] is False
    assert any(item["path"] == ["label", "price_field"] for item in error["errors"])
    assert not output.exists()


@pytest.mark.parametrize("return_type,expected", [
    ("price_return", Fraction(20, 20) - 1),
    ("excess_price_return", Fraction(20, 20) - Fraction(50, 40)),
])
def test_supported_open_cli_retains_exact_event_and_rational_answer(tmp_path: Path, run_cli, return_type: str, expected: Fraction) -> None:
    config = synthetic_config(return_type)
    source = tmp_path / "open.json"
    source.write_text(config.model_dump_json(), encoding="utf-8")
    output = tmp_path / "runs"
    result = run_cli("s1-labels", "--config", source, "--output-dir", output, "--run-id", "open")
    assert result.returncode == 0, result.stderr
    saved = json.loads((output / "open" / "result.json").read_bytes())
    first = saved["rows"][0]
    assert first["session"] == "2026-02-02" and first["asset"] == "A"
    assert first["status"] == "MATURED"
    assert first["value"] == float(expected)
    assert datetime.fromisoformat(first["entry_at"]) == instant(1, 9)
    assert datetime.fromisoformat(first["exit_at"]) == instant(2, 9)
    assert datetime.fromisoformat(first["mature_at"]) == instant(2, 16)
    assert saved["rule"]["price_field"] == "open"


@pytest.mark.parametrize("return_type,expected", [("price_return", 0.0), ("excess_price_return", -0.25)])
def test_open_rule_maturity_still_withholds_until_exact_availability(return_type: str, expected: float) -> None:
    panel, rule = synthetic_panel(), label_rule(return_type)
    boundary = instant(2, 16)
    before = first_label(panel, rule, boundary - timedelta(microseconds=1))
    exact = first_label(panel, rule, boundary)
    assert before.status == "NOT_MATURED" and before.value is None
    assert before.mature_at == boundary
    assert exact.status == "MATURED" and exact.value == expected
    assert (exact.entry_at, exact.exit_at) == (instant(1, 9), instant(2, 9))


def test_excess_open_quote_waits_for_late_benchmark_without_reading_close_or_volume() -> None:
    panel = synthetic_panel()
    late = instant(3, 18)
    for observation in panel.observations:
        if observation.asset == "BENCH" and observation.session == START + timedelta(days=2):
            observation.available_at = late
    rule = label_rule("excess_price_return")
    before = first_label(panel, rule, late - timedelta(microseconds=1))
    exact = first_label(panel, rule, late)
    assert before.mature_at == late and before.status == "NOT_MATURED" and before.value is None
    assert exact.status == "MATURED" and exact.value == float(Fraction(-1, 4))
    assert exact.entry_at == instant(1, 9) and exact.exit_at == instant(2, 9)
