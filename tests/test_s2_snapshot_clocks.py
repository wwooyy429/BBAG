"""Frozen synthetic snapshot clocks; no provider, SDK or market data calls."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from bbag.contracts import TimeRange
from bbag.s2_access import AccessGuard, AccessPolicy, DataRequest, STANDARD_FIELDS
from bbag.s2_data import (
    LiquidityRule, StandardDataBundle, canonical_request_sha256, data_quality,
    historical_universe, standardize_daily_json, to_s1_panel, validate_snapshot_selection,
)


DAYS = [date(2024, 1, day) for day in (2, 3, 4)]
ASSETS = ["SYN_A", "SYN_B"]
DELTA = timedelta(microseconds=1)


def at(day, hour=22, minute=0):
    return datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=hour, minutes=minute)


CUTOFF = at(DAYS[-1])
OPERATIONS = (historical_universe, data_quality, to_s1_panel)
KNOWLEDGE = (
    ("identities", 0, "known_at"),
    ("identities", 1, "delisting_known_at"),
    ("tickers", 2, "known_at"),
    ("tickers", 0, "valid_to_known_at"),
    ("membership", 0, "known_at"),
    ("corporate_actions", 0, "known_at"),
)


def request(**changes):
    values = dict(provider="SYNTHETIC", start=DAYS[0], end=DAYS[-1], assets=ASSETS,
                  fields=list(STANDARD_FIELDS), as_of=CUTOFF, classification="DEVELOPMENT")
    values.update(changes)
    return DataRequest(**values)


def guard():
    return AccessGuard(AccessPolicy(policy_id="SYNTHETIC_FROZEN_CLOCKS", provider="SYNTHETIC",
        development=TimeRange(start=DAYS[0], end=DAYS[-1]), allowed_assets=ASSETS,
        allowed_fields=list(STANDARD_FIELDS)))


def payload():
    original = request()
    known = at(date(2024, 1, 1))
    bars = []
    for index, day in enumerate(DAYS):
        for sid, close in (("SYN_A", (10.0, 11.0, 5.5)[index]), ("SYN_B", 20.0)):
            bars.append(dict(sid=sid, session=day, open=close, high=close + 1.0,
                low=close - 1.0, close=close, volume=100.0, available_at=at(day, 21, 1)))
    return dict(data_id="SYNTHETIC_FROZEN_SNAPSHOT", calendar_id="SYNTHETIC_EXPLICIT_THREE_DAYS",
        calendar_version="v1", request=original,
        provenance=dict(provider="SYNTHETIC", provider_data_version="synthetic-clock-v1",
            source_sha256="a" * 64, request_sha256=canonical_request_sha256(original),
            retrieved_at=CUTOFF + timedelta(hours=2), classification="DEVELOPMENT"),
        sessions=[dict(session=day, open_at=at(day, 14, 30), close_at=at(day, 21), signal_at=at(day, 21, 5)) for day in DAYS],
        identities=[dict(sid="SYN_A", listed_from=DAYS[0], known_at=known),
            dict(sid="SYN_B", listed_from=DAYS[0], known_at=known, delisted_on=date(2024, 1, 5),
                delisting_known_at=at(DAYS[1], 12))],
        tickers=[dict(sid="SYN_A", ticker="OLD", valid_from=DAYS[0], valid_to=DAYS[1],
                     known_at=known, valid_to_known_at=at(DAYS[1], 12)),
            dict(sid="SYN_A", ticker="NEW", valid_from=DAYS[2], known_at=at(DAYS[1], 12)),
            dict(sid="SYN_B", ticker="B", valid_from=DAYS[0], known_at=known)],
        membership=[dict(sid=sid, effective_on=DAYS[0], member=True, known_at=known) for sid in ASSETS],
        corporate_actions=[dict(sid="SYN_A", kind="SPLIT", effective_on=DAYS[2],
                                known_at=at(DAYS[1], 12), shares_after_per_before=2.0)], bars=bars)


@pytest.mark.parametrize("target", KNOWLEDGE)
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_each_metadata_clock_obeys_the_frozen_cutoff_not_later_retrieval(target, offset):
    raw = payload()
    collection, index, field = target
    raw[collection][index][field] = CUTOFF + offset * DELTA
    if offset > 0:
        with pytest.raises(ValueError, match="metadata knowledge"):
            StandardDataBundle.model_validate(raw)
    else:
        checked = StandardDataBundle.model_validate(raw)
        assert getattr(getattr(checked, collection)[index], field) == CUTOFF + offset * DELTA


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_physical_retrieval_must_reach_the_declared_snapshot_cutoff(offset):
    raw = payload()
    raw["provenance"]["retrieved_at"] = CUTOFF + offset * DELTA
    if offset < 0:
        with pytest.raises(ValueError, match="retrieval cannot precede"):
            StandardDataBundle.model_validate(raw)
    else:
        assert StandardDataBundle.model_validate(raw).provenance.retrieved_at == CUTOFF + offset * DELTA


@pytest.mark.parametrize("function", OPERATIONS)
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_selection_can_rewind_or_reach_but_not_advance_the_original_snapshot(function, offset):
    data = StandardDataBundle.model_validate(payload())
    selected = request(as_of=CUTOFF + offset * DELTA)
    if offset > 0:
        with pytest.raises(ValueError, match="selection as_of"):
            function(data, selected, guard())
    else:
        result = function(data, selected, guard())
        if function is data_quality:
            assert len(result.rows) == 6
        else:
            rows = result.sessions if function is to_s1_panel else result.rows
            assert [row.session for row in rows] == DAYS


def test_aware_offsets_compare_instants_and_preserve_original_request_bytes():
    raw = payload()
    equivalent = CUTOFF.astimezone(timezone(timedelta(hours=8)))
    raw["provenance"]["retrieved_at"] = equivalent
    for collection, index, field in KNOWLEDGE:
        raw[collection][index][field] = equivalent
    data = StandardDataBundle.model_validate(raw)
    before = data.request.model_dump(mode="json")
    assert historical_universe(data, request(as_of=equivalent), guard()).as_of == equivalent
    assert data.request.model_dump(mode="json") == before


@pytest.mark.parametrize("target", ["request", "retrieved", *KNOWLEDGE])
def test_snapshot_clocks_reject_naive_datetimes(target):
    raw = payload()
    naive = CUTOFF.replace(tzinfo=None)
    if target == "request":
        raw["request"] = raw["request"].model_copy(update={"as_of": naive})
    elif target == "retrieved":
        raw["provenance"]["retrieved_at"] = naive
    else:
        collection, index, field = target
        raw[collection][index][field] = naive
    with pytest.raises(ValueError):
        StandardDataBundle.model_validate(raw)


@pytest.mark.parametrize("function", OPERATIONS)
@pytest.mark.parametrize("mutation", ["metadata", "retrieval", "request_naive", "selection_naive"])
def test_public_operations_revalidate_mutated_typed_clocks(function, mutation):
    data = StandardDataBundle.model_validate(payload())
    selected = request()
    if mutation == "metadata":
        data.membership[0].known_at = CUTOFF + DELTA
    elif mutation == "retrieval":
        data.provenance.retrieved_at = CUTOFF - DELTA
    elif mutation == "request_naive":
        data = data.model_copy(update={"request": data.request.model_copy(update={"as_of": CUTOFF.replace(tzinfo=None)})})
    else:
        selected = selected.model_copy(update={"as_of": CUTOFF.replace(tzinfo=None)})
    with pytest.raises(ValueError):
        function(data, selected, guard())


@pytest.mark.parametrize("function", OPERATIONS)
def test_later_known_metadata_and_future_bars_inside_snapshot_do_not_change_a_prefix(function):
    original = payload()
    changed = deepcopy(original)
    changed["membership"].append(dict(sid="SYN_A", effective_on=DAYS[1], member=False, known_at=CUTOFF))
    changed["corporate_actions"][0]["known_at"] = CUTOFF
    for bar in changed["bars"]:
        if bar["session"] == DAYS[-1]:
            for field in ("open", "high", "low", "close", "volume"):
                bar[field] *= 8.0
    selected = request(end=DAYS[1], as_of=at(DAYS[1]))
    assert function(StandardDataBundle.model_validate(original), selected, guard()) == function(StandardDataBundle.model_validate(changed), selected, guard())


def test_announced_future_effective_events_remain_valid_without_early_application():
    original = payload()
    changed = deepcopy(original)
    future = date(2024, 1, 5)
    known = at(DAYS[1], 12)
    changed["membership"].append(dict(sid="SYN_A", effective_on=future, member=False, known_at=known))
    changed["identities"][0].update(delisted_on=future, delisting_known_at=known)
    changed["tickers"][2].update(valid_to=DAYS[-1], valid_to_known_at=known)
    changed["tickers"].append(dict(sid="SYN_B", ticker="FUTURE_B", valid_from=future, known_at=known))
    changed["corporate_actions"].append(dict(sid="SYN_A", kind="DIVIDEND", effective_on=future, known_at=known, cash_per_share=1.0))
    before, after = StandardDataBundle.model_validate(original), StandardDataBundle.model_validate(changed)
    for function in OPERATIONS:
        assert function(before, request(), guard()) == function(after, request(), guard())
    assert [row.assets for row in historical_universe(after, request(), guard()).rows] == [ASSETS] * 3


@pytest.mark.parametrize("boundary", ["early", "at_close", "at_cutoff", "after_cutoff"])
def test_bar_availability_keeps_its_own_close_and_snapshot_boundaries(boundary):
    raw = payload()
    close = at(DAYS[0], 21)
    raw["bars"][0]["available_at"] = {"early": close - DELTA, "at_close": close,
                                         "at_cutoff": CUTOFF, "after_cutoff": CUTOFF + DELTA}[boundary]
    if boundary in {"early", "after_cutoff"}:
        with pytest.raises(ValueError, match="bar availability"):
            StandardDataBundle.model_validate(raw)
    else:
        assert StandardDataBundle.model_validate(raw).bars[0].available_at == raw["bars"][0]["available_at"]


def test_late_historical_bar_does_not_rewrite_past_quality_but_can_enter_later_liquidity():
    raw = payload()
    delayed = next(bar for bar in raw["bars"] if bar["sid"] == "SYN_A" and bar["session"] == DAYS[1])
    delayed["available_at"] = at(DAYS[2], 21, 4)
    data = StandardDataBundle.model_validate(raw)
    selected = request(end=DAYS[1], as_of=at(DAYS[1]))
    prefix = data_quality(data, selected, guard())
    full = data_quality(data, request(), guard())
    past = next(row for row in prefix.rows if row.sid == "SYN_A" and row.session == DAYS[1])
    assert past.present and not past.available and past.missing_fields == list(STANDARD_FIELDS)
    assert next(row for row in full.rows if row.sid == "SYN_A" and row.session == DAYS[1]) == past
    assert (DAYS[1], "SYN_A") not in {(row.session, row.asset) for row in to_s1_panel(data, selected, guard()).observations}
    assert historical_universe(data, request(), guard(), liquidity=LiquidityRule(lookback_sessions=2)).rows[-1].assets == ["SYN_B", "SYN_A"]


@pytest.mark.parametrize("function", OPERATIONS)
def test_scope_denial_precedes_payload_clock_inspection(function, monkeypatch):
    data = StandardDataBundle.model_validate(payload())
    data.membership[0].known_at = CUTOFF + DELTA
    def forbidden(*args, **kwargs):
        raise AssertionError("payload inspected before authorization")
    monkeypatch.setattr(StandardDataBundle, "model_dump", forbidden)
    with pytest.raises(ValueError, match="development interval"):
        function(data, request(start=date(2024, 1, 1)), guard())


def test_normalization_scope_denial_precedes_parser_even_with_invalid_clocks(monkeypatch):
    import bbag.s2_data as module
    def forbidden(*args, **kwargs):
        raise AssertionError("parser ran before authorization")
    monkeypatch.setattr(module.json, "loads", forbidden)
    with pytest.raises(ValueError, match="sealed"):
        standardize_daily_json(b"not JSON", request=request(classification="SEALED_FINAL"), guard=guard(),
            data_id="SYNTHETIC_DENIED", provider_data_version="v1", retrieved_at=CUTOFF - DELTA,
            calendar_id="SYNTHETIC", calendar_version="v1")


@pytest.mark.parametrize("operation", ["query", "quality", "cache_read", "cache_write"])
def test_public_snapshot_validator_returns_revalidated_original_and_subset(operation):
    data = StandardDataBundle.model_validate(payload())
    selected = request(end=DAYS[1], as_of=at(DAYS[1]), assets=["SYN_B"], fields=["close"])
    checked, checked_selection = validate_snapshot_selection(data, selected, guard(), operation=operation)
    assert checked == data and checked.request == request()
    assert checked_selection == selected
    assert checked.provenance.request_sha256 == canonical_request_sha256(request())
    with pytest.raises(ValueError, match="selection as_of"):
        validate_snapshot_selection(data, request(as_of=CUTOFF + DELTA), guard(), operation=operation)


@pytest.mark.parametrize("operation", ["cloud_dispatch", "export", "unknown", None, []])
def test_public_snapshot_validator_rejects_operations_outside_its_data_scope(operation):
    with pytest.raises(ValueError, match="snapshot selection operation"):
        validate_snapshot_selection(object(), object(), object(), operation=operation)


@pytest.mark.parametrize("operation", ["query", "quality", "cache_read", "cache_write"])
def test_public_snapshot_validator_authorizes_before_payload_for_every_operation(operation, monkeypatch):
    data = StandardDataBundle.model_validate(payload())
    def forbidden(*args, **kwargs):
        raise AssertionError("payload inspected before authorization")
    monkeypatch.setattr(StandardDataBundle, "model_dump", forbidden)
    with pytest.raises(ValueError, match="development interval"):
        validate_snapshot_selection(data, request(start=date(2024, 1, 1)), guard(), operation=operation)
