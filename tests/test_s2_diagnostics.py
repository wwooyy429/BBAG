"""Independent synthetic identity, quality and static-entitlement examples."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
import math

import pytest

from bbag.contracts import TimeRange
from bbag.s2_access import AccessGuard, AccessPolicy, DataRequest, STANDARD_FIELDS
from bbag.s2_data import QualityReport, StandardDataBundle, canonical_request_sha256
from bbag.s2_diagnostics import action_entitlement_chain, identity_audit, validate_quality_consistency


DAYS = [date(2024, 1, day) for day in (2, 3, 4, 5)]
SIDS = ["SYN_A", "SYN_B"]
TICK = timedelta(microseconds=1)


def at(day, hour=21, minute=5):
    return datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=hour, minutes=minute)


CUTOFF = at(DAYS[-1], 23, 0)
KNOWN = at(date(2024, 1, 1), 12, 0)


def request(**changes):
    values = dict(provider="SYNTHETIC", start=DAYS[0], end=DAYS[-1], assets=SIDS,
        fields=list(STANDARD_FIELDS), as_of=CUTOFF, classification="DEVELOPMENT")
    values.update(changes)
    return DataRequest(**values)


def guard():
    return AccessGuard(AccessPolicy(policy_id="SYNTHETIC_DIAGNOSTIC_TEST", provider="SYNTHETIC",
        development=TimeRange(start=DAYS[0], end=DAYS[-1]), allowed_assets=SIDS, allowed_fields=list(STANDARD_FIELDS)))


def payload():
    original = request()
    return dict(data_id="SYNTHETIC_DIAGNOSTIC_INPUT", calendar_id="SYNTHETIC_FOUR_DAYS", calendar_version="v1",
        request=original, provenance=dict(provider="SYNTHETIC", provider_data_version="synthetic-v1",
            source_sha256="b" * 64, request_sha256=canonical_request_sha256(original), retrieved_at=CUTOFF,
            classification="DEVELOPMENT"),
        sessions=[dict(session=day, open_at=at(day, 14, 30), close_at=at(day, 21, 0), signal_at=at(day)) for day in DAYS],
        identities=[dict(sid=sid, listed_from=DAYS[0], known_at=KNOWN) for sid in SIDS],
        tickers=[dict(sid=sid, ticker=("A" if sid == "SYN_A" else "B"), valid_from=DAYS[0], known_at=KNOWN) for sid in SIDS],
        membership=[dict(sid=sid, effective_on=DAYS[0], member=True, known_at=KNOWN) for sid in SIDS],
        corporate_actions=[], bars=[dict(sid=sid, session=day, open=10.0, high=11.0, low=9.0,
            close=10.0, volume=100.0, available_at=at(day, 21, 1)) for day in DAYS for sid in SIDS])


def bundle(raw=None):
    return StandardDataBundle.model_validate(payload() if raw is None else raw)


def row_for(result, day, sid="SYN_A"):
    return next(row for row in result.rows if row.session == day and row.sid == sid)


@pytest.mark.parametrize("kind,reason", [
    ("base", "ELIGIBLE"), ("identity", "IDENTITY_NOT_KNOWN"), ("listing", "NOT_YET_LISTED"),
    ("delisting", "DELISTED"), ("missing_member", "NO_KNOWN_MEMBERSHIP"),
    ("removed", "NOT_MEMBER"), ("ticker", "NO_KNOWN_TICKER"),
])
def test_identity_closed_reasons_have_literal_expected_outcomes(kind, reason):
    raw = payload()
    target_day = DAYS[0]
    if kind == "identity":
        raw["identities"][0]["known_at"] = at(DAYS[0]) + TICK
    elif kind == "listing":
        raw["identities"][0]["listed_from"] = DAYS[1]
        raw["bars"] = [bar for bar in raw["bars"] if not (bar["sid"] == "SYN_A" and bar["session"] == DAYS[0])]
    elif kind == "delisting":
        raw["identities"][0].update(delisted_on=DAYS[1], delisting_known_at=KNOWN)
        raw["bars"] = [bar for bar in raw["bars"] if bar["sid"] != "SYN_A" or bar["session"] < DAYS[1]]
        target_day = DAYS[1]
    elif kind == "missing_member":
        raw["membership"] = [event for event in raw["membership"] if event["sid"] != "SYN_A"]
    elif kind == "removed":
        raw["membership"][0]["member"] = False
    elif kind == "ticker":
        raw["tickers"][0]["known_at"] = at(DAYS[0]) + TICK
    observed = row_for(identity_audit(bundle(raw), request(), guard()), target_day)
    assert observed.reason == reason and observed.eligible == (reason == "ELIGIBLE")
    assert observed.ticker == ("A" if reason == "ELIGIBLE" else None)
    if reason in {"IDENTITY_NOT_KNOWN", "NOT_YET_LISTED", "DELISTED", "NO_KNOWN_MEMBERSHIP"}:
        assert observed.membership_effective_on is observed.membership_known_at is None


@pytest.mark.parametrize("target", ["identity", "ticker", "membership", "delisting"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_signal_knowledge_boundaries_do_not_expose_future_names_or_membership(target, offset):
    raw = payload()
    moment = at(DAYS[1]) + offset * TICK
    if target == "identity":
        raw["identities"][0]["known_at"] = moment
        late_reason = "IDENTITY_NOT_KNOWN"
    elif target == "ticker":
        raw["tickers"][0]["known_at"] = moment
        late_reason = "NO_KNOWN_TICKER"
    elif target == "membership":
        raw["membership"][0]["known_at"] = moment
        late_reason = "NO_KNOWN_MEMBERSHIP"
    else:
        raw["identities"][0].update(delisted_on=DAYS[1], delisting_known_at=moment)
        raw["bars"] = [bar for bar in raw["bars"] if bar["sid"] != "SYN_A" or bar["session"] < DAYS[1]]
        late_reason = "ELIGIBLE"
    result = identity_audit(bundle(raw), request(), guard())
    observed = row_for(result, DAYS[1])
    expected = late_reason if offset > 0 else "DELISTED" if target == "delisting" else "ELIGIBLE"
    assert observed.reason == expected
    assert observed.ticker == ("A" if expected == "ELIGIBLE" else None)
    assert all(row.membership_known_at is None or row.membership_known_at <= row.signal_at for row in result.rows)


def test_rename_waits_for_knowledge_and_never_backfills_new_ticker():
    raw = payload()
    raw["tickers"][0].update(ticker="OLD", valid_to=DAYS[1], valid_to_known_at=at(DAYS[3]))
    raw["tickers"].append(dict(sid="SYN_A", ticker="NEW", valid_from=DAYS[2], known_at=at(DAYS[3])))
    result = identity_audit(bundle(raw), request(), guard())
    assert [row_for(result, day).ticker for day in DAYS] == ["OLD", "OLD", "OLD", "NEW"]
    prefix = identity_audit(bundle(raw), request(end=DAYS[2], as_of=at(DAYS[2])), guard())
    assert [row.ticker for row in prefix.rows if row.sid == "SYN_A"] == ["OLD"] * 3


def test_announced_future_rename_and_member_removal_apply_only_when_effective():
    raw = payload()
    raw["tickers"][0].update(ticker="OLD", valid_to=DAYS[1], valid_to_known_at=KNOWN)
    raw["tickers"].append(dict(sid="SYN_A", ticker="NEW", valid_from=DAYS[2], known_at=KNOWN))
    raw["membership"].append(dict(sid="SYN_A", effective_on=DAYS[3], member=False, known_at=KNOWN))
    result = identity_audit(bundle(raw), request(), guard())
    assert [(row_for(result, day).reason, row_for(result, day).ticker) for day in DAYS] == [
        ("ELIGIBLE", "OLD"), ("ELIGIBLE", "OLD"), ("ELIGIBLE", "NEW"), ("NOT_MEMBER", None)]


def test_leave_and_late_known_rejoin_preserve_the_actual_known_event_trace():
    raw = payload()
    raw["membership"].extend([
        dict(sid="SYN_A", effective_on=DAYS[1], member=False, known_at=at(DAYS[1], 12, 0)),
        dict(sid="SYN_A", effective_on=DAYS[2], member=True, known_at=at(DAYS[3], 12, 0)),
    ])
    result = identity_audit(bundle(raw), request(), guard())
    assert [row_for(result, day).eligible for day in DAYS] == [True, False, False, True]
    assert [row_for(result, day).membership_effective_on for day in DAYS] == [DAYS[0], DAYS[1], DAYS[1], DAYS[2]]
    assert row_for(result, DAYS[2]).membership_known_at == at(DAYS[1], 12, 0)


def test_ticker_reuse_keeps_two_distinct_sids_and_delisting_is_exclusive():
    raw = payload()
    raw["identities"][0].update(delisted_on=DAYS[2], delisting_known_at=KNOWN)
    raw["identities"][1]["listed_from"] = DAYS[2]
    raw["tickers"][0].update(ticker="REUSED", valid_to=DAYS[1], valid_to_known_at=KNOWN)
    raw["tickers"][1].update(ticker="REUSED", valid_from=DAYS[2])
    raw["membership"][1]["effective_on"] = DAYS[2]
    raw["bars"] = [bar for bar in raw["bars"] if (bar["sid"] == "SYN_A") == (bar["session"] < DAYS[2])]
    result = identity_audit(bundle(raw), request(), guard())
    assert len(result.rows) == 8
    assert [(row.sid, row.ticker) for row in result.rows if row.eligible] == [("SYN_A", "REUSED")] * 2 + [("SYN_B", "REUSED")] * 2
    assert row_for(result, DAYS[2]).reason == "DELISTED"
    assert row_for(result, DAYS[1], "SYN_B").reason == "NOT_YET_LISTED"


def test_identity_selection_preserves_stable_sid_order_and_requested_prefix():
    selected = request(end=DAYS[1], assets=["SYN_B"], fields=["volume"], as_of=at(DAYS[1]))
    result = identity_audit(bundle(), selected, guard())
    assert [(row.session, row.sid, row.ticker) for row in result.rows] == [(DAYS[0], "SYN_B", "B"), (DAYS[1], "SYN_B", "B")]
    assert result.selection == "membership_before_optional_liquidity"
    assert identity_audit(bundle(), request(end=DAYS[0], as_of=at(DAYS[0], 21, 0)), guard()).rows == []


def split(day, ratio, known=KNOWN):
    return dict(sid="SYN_A", kind="SPLIT", effective_on=day, known_at=known, shares_after_per_before=float(ratio))


def dividend(day, amount, known=KNOWN):
    return dict(sid="SYN_A", kind="DIVIDEND", effective_on=day, known_at=known, cash_per_share=float(amount))


def chain(events, *, shares=10.0, selected=None):
    raw = payload()
    raw["corporate_actions"] = events
    return action_entitlement_chain(bundle(raw), selected or request(), guard(), sid="SYN_A", shares_before=shares)


@pytest.mark.parametrize("cash_first,expected_cash", [(False, Fraction(20)), (True, Fraction(10))])
def test_split_dividend_order_has_independent_exact_cash_answers(cash_first, expected_cash):
    events = [dividend(DAYS[1], 1), split(DAYS[2], 2)] if cash_first else [split(DAYS[1], 2), dividend(DAYS[2], 1)]
    result = chain(list(reversed(events)))  # provider order cannot alter effective-day order
    assert result.shares_before == 10.0 and result.shares_after == float(Fraction(10) * 2)
    assert result.cash_distribution == float(expected_cash)
    assert [step.kind for step in result.steps] == (["DIVIDEND", "SPLIT"] if cash_first else ["SPLIT", "DIVIDEND"])
    expected = [(10.0, 10.0, 10.0, 10.0), (10.0, 20.0, 0.0, 10.0)] if cash_first else [(10.0, 20.0, 0.0, 0.0), (20.0, 20.0, 20.0, 20.0)]
    assert [(step.shares_before, step.shares_after, step.cash_distribution, step.cumulative_cash) for step in result.steps] == expected
    assert result.semantics == "static_holding_entitlements_only"


def test_multiday_fraction_reference_tracks_shares_and_cash_at_each_event():
    events = [split(DAYS[0], Fraction(1, 2)), dividend(DAYS[1], Fraction(1, 4)),
              split(DAYS[2], 3), dividend(DAYS[3], Fraction(1, 2))]
    result = chain(events)
    assert [step.shares_after for step in result.steps] == [5.0, 5.0, 15.0, 15.0]
    assert [step.cash_distribution for step in result.steps] == [0.0, float(Fraction(5, 4)), 0.0, float(Fraction(15, 2))]
    assert result.shares_after == 15.0 and result.cash_distribution == float(Fraction(35, 4))


def test_known_late_actions_and_future_effective_actions_do_not_enter_earlier_selection():
    events = [split(DAYS[0], 2, known=at(DAYS[2], 12, 0)), dividend(DAYS[1], 1), dividend(DAYS[3], 2)]
    selected = request(end=DAYS[1], as_of=at(DAYS[1]))
    result = chain(events, selected=selected)
    assert [(step.kind, step.effective_on) for step in result.steps] == [("DIVIDEND", DAYS[1])]
    assert result.shares_after == 10.0 and result.cash_distribution == 10.0
    complete = chain(events)
    assert complete.shares_after == 20.0 and complete.cash_distribution == 60.0


def test_selected_start_defines_the_initial_static_holding_without_reapplying_old_split():
    result = chain([split(DAYS[0], 2), dividend(DAYS[2], 1)], selected=request(start=DAYS[2]))
    assert result.shares_after == 10.0 and result.cash_distribution == 10.0
    assert [step.effective_on for step in result.steps] == [DAYS[2]]


@pytest.mark.parametrize("events,shares", [([], 10.0), ([], 0.0), ([split(DAYS[1], 2), dividend(DAYS[2], 1)], 0.0), ([dividend(DAYS[1], 0)], 10.0)])
def test_empty_or_zero_holdings_and_zero_distributions_are_explicit(events, shares):
    result = chain(events, shares=shares)
    assert result.shares_after == shares and result.cash_distribution == 0.0
    assert len(result.steps) == len(events)


def test_same_day_mixed_actions_reject_but_an_unobserved_event_is_not_inferred():
    events = [split(DAYS[1], 2), dividend(DAYS[1], 1, known=at(DAYS[2], 12, 0))]
    earlier = chain(events, selected=request(end=DAYS[1], as_of=at(DAYS[1])))
    assert earlier.shares_after == 20.0 and earlier.cash_distribution == 0.0
    with pytest.raises(ValueError, match="same-day"):
        chain(events)


@pytest.mark.parametrize("events,shares", [
    ([split(DAYS[1], 2)], 1e308),
    ([dividend(DAYS[1], 1e308)], 2.0),
    ([dividend(DAYS[1], 1e308), dividend(DAYS[2], 1e308)], 1.0),
])
def test_share_per_event_cash_and_cumulative_cash_overflow_reject(events, shares):
    with pytest.raises(ValueError, match="overflow"):
        chain(events, shares=shares)


@pytest.mark.parametrize("shares", [True, 10, -1.0, float("nan"), float("inf"), "10", None])
def test_action_chain_rejects_untyped_or_invalid_holdings(shares):
    with pytest.raises(ValueError, match="holding"):
        chain([], shares=shares)


@pytest.mark.parametrize("sid", ["NOT_REQUESTED", [], None, 1])
def test_action_chain_sid_must_be_in_the_authorized_selection(sid):
    with pytest.raises(ValueError, match="SID"):
        action_entitlement_chain(bundle(), request(), guard(), sid=sid, shares_before=10.0)
    with pytest.raises(ValueError, match="SID"):
        action_entitlement_chain(bundle(), request(assets=["SYN_B"]), guard(), sid="SYN_A", shares_before=10.0)


def quality_payload():
    def row(day, sid, **changes):
        values = dict(session=day, sid=sid, present=True, available=True, missing_fields=[], stale=False,
                      zero_volume=False, raw_close_jump=False, known_action_on_session=False)
        values.update(changes)
        return values
    return dict(data_id="SYNTHETIC_QUALITY", as_of=CUTOFF, expected_bars=4, available_bars=2,
        complete_bars=1, coverage=0.25, rows=[row(DAYS[0], "SYN_A"), row(DAYS[0], "SYN_B", missing_fields=["volume"]),
        row(DAYS[1], "SYN_A", available=False, missing_fields=list(STANDARD_FIELDS), known_action_on_session=True),
        row(DAYS[1], "SYN_B", present=False, available=False, missing_fields=list(STANDARD_FIELDS), stale=True)])


def test_quality_accepts_legal_partial_missing_late_and_absent_rows_without_mutation():
    report = QualityReport.model_validate(quality_payload())
    before = report.model_dump(mode="python")
    result = validate_quality_consistency(report)
    assert result == report and result.coverage == float(Fraction(1, 4))
    assert report.model_dump(mode="python") == before
    assert result.rows[1].available and result.rows[1].missing_fields == ["volume"]
    assert result.rows[2].present and not result.rows[2].available and result.rows[2].known_action_on_session


def test_quality_empty_report_has_zero_counts_and_zero_coverage():
    report = QualityReport(data_id="SYNTHETIC_EMPTY", as_of=CUTOFF, expected_bars=0, available_bars=0,
                           complete_bars=0, coverage=0.0, rows=[])
    assert validate_quality_consistency(report) == report
    with pytest.raises(ValueError, match="summaries"):
        validate_quality_consistency(report.model_copy(update={"coverage": 1.0}))


@pytest.mark.parametrize("mutation,message", [
    ("duplicate_key", "unique session"), ("duplicate_missing", "missing fields must be unique"),
    ("available_absent", "must be present"), ("unavailable_complete", "unavailable"),
    ("unavailable_zero", "unavailable"), ("unavailable_jump", "unavailable"),
    ("missing_close_jump", "observed close"), ("missing_volume_zero", "observed volume"),
])
def test_quality_rejects_row_contradictions_before_relying_on_summaries(mutation, message):
    raw = quality_payload()
    if mutation == "duplicate_key":
        raw["rows"].append(deepcopy(raw["rows"][0]))
        raw.update(expected_bars=5, available_bars=3, complete_bars=2, coverage=0.4)
    elif mutation == "duplicate_missing":
        raw["rows"][1]["missing_fields"] = ["volume", "volume"]
    elif mutation == "available_absent":
        raw["rows"][0]["present"] = False
    elif mutation == "unavailable_complete":
        raw["rows"][2]["missing_fields"] = []
    elif mutation == "unavailable_zero":
        raw["rows"][2]["zero_volume"] = True
    elif mutation == "unavailable_jump":
        raw["rows"][2]["raw_close_jump"] = True
    elif mutation == "missing_close_jump":
        raw["rows"][1].update(missing_fields=["close"], raw_close_jump=True)
    else:
        raw["rows"][1]["zero_volume"] = True
    with pytest.raises(ValueError, match=message):
        validate_quality_consistency(QualityReport.model_validate(raw))


@pytest.mark.parametrize("changes", [{"expected_bars": 5}, {"available_bars": 3}, {"complete_bars": 2},
                                     {"coverage": 0.5}, {"coverage": math.nextafter(0.25, 1.0)}])
def test_quality_rejects_incorrect_counts_and_even_small_coverage_changes(changes):
    raw = quality_payload()
    raw.update(changes)
    with pytest.raises(ValueError, match="summaries"):
        validate_quality_consistency(QualityReport.model_validate(raw))


@pytest.mark.parametrize("bad", [{}, None, "report"])
def test_quality_requires_a_typed_report(bad):
    with pytest.raises(ValueError, match="typed quality"):
        validate_quality_consistency(bad)


def call_guarded(function, data, selected, selected_guard):
    kwargs = {"sid": "SYN_A", "shares_before": 10.0} if function is action_entitlement_chain else {}
    return function(data, selected, selected_guard, **kwargs)


@pytest.mark.parametrize("function", [identity_audit, action_entitlement_chain])
@pytest.mark.parametrize("changes", [{"classification": "SEALED_FINAL"}, {"derived_from": ["SEALED_FINAL"]},
                                    {"start": date(2024, 1, 1)}, {"assets": ["NOT_AUTHORIZED"]}, {"output_mode": "CONTROL_ONLY"}])
def test_diagnostics_guard_denial_precedes_any_payload_inspection(function, changes, monkeypatch):
    data = bundle()
    def forbidden(*args, **kwargs):
        raise AssertionError("payload read before authorization")
    monkeypatch.setattr(StandardDataBundle, "model_dump", forbidden)
    with pytest.raises(ValueError):
        call_guarded(function, data, request(**changes), guard())


@pytest.mark.parametrize("function", [identity_audit, action_entitlement_chain])
def test_diagnostics_validate_subsets_clock_artifact_taint_and_wrong_inputs(function):
    with pytest.raises(ValueError, match="selection as_of"):
        call_guarded(function, bundle(), request(as_of=CUTOFF + TICK), guard())
    tainted = bundle()
    tainted.provenance.derived_from.append("SEALED_FINAL")
    with pytest.raises(ValueError):
        call_guarded(function, tainted, request(), guard())
    for bad in ({}, None, "bundle"):
        with pytest.raises(ValueError):
            call_guarded(function, bad, request(), guard())
    with pytest.raises(ValueError):
        call_guarded(function, bundle(), {}, guard())
    with pytest.raises(ValueError):
        call_guarded(function, bundle(), request(), None)


def test_mutated_action_and_quality_objects_are_revalidated_and_inputs_are_not_repaired():
    raw = payload()
    raw["corporate_actions"] = [split(DAYS[1], 2)]
    data = bundle(raw)
    before = data.model_dump(mode="python")
    selected = request()
    action_entitlement_chain(data, selected, guard(), sid="SYN_A", shares_before=10.0)
    identity_audit(data, selected, guard())
    assert data.model_dump(mode="python") == before and selected == request()
    data.corporate_actions[0].sid = "UNREQUESTED"
    with pytest.raises(ValueError, match="unrequested SID"):
        action_entitlement_chain(data, selected, guard(), sid="SYN_A", shares_before=10.0)
    assert data.corporate_actions[0].sid == "UNREQUESTED"
    report = QualityReport.model_validate(quality_payload())
    report.rows.append(report.rows[0])
    with pytest.raises(ValueError, match="unique session"):
        validate_quality_consistency(report)
