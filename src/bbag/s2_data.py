"""Bounded raw USA daily data and point-in-time synthetic engineering helpers.

Prices and volumes are unadjusted. Corporate actions are separate metadata, not
a total-return series. Licensed cloud bundles may only be processed in cloud;
the local gateway does not accept this payload or expose its source-data hash.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator

from .contracts import NonNegativeFloat, PositiveFloat, ProtocolDate, Sha256, StrictContract, Text
from .s1_contracts import MarketPanel, Observation, Session, utc
from .s2_access import AccessGuard, DataClassification, DataRequest, Provider, STANDARD_FIELDS


def canonical_request_sha256(request: DataRequest) -> str:
    if not isinstance(request, DataRequest):
        raise ValueError("request must be a DataRequest")
    checked = DataRequest.model_validate(request.model_dump(mode="python"))
    raw = json.dumps(checked.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class CalendarSession(Session):
    """An explicit supplied exchange session; no inferred weekday filling."""


class SecurityIdentity(StrictContract):
    sid: Text
    listed_from: ProtocolDate
    known_at: AwareDatetime
    delisted_on: ProtocolDate | None = None
    delisting_known_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.delisted_on is None) != (self.delisting_known_at is None):
            raise ValueError("delisting date and its knowledge time must appear together")
        if self.delisted_on is not None and self.delisted_on < self.listed_from:
            raise ValueError("delisting cannot precede listing")
        return self


class TickerMapping(StrictContract):
    sid: Text
    ticker: Text
    valid_from: ProtocolDate
    valid_to: ProtocolDate | None = None
    known_at: AwareDatetime
    valid_to_known_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("ticker validity must be ordered, inclusive dates")
        if (self.valid_to is None) != (self.valid_to_known_at is None):
            raise ValueError("ticker validity end and its knowledge time must appear together")
        return self


class MembershipEvent(StrictContract):
    sid: Text
    effective_on: ProtocolDate
    member: bool
    known_at: AwareDatetime


class CorporateAction(StrictContract):
    sid: Text
    kind: Literal["SPLIT", "DIVIDEND"]
    effective_on: ProtocolDate
    known_at: AwareDatetime
    shares_after_per_before: PositiveFloat | None = None
    cash_per_share: NonNegativeFloat | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.kind == "SPLIT":
            if self.shares_after_per_before is None or self.cash_per_share is not None:
                raise ValueError("a split requires only shares_after_per_before")
        elif self.cash_per_share is None or self.shares_after_per_before is not None:
            raise ValueError("a dividend requires only cash_per_share")
        return self


class DailyBar(StrictContract):
    sid: Text
    session: ProtocolDate
    open: PositiveFloat | None = None
    high: PositiveFloat | None = None
    low: PositiveFloat | None = None
    close: PositiveFloat | None = None
    volume: NonNegativeFloat | None = None
    available_at: AwareDatetime

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError("raw high must be at least raw low")
        for value in (self.open, self.close):
            if value is not None and ((self.high is not None and value > self.high) or (self.low is not None and value < self.low)):
                raise ValueError("raw open/close must be within supplied low/high")
        return self


class DataProvenance(StrictContract):
    provider: Provider
    provider_data_version: Text
    normalization: Literal["raw"] = "raw"
    price_semantics: Literal["unadjusted_trade_price"] = "unadjusted_trade_price"
    volume_semantics: Literal["unadjusted_shares"] = "unadjusted_shares"
    source_sha256: Sha256 = Field(description="SHA-256 of original provider input bytes; licensed data hashes remain inside cloud and never enter control metadata")
    request_sha256: Sha256
    retrieved_at: AwareDatetime
    classification: DataClassification = "UNKNOWN"
    derived_from: list[DataClassification] = Field(default_factory=list, max_length=32)


class StandardDataBundle(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    data_id: Text
    market: Literal["USA"] = "USA"
    frequency: Literal["daily"] = "daily"
    currency: Literal["USD"] = "USD"
    timezone: Text = "America/New_York"
    calendar_id: Text
    calendar_version: Text
    request: DataRequest
    provenance: DataProvenance
    sessions: list[CalendarSession] = Field(min_length=1, max_length=256)
    identities: list[SecurityIdentity] = Field(min_length=1, max_length=32)
    tickers: list[TickerMapping] = Field(min_length=1, max_length=256)
    membership: list[MembershipEvent] = Field(default_factory=list, max_length=1024)
    corporate_actions: list[CorporateAction] = Field(default_factory=list, max_length=1024)
    bars: list[DailyBar] = Field(max_length=8192)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        try:
            zone = ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("bundle timezone must identify an available IANA zone") from exc
        if self.provenance.provider != self.request.provider or self.provenance.request_sha256 != canonical_request_sha256(self.request):
            raise ValueError("provenance must bind the exact original request and provider")
        if self.provenance.classification != self.request.classification or self.provenance.derived_from != self.request.derived_from:
            raise ValueError("request and provenance classification/ancestry must match")
        snapshot_cutoff = utc(self.request.as_of)
        if utc(self.provenance.retrieved_at) < snapshot_cutoff:
            raise ValueError("snapshot retrieval cannot precede the original request as_of")
        knowledge_times = [item.known_at for collection in (self.identities, self.tickers, self.membership, self.corporate_actions)
                           for item in collection]
        knowledge_times.extend(item.delisting_known_at for item in self.identities if item.delisting_known_at is not None)
        knowledge_times.extend(item.valid_to_known_at for item in self.tickers if item.valid_to_known_at is not None)
        if any(utc(value) > snapshot_cutoff for value in knowledge_times):
            raise ValueError("snapshot metadata knowledge cannot follow the original request as_of")
        days = [item.session for item in self.sessions]
        if days != sorted(set(days)):
            raise ValueError("calendar sessions must be unique and ordered")
        for item in self.sessions:
            if not self.request.start <= item.session <= self.request.end:
                raise ValueError("calendar is outside the original request interval")
            if item.open_at.astimezone(zone).date() != item.session or item.close_at.astimezone(zone).date() != item.session:
                raise ValueError("calendar dates must match the market timezone")
        for current, following in zip(self.sessions, self.sessions[1:]):
            if utc(current.signal_at) >= utc(following.open_at):
                raise ValueError("each signal must precede the next session open")
        identities = {item.sid: item for item in self.identities}
        if len(identities) != len(self.identities) or set(identities) != set(self.request.assets):
            raise ValueError("identities must contain exactly the original request SIDs")
        for collection in (self.tickers, self.membership, self.corporate_actions):
            if any(item.sid not in identities for item in collection):
                raise ValueError("metadata contains an unrequested SID")
        if set(item.sid for item in self.tickers) != set(identities):
            raise ValueError("each SID needs explicit ticker history")
        for sid in identities:
            mappings = sorted((item for item in self.tickers if item.sid == sid), key=lambda item: item.valid_from)
            for left, right in zip(mappings, mappings[1:]):
                if left.valid_to is None or left.valid_to >= right.valid_from:
                    raise ValueError("ticker validity intervals for a SID must not overlap")
        # A ticker may be reused for a different SID only in disjoint intervals.
        for index, left in enumerate(self.tickers):
            for right in self.tickers[index + 1:]:
                if left.ticker == right.ticker and left.sid != right.sid and (left.valid_to is None or right.valid_from <= left.valid_to) and (right.valid_to is None or left.valid_from <= right.valid_to):
                    raise ValueError("one ticker cannot identify two SIDs on the same date")
        member_keys = [(item.sid, item.effective_on) for item in self.membership]
        action_keys = [(item.sid, item.kind, item.effective_on) for item in self.corporate_actions]
        if len(member_keys) != len(set(member_keys)) or len(action_keys) != len(set(action_keys)):
            raise ValueError("duplicate or in-place revised metadata events are not supported")
        by_day = {item.session: item for item in self.sessions}
        seen: set[tuple[date, str]] = set()
        for bar in self.bars:
            key = (bar.session, bar.sid)
            if key in seen or bar.session not in by_day or bar.sid not in identities:
                raise ValueError("bars must have unique declared session/SID keys")
            seen.add(key)
            identity = identities[bar.sid]
            if bar.session < identity.listed_from or (identity.delisted_on is not None and bar.session >= identity.delisted_on):
                raise ValueError("bars must fall in the actual listed interval; delisted_on is exclusive")
            if (utc(bar.available_at) < utc(by_day[bar.session].close_at)
                    or utc(bar.available_at) > utc(self.provenance.retrieved_at)
                    or utc(bar.available_at) > utc(self.request.as_of)):
                raise ValueError("bar availability must fall between session close and request/retrieval cutoff")
            if any(getattr(bar, field) is not None for field in STANDARD_FIELDS if field not in self.request.fields):
                raise ValueError("bars must not contain unrequested market fields")
        return self


def _checked(bundle: StandardDataBundle, request: DataRequest, guard: AccessGuard, operation: str) -> tuple[StandardDataBundle, DataRequest]:
    if not isinstance(guard, AccessGuard):
        raise ValueError("guard must be an AccessGuard")
    # Authorize before reading/revalidating the data payload or computing anything.
    checked_request = guard.authorize(request, operation=operation)
    if not isinstance(bundle, StandardDataBundle):
        raise ValueError("bundle must be a StandardDataBundle")
    checked_bundle = StandardDataBundle.model_validate(bundle.model_dump(mode="python"))
    guard.authorize_artifact(checked_request, classification=checked_bundle.provenance.classification,
                             derived_from=checked_bundle.provenance.derived_from, operation=operation)
    guard.authorize(checked_bundle.request, operation=operation)
    original = checked_bundle.request
    if (checked_request.provider != original.provider or checked_request.purpose != original.purpose
            or checked_request.start < original.start or checked_request.end > original.end
            or not set(checked_request.assets).issubset(original.assets)
            or not set(checked_request.fields).issubset(original.fields)):
        raise ValueError("selection must be a subset of the original data request")
    if utc(checked_request.as_of) > utc(original.as_of):
        raise ValueError("selection as_of must not exceed the original snapshot as_of")
    if checked_bundle.timezone != guard.policy.timezone:
        raise ValueError("bundle and access policy must use the same market timezone")
    return checked_bundle, checked_request


def validate_snapshot_selection(
    bundle: StandardDataBundle, request: DataRequest, guard: AccessGuard, *,
    operation: Literal["query", "quality", "cache_read", "cache_write"] = "query",
) -> tuple[StandardDataBundle, DataRequest]:
    """Authorize and revalidate an in-memory snapshot and its bounded selection.

    A caller performing I/O must also authorize the request before reading the
    payload. This function performs no transport, cache read or write itself.
    """
    if type(operation) is not str or operation not in {"query", "quality", "cache_read", "cache_write"}:
        raise ValueError("unknown snapshot selection operation")
    return _checked(bundle, request, guard, operation)


def _sessions(bundle: StandardDataBundle, request: DataRequest) -> list[CalendarSession]:
    return [item for item in bundle.sessions if request.start <= item.session <= request.end and utc(item.signal_at) <= utc(request.as_of)]


def _ticker(bundle: StandardDataBundle, sid: str, session: date, decision_at: datetime) -> str | None:
    known = [item for item in bundle.tickers if item.sid == sid and item.valid_from <= session
             and (item.valid_to is None or session <= item.valid_to or utc(item.valid_to_known_at) > utc(decision_at))
             and utc(item.known_at) <= utc(decision_at)]
    # A newly known mapping supersedes an old, not-yet-disclosed end boundary.
    return max(known, key=lambda item: item.valid_from).ticker if known else None


def _members(bundle: StandardDataBundle, request: DataRequest, session: CalendarSession) -> list[str]:
    result: list[str] = []
    for identity in sorted(bundle.identities, key=lambda item: item.sid):
        sid = identity.sid
        if sid not in request.assets or identity.listed_from > session.session or utc(identity.known_at) > utc(session.signal_at):
            continue
        if (identity.delisted_on is not None and session.session >= identity.delisted_on
                and utc(identity.delisting_known_at) <= utc(session.signal_at)):
            continue
        events = [item for item in bundle.membership if item.sid == sid and item.effective_on <= session.session
                  and utc(item.known_at) <= utc(session.signal_at)]
        latest = max(events, key=lambda item: item.effective_on, default=None)
        if latest is not None and latest.member and _ticker(bundle, sid, session.session, session.signal_at) is not None:
            result.append(sid)
    return result


class UniverseRow(StrictContract):
    session: ProtocolDate
    signal_at: AwareDatetime
    assets: list[Text] = Field(max_length=32)


class UniverseResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    data_id: Text
    as_of: AwareDatetime
    rows: list[UniverseRow] = Field(max_length=256)


class LiquidityRule(StrictContract):
    lookback_sessions: Annotated[int, Field(strict=True, ge=1, le=128)] = 2
    minimum_mean_raw_dollar_volume: NonNegativeFloat = 0.0
    max_assets: Annotated[int, Field(strict=True, ge=1, le=32)] = 32
    lag_sessions: Literal[1] = 1
    missing: Literal["exclude_full_window"] = "exclude_full_window"
    tie_break: Literal["sid_ascending"] = "sid_ascending"

    @model_validator(mode="before")
    @classmethod
    def strict_lag(cls, value: object) -> object:
        if isinstance(value, dict) and "lag_sessions" in value and type(value["lag_sessions"]) is not int:
            raise ValueError("lag_sessions must be integer 1")
        return value


def historical_universe(bundle: StandardDataBundle, request: DataRequest, guard: AccessGuard, *, liquidity: LiquidityRule | None = None) -> UniverseResult:
    bundle, request = _checked(bundle, request, guard, "query")
    if liquidity is not None and not isinstance(liquidity, LiquidityRule):
        raise ValueError("liquidity must be a LiquidityRule")
    rule = LiquidityRule.model_validate(liquidity.model_dump(mode="python")) if liquidity is not None else None
    if rule is not None and not {"close", "volume"}.issubset(request.fields):
        raise ValueError("liquidity requires explicitly requested close and volume")
    bars = {(item.session, item.sid): item for item in bundle.bars}
    indices = {item.session: index for index, item in enumerate(bundle.sessions)}
    rows: list[UniverseRow] = []
    for session in _sessions(bundle, request):
        assets = _members(bundle, request, session)
        if rule is not None:
            index = indices[session.session]
            window = bundle.sessions[max(0, index - rule.lookback_sessions):index]
            scores: list[tuple[float, str]] = []
            for sid in assets:
                values: list[float] = []
                if len(window) != rule.lookback_sessions:
                    continue
                for previous in window:
                    bar = bars.get((previous.session, sid))
                    if (bar is None or utc(bar.available_at) > utc(session.signal_at)
                            or bar.close is None or bar.volume is None):
                        break
                    value = bar.close * bar.volume
                    if not math.isfinite(value):
                        break
                    values.append(value)
                if len(values) == rule.lookback_sessions:
                    mean = math.fsum(value / rule.lookback_sessions for value in values)
                    if math.isfinite(mean) and mean >= rule.minimum_mean_raw_dollar_volume:
                        scores.append((mean, sid))
            assets = [sid for _, sid in sorted(scores, key=lambda item: (-item[0], item[1]))[:rule.max_assets]]
        rows.append(UniverseRow(session=session.session, signal_at=session.signal_at, assets=assets))
    return UniverseResult(data_id=bundle.data_id, as_of=request.as_of, rows=rows)


class ActionEntitlement(StrictContract):
    shares_after: NonNegativeFloat
    cash_distribution: NonNegativeFloat


def action_entitlement(action: CorporateAction, shares_before: float, *, as_of: datetime, timezone: str = "America/New_York") -> ActionEntitlement:
    """Explicit single-event position entitlement, not a price/return adjustment."""
    if not isinstance(action, CorporateAction) or type(shares_before) is not float or not math.isfinite(shares_before) or shares_before < 0:
        raise ValueError("a typed action and finite nonnegative float share count are required")
    checked = CorporateAction.model_validate(action.model_dump(mode="python"))
    try:
        zone = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError, TypeError) as exc:
        raise ValueError("an available IANA timezone is required") from exc
    if utc(checked.known_at) > utc(as_of) or utc(as_of).astimezone(zone).date() < checked.effective_on:
        raise ValueError("the corporate action is not yet known and effective")
    shares = shares_before * checked.shares_after_per_before if checked.kind == "SPLIT" else shares_before
    cash = shares_before * checked.cash_per_share if checked.kind == "DIVIDEND" else 0.0
    if not math.isfinite(shares) or not math.isfinite(cash):
        raise ValueError("corporate action entitlement overflow")
    return ActionEntitlement(shares_after=shares, cash_distribution=cash)


class QualityRule(StrictContract):
    stale_after_sessions: Annotated[int, Field(strict=True, ge=0, le=256)] = 1
    maximum_absolute_raw_close_change: Annotated[float, Field(strict=True, gt=0, le=1000, allow_inf_nan=False)] = 0.5
    raw_change_is_economic_signal: Literal[False] = False

    @model_validator(mode="before")
    @classmethod
    def strict_flag(cls, value: object) -> object:
        if isinstance(value, dict) and "raw_change_is_economic_signal" in value and type(value["raw_change_is_economic_signal"]) is not bool:
            raise ValueError("raw_change_is_economic_signal must be the boolean false")
        return value


class QualityRow(StrictContract):
    session: ProtocolDate
    sid: Text
    present: bool
    available: bool
    missing_fields: list[Literal["open", "high", "low", "close", "volume"]] = Field(max_length=5)
    stale: bool
    zero_volume: bool
    raw_close_jump: bool
    known_action_on_session: bool


class QualityReport(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    data_id: Text
    as_of: AwareDatetime
    expected_bars: Annotated[int, Field(strict=True, ge=0)]
    available_bars: Annotated[int, Field(strict=True, ge=0)]
    complete_bars: Annotated[int, Field(strict=True, ge=0)]
    coverage: Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
    rows: list[QualityRow] = Field(max_length=8192)


def data_quality(bundle: StandardDataBundle, request: DataRequest, guard: AccessGuard, *, rule: QualityRule | None = None) -> QualityReport:
    bundle, request = _checked(bundle, request, guard, "quality")
    if rule is not None and not isinstance(rule, QualityRule):
        raise ValueError("rule must be a QualityRule")
    checked_rule = QualityRule.model_validate(rule.model_dump(mode="python")) if rule is not None else QualityRule()
    bars = {(item.session, item.sid): item for item in bundle.bars}
    indices = {item.session: index for index, item in enumerate(bundle.sessions)}
    rows: list[QualityRow] = []
    for session in _sessions(bundle, request):
        index = indices[session.session]
        # Coverage denominator is the point-in-time member set, not survivors.
        for sid in _members(bundle, request, session):
            bar = bars.get((session.session, sid))
            available = bar is not None and utc(bar.available_at) <= utc(session.signal_at)
            missing = [field for field in request.fields if not available or getattr(bar, field) is None]
            history = [(i, bars.get((previous.session, sid))) for i, previous in enumerate(bundle.sessions[:index])]
            valid_history = [(i, item) for i, item in history if item is not None and item.close is not None and utc(item.available_at) <= utc(session.signal_at)]
            previous = valid_history[-1] if valid_history else None
            complete_history = [i for i, item in history if item is not None and utc(item.available_at) <= utc(session.signal_at)
                                and all(getattr(item, field) is not None for field in request.fields)]
            age = 0 if available and not missing else index - complete_history[-1] if complete_history else None
            jump = (available and bar.close is not None and previous is not None
                    and abs(bar.close / previous[1].close - 1.0) > checked_rule.maximum_absolute_raw_close_change)
            known_action = any(item.sid == sid and item.effective_on == session.session and utc(item.known_at) <= utc(session.signal_at) for item in bundle.corporate_actions)
            rows.append(QualityRow(session=session.session, sid=sid, present=bar is not None, available=available,
                missing_fields=missing, stale=age is None or age > checked_rule.stale_after_sessions,
                zero_volume=available and bar.volume == 0, raw_close_jump=bool(jump), known_action_on_session=known_action))
    available_count = sum(item.available for item in rows)
    complete = sum(not item.missing_fields for item in rows)
    return QualityReport(data_id=bundle.data_id, as_of=request.as_of, expected_bars=len(rows), available_bars=available_count,
                         complete_bars=complete, coverage=complete / len(rows) if rows else 0.0, rows=rows)


def to_s1_panel(bundle: StandardDataBundle, request: DataRequest, guard: AccessGuard) -> MarketPanel:
    bundle, request = _checked(bundle, request, guard, "query")
    if request.provider != "SYNTHETIC":
        raise ValueError("accepted S1 MarketPanel supports synthetic fixtures only")
    sessions = _sessions(bundle, request)
    if not sessions:
        raise ValueError("no completed signal sessions exist at as_of")
    by_day = {item.session: item for item in sessions}
    observations = [Observation(session=bar.session, asset=bar.sid, available_at=bar.available_at,
                    values={field: getattr(bar, field) for field in request.fields})
                    for bar in sorted(bundle.bars, key=lambda item: (item.session, item.sid))
                    if bar.session in by_day and bar.sid in request.assets and utc(bar.available_at) <= utc(request.as_of)]
    return MarketPanel(data_id=bundle.data_id, source="synthetic_fixture", timezone=bundle.timezone,
        sessions=[Session.model_validate(item.model_dump(mode="python")) for item in sessions], assets=sorted(request.assets),
        fields=list(request.fields), observations=observations)


class RawDailyInput(StrictContract):
    """Exact raw adapter envelope; numeric values are copied without adjustment."""
    sessions: list[CalendarSession] = Field(min_length=1, max_length=256)
    identities: list[SecurityIdentity] = Field(min_length=1, max_length=32)
    tickers: list[TickerMapping] = Field(min_length=1, max_length=256)
    membership: list[MembershipEvent] = Field(default_factory=list, max_length=1024)
    corporate_actions: list[CorporateAction] = Field(default_factory=list, max_length=1024)
    bars: list[DailyBar] = Field(max_length=8192)


def standardize_daily_json(raw_source: bytes, *, request: DataRequest, guard: AccessGuard, data_id: str,
                           provider_data_version: str, retrieved_at: datetime, calendar_id: str,
                           calendar_version: str, timezone: str = "America/New_York") -> StandardDataBundle:
    """Bind exact input bytes to raw fields. QC invocation is cloud-internal only.

    This is not a transport or data export entry point. Local adapters only use
    synthetic bytes; the gateway control contract cannot carry this envelope.
    """
    if not isinstance(guard, AccessGuard):
        raise ValueError("guard must be an AccessGuard")
    checked_request = guard.authorize(request, "query")
    if type(raw_source) is not bytes or not 1 <= len(raw_source) <= 4_000_000:
        raise ValueError("raw source must be bounded UTF-8 JSON bytes")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON keys are forbidden in raw input")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError("non-finite JSON constants are forbidden in raw input")

    def finite_number(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON numbers are forbidden in raw input")
        return number

    try:
        decoded = json.loads(raw_source.decode("utf-8"), object_pairs_hook=unique_object,
                             parse_constant=reject_constant, parse_float=finite_number)
    except (UnicodeDecodeError, RecursionError) as exc:
        raise ValueError("raw source must be bounded UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("raw source must be a JSON object")
    source = RawDailyInput.model_validate_json(raw_source)
    return StandardDataBundle(data_id=data_id, calendar_id=calendar_id, calendar_version=calendar_version, timezone=timezone,
        request=checked_request, provenance=DataProvenance(provider=checked_request.provider, provider_data_version=provider_data_version,
            source_sha256=hashlib.sha256(raw_source).hexdigest(), request_sha256=canonical_request_sha256(checked_request),
            retrieved_at=retrieved_at, classification=checked_request.classification, derived_from=checked_request.derived_from),
        **source.model_dump(mode="python"))


S2_DATA_CONTRACTS = {model.__name__: model for model in (
    CalendarSession, SecurityIdentity, TickerMapping, MembershipEvent, CorporateAction, DailyBar, DataProvenance,
    StandardDataBundle, UniverseRow, UniverseResult, LiquidityRule, ActionEntitlement, QualityRule, QualityRow, QualityReport, RawDailyInput,
)}
