"""Cloud-local daily data bridge; no local export, grant or automatic dispatch.

The injected resolver supplies explicit provider identity/knowledge evidence.
It is an application trust boundary, not authentication of provider assertions.
All market records and their hashes returned here must remain inside Cloud.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import math
from numbers import Real
from typing import Annotated, Callable, Iterable, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from .contracts import GitCommit, ProtocolDate, Sha256, StrictContract
from .jsonio import parse_json_object
from .s1_contracts import utc
from .s2_access import AccessGuard, DataRequest, STANDARD_FIELDS
from .s2_data import (
    CalendarSession, CorporateAction, DailyBar, DataProvenance, MembershipEvent,
    SecurityIdentity, StandardDataBundle, TickerMapping, canonical_request_sha256,
)

ExactText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
Alias = Literal["SPY", "AAPL"]
START, END = date(2020, 1, 2), date(2020, 1, 10)
ZONE = "America/New_York"


class CloudDataError(ValueError):
    """Closed error message; never echo market values or provider diagnostics."""


def _require(value: bool, message: str) -> None:
    if not value:
        raise CloudDataError(message)


def _canonical(model: StrictContract) -> bytes:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _checked(model, value):
    if type(value) is bytes:
        _require(0 < len(value) <= 4_000_000, "INVALID_PROVIDER_PAYLOAD")
        parse_json_object(value)  # Same duplicate/nonfinite/UTF-8 hooks as S0/S1.
        return model.model_validate_json(value)
    _require(isinstance(value, model), "TYPED_PROVIDER_PAYLOAD_REQUIRED")
    # A deliberately bypassed nested model must not echo market values in a
    # serializer warning before strict validation rejects it.
    return model.model_validate(value.model_dump(mode="python", warnings=False))


class CloudSourceBinding(StrictContract):
    project_id: Annotated[int, Field(strict=True, gt=0)]
    source_commit: GitCommit
    config_sha256: Sha256
    request_sha256: Sha256
    owner_policy_sha256: Sha256


class CloudDataPlan(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    # Existing recorded target, not proof of current ownership or availability.
    project_id: Literal[38698074]
    source_commit: GitCommit
    config_sha256: Sha256
    owner_policy_sha256: Sha256
    request: DataRequest
    max_history_rows: Annotated[int, Field(strict=True, ge=1, le=128)] = 32

    @field_validator("project_id", mode="before")
    @classmethod
    def exact_project(cls, value):
        if type(value) is not int or value != 38698074:
            raise ValueError("bridge is bounded to the original recorded project")
        return value

    @model_validator(mode="after")
    def bounded(self) -> Self:
        r = self.request
        if (r.provider != "QUANTCONNECT_CLOUD" or r.output_mode != "IN_CLOUD_DATA"
                or r.purpose != "ENGINEERING_TEST" or not START <= r.start <= r.end <= END
                or not set(r.assets) <= {"SPY", "AAPL"} or set(r.fields) != set(STANDARD_FIELDS)):
            raise ValueError("cloud plan must retain the fixed original alias/data scope")
        return self


def cloud_plan_binding(plan: CloudDataPlan) -> CloudSourceBinding:
    plan = _checked(CloudDataPlan, plan)
    return CloudSourceBinding(project_id=plan.project_id, source_commit=plan.source_commit,
        config_sha256=plan.config_sha256, request_sha256=canonical_request_sha256(plan.request),
        owner_policy_sha256=plan.owner_policy_sha256)


class CloudAliasBinding(StrictContract):
    alias: Alias
    sid: ExactText
    valid_from: ProtocolDate
    valid_to: ProtocolDate
    known_at: AwareDatetime

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.sid != self.sid.strip() or any(c in self.sid for c in "\r\n\0")
                or self.sid.upper() in {"SPY", "AAPL"} or self.valid_from > self.valid_to):
            raise ValueError("provider SID must be explicit and distinct from request aliases")
        return self


class CloudMetadataCoverage(StrictContract):
    identity_knowledge: Literal["PROVIDER_DOCUMENTED", "UNVERIFIED"]
    membership: Literal["NOT_PROVIDED", "PROVIDED_EMPTY", "PROVIDED_NONEMPTY"]
    corporate_actions: Literal["NOT_PROVIDED", "PROVIDED_EMPTY", "PROVIDED_NONEMPTY"]
    disclosure_revision: Literal["UNVERIFIED"] = "UNVERIFIED"
    independent_platform_review: Literal["PENDING"] = "PENDING"


class CloudResolution(StrictContract):
    binding: CloudSourceBinding
    identity_data_version: ExactText
    aliases: list[CloudAliasBinding] = Field(min_length=1, max_length=2)
    identities: list[SecurityIdentity] = Field(min_length=1, max_length=2)
    tickers: list[TickerMapping] = Field(min_length=1, max_length=32)
    membership: list[MembershipEvent] = Field(max_length=128)
    corporate_actions: list[CorporateAction] = Field(max_length=128)
    coverage: CloudMetadataCoverage

    @model_validator(mode="after")
    def coverage_matches_records(self) -> Self:
        for name in ("membership", "corporate_actions"):
            records, status = getattr(self, name), getattr(self.coverage, name)
            if bool(records) != (status == "PROVIDED_NONEMPTY"):
                raise ValueError("metadata coverage must describe actual supplied records")
        if (not self.identity_data_version.strip() or self.identity_data_version != self.identity_data_version.strip()
                or any(c in self.identity_data_version for c in "\r\n\0")
                or self.identity_data_version.casefold() in {"unknown", "unverified", "not_provided"}):
            raise ValueError("identity version must be explicit")
        return self


class CloudSnapshotContext(StrictContract):
    binding: CloudSourceBinding
    provider_data_version: ExactText
    calendar_id: ExactText
    calendar_version: ExactText
    retrieved_at: AwareDatetime
    sessions: list[CalendarSession] = Field(min_length=1, max_length=16)
    normalization: Literal["raw"] = "raw"
    availability_basis: Literal["PROVIDER_DOCUMENTED", "BAR_END_PROXY"]
    # Provider-documented availability is supplied explicitly per actual bar.
    availability: list[DailyBar] = Field(max_length=128)

    @field_validator("provider_data_version", "calendar_id", "calendar_version")
    @classmethod
    def explicit_provider_version(cls, value):
        if value != value.strip() or any(c in value for c in "\r\n\0") or value.casefold() in {"unknown", "unverified", "not_provided"}:
            raise ValueError("provider and calendar version evidence must be explicit")
        return value


class CloudDailySnapshot(CloudSnapshotContext):
    bars: list[DailyBar] = Field(max_length=128)


def _preflight(plan: CloudDataPlan, guard: AccessGuard) -> CloudDataPlan:
    plan = _checked(CloudDataPlan, plan)
    _require(isinstance(guard, AccessGuard) and not isinstance(guard, CloudMappingGuard), "ORIGINAL_ALIAS_GUARD_REQUIRED")
    _require(guard.execution_location == "QUANTCONNECT_CLOUD", "CLOUD_EXECUTION_REQUIRED")
    p = guard.policy
    _require(p.provider == "QUANTCONNECT_CLOUD" and p.authorization == "OWNER_AUTHORIZED"
             and p.timezone == ZONE and p.development.start == START and p.development.end == END
             and set(p.allowed_assets) == {"SPY", "AAPL"} and set(p.allowed_fields) == set(STANDARD_FIELDS)
             and p.allowed_purposes == ["ENGINEERING_TEST"] and guard.policy_sha256 == plan.owner_policy_sha256,
             "ORIGINAL_OWNER_SCOPE_REQUIRED")
    guard.authorize(plan.request, "query")  # Also checks the trusted digest registry.
    return plan


def _resolution(plan: CloudDataPlan, value) -> CloudResolution:
    value = _checked(CloudResolution, value)
    _require(value.binding == cloud_plan_binding(plan), "SOURCE_CONFIG_REQUEST_BINDING_MISMATCH")
    _require(value.coverage.identity_knowledge == "PROVIDER_DOCUMENTED", "IDENTITY_KNOWLEDGE_UNVERIFIED")
    aliases = {row.alias: row for row in value.aliases}
    sids = {row.sid for row in value.aliases}
    _require(len(aliases) == len(sids) == len(value.aliases)
             and set(aliases) == set(plan.request.assets), "AMBIGUOUS_ALIAS_SID_MAPPING")
    identities = {row.sid: row for row in value.identities}
    _require(len(identities) == len(value.identities) and set(identities) == sids, "IDENTITY_MAPPING_MISMATCH")
    cutoff = utc(plan.request.as_of)
    for alias, row in aliases.items():
        _require(row.valid_from <= plan.request.start <= plan.request.end <= row.valid_to
                 and utc(row.known_at) <= cutoff, "ALIAS_INTERVAL_OR_KNOWLEDGE_MISMATCH")
        matches = [t for t in value.tickers if t.sid == row.sid and t.ticker == alias
                   and t.valid_from <= plan.request.start and (t.valid_to is None or t.valid_to >= plan.request.end)
                   and utc(t.known_at) <= cutoff]
        _require(len(matches) == 1, "ALIAS_TICKER_HISTORY_MISMATCH")
    for collection in (value.identities, value.tickers, value.membership, value.corporate_actions):
        _require(all(row.sid in sids and utc(row.known_at) <= cutoff for row in collection), "UNBOUND_OR_FUTURE_METADATA")
    secondary_knowledge = [row.delisting_known_at for row in value.identities if row.delisting_known_at is not None]
    secondary_knowledge += [row.valid_to_known_at for row in value.tickers if row.valid_to_known_at is not None]
    _require(all(utc(when) <= cutoff for when in secondary_knowledge), "UNBOUND_OR_FUTURE_METADATA")
    _require(len({(row.sid, row.effective_on) for row in value.membership}) == len(value.membership)
             and len({(row.sid, row.kind, row.effective_on) for row in value.corporate_actions}) == len(value.corporate_actions),
             "DUPLICATE_PROVIDER_METADATA")
    for sid in sids:
        ticks = sorted((row for row in value.tickers if row.sid == sid), key=lambda row: row.valid_from)
        _require(all(a.valid_to is not None and a.valid_to < b.valid_from for a, b in zip(ticks, ticks[1:])),
                 "CONFLICTING_TICKER_INTERVALS")
    return value


class CloudMappingGuard(AccessGuard):
    """Narrow SID view of an existing alias grant, never a replacement policy.

    Every authorization maps back to the original alias request and rechecks its
    original trusted runtime guard. No SID is added to policy.allowed_assets.
    """

    def __init__(self, plan: CloudDataPlan, resolution: CloudResolution, guard: AccessGuard) -> None:
        checked = _preflight(plan, guard)
        self._plan_payload = _canonical(checked)
        self._resolution_payload = _canonical(_resolution(checked, resolution))
        self._origin = guard
        self._sealed = True

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("Cloud mapping context is immutable")
        object.__setattr__(self, name, value)

    @property
    def policy(self):
        return self._origin.policy

    @property
    def policy_sha256(self):
        return self._origin.policy_sha256

    @property
    def execution_location(self):
        return self._origin.execution_location

    def authorize(self, request: DataRequest, operation="query") -> DataRequest:
        plan = _preflight(_checked(CloudDataPlan, self._plan_payload), self._origin)
        resolution = _resolution(plan, self._resolution_payload)
        _require(type(operation) is str and operation in {"query", "quality"}, "MAPPING_OPERATION_DENIED")
        _require(isinstance(request, DataRequest), "TYPED_SID_REQUEST_REQUIRED")
        checked = DataRequest.model_validate(request.model_dump(mode="python", warnings=False))
        original = plan.request
        reverse = {row.sid: row.alias for row in resolution.aliases}
        _require(set(checked.assets) <= set(reverse) and checked.start >= original.start and checked.end <= original.end
                 and utc(checked.as_of) <= utc(original.as_of) and set(checked.fields) <= set(original.fields)
                 and checked.provider == original.provider and checked.purpose == original.purpose
                 and checked.output_mode == "IN_CLOUD_DATA" and checked.classification == original.classification
                 and checked.derived_from == original.derived_from, "DERIVED_SID_SCOPE_DENIED")
        payload = checked.model_dump(mode="python")
        payload["assets"] = [reverse[sid] for sid in checked.assets]
        self._origin.authorize(DataRequest.model_validate(payload), operation)
        return checked


@dataclass(frozen=True)
class CloudDataResult:
    """Cloud-local data, NOT a control response or an exportable report."""

    bundle: StandardDataBundle
    mapping_guard: CloudMappingGuard
    resolution: CloudResolution
    availability_basis: str
    resolution_payload: bytes = field(repr=False)
    snapshot_payload: bytes = field(repr=False)
    platform_integration: Literal["NOT_RUN"] = "NOT_RUN"
    independent_review: Literal["PENDING"] = "PENDING"


def _context(plan: CloudDataPlan, resolution: CloudResolution, context: CloudSnapshotContext) -> None:
    _require(context.binding == cloud_plan_binding(plan), "SOURCE_CONFIG_REQUEST_BINDING_MISMATCH")
    _require(utc(context.retrieved_at) >= utc(plan.request.as_of), "SNAPSHOT_CLOCK_DENIED")
    days = [row.session for row in context.sessions]
    _require(days == sorted(set(days)), "DUPLICATE_OR_UNORDERED_PROVIDER_SESSION")
    zone = ZoneInfo(ZONE)
    for row in context.sessions:
        _require(plan.request.start <= row.session <= plan.request.end
                 and row.open_at.astimezone(zone).date() == row.session
                 and row.close_at.astimezone(zone).date() == row.session, "PROVIDER_CALENDAR_SCOPE_DENIED")
    sids = {row.sid for row in resolution.aliases}
    by_day = {row.session: row for row in context.sessions}
    keys = [(row.session, row.sid) for row in context.availability]
    _require(len(keys) == len(set(keys)), "DUPLICATE_AVAILABILITY")
    for row in context.availability:
        _require(row.sid in sids and row.session in by_day
                 and utc(by_day[row.session].close_at) <= utc(row.available_at) <= utc(plan.request.as_of)
                 and not any(getattr(row, f) is not None for f in STANDARD_FIELDS), "PROVIDER_AVAILABILITY_DENIED")
    _require(context.availability_basis != "BAR_END_PROXY" or not context.availability, "BAR_END_PROXY_MUST_BE_EXPLICIT")


def _bundle(plan: CloudDataPlan, resolution: CloudResolution, snapshot, guard: AccessGuard) -> CloudDataResult:
    plan = _preflight(plan, guard)
    resolution = _resolution(plan, resolution)
    raw = snapshot if type(snapshot) is bytes else _canonical(_checked(CloudDailySnapshot, snapshot))
    snapshot = _checked(CloudDailySnapshot, raw)
    _context(plan, resolution, CloudSnapshotContext.model_validate(snapshot.model_dump(mode="python", exclude={"bars"})))
    _require(len(snapshot.bars) <= plan.max_history_rows and utc(snapshot.retrieved_at) >= utc(plan.request.as_of),
             "SNAPSHOT_ROWS_OR_CLOCK_DENIED")
    sids = {row.sid for row in resolution.aliases}
    payload = plan.request.model_dump(mode="python")
    payload["assets"] = [next(row.sid for row in resolution.aliases if row.alias == alias) for alias in plan.request.assets]
    derived = DataRequest.model_validate(payload)
    mapping = CloudMappingGuard(plan, resolution, guard)
    mapping.authorize(derived)
    _require({bar.sid for bar in snapshot.bars} <= sids, "UNREQUESTED_PROVIDER_IDENTITY")
    supplied = {(bar.session, bar.sid): bar for bar in snapshot.availability}
    _require(len(supplied) == len(snapshot.availability), "DUPLICATE_AVAILABILITY")
    for row in snapshot.availability:
        _require(not any(getattr(row, f) is not None for f in STANDARD_FIELDS), "AVAILABILITY_MUST_NOT_CARRY_PRICES")
    keys = {(bar.session, bar.sid) for bar in snapshot.bars}
    if snapshot.availability_basis == "PROVIDER_DOCUMENTED":
        _require(set(supplied) == keys and all(bar.available_at == supplied[(bar.session, bar.sid)].available_at for bar in snapshot.bars),
                 "PROVIDER_AVAILABILITY_REQUIRED")
    else:
        closes = {row.session: row.close_at for row in snapshot.sessions}
        _require(not supplied and all(bar.session in closes and utc(bar.available_at) == utc(closes[bar.session]) for bar in snapshot.bars),
                 "BAR_END_PROXY_MUST_BE_EXPLICIT")
    bundle = StandardDataBundle(data_id="CLOUD_INTERNAL_DAILY", timezone=ZONE,
        calendar_id=snapshot.calendar_id, calendar_version=snapshot.calendar_version, request=derived,
        provenance=DataProvenance(provider="QUANTCONNECT_CLOUD", provider_data_version=snapshot.provider_data_version,
            source_sha256=hashlib.sha256(_canonical(resolution) + b"\n" + raw).hexdigest(), request_sha256=canonical_request_sha256(derived),
            retrieved_at=snapshot.retrieved_at, classification=derived.classification, derived_from=derived.derived_from),
        sessions=snapshot.sessions, identities=resolution.identities, tickers=resolution.tickers,
        membership=resolution.membership, corporate_actions=resolution.corporate_actions, bars=snapshot.bars)
    return CloudDataResult(bundle, mapping, resolution, snapshot.availability_basis, _canonical(resolution), raw)


def revalidate_cloud_result(plan: CloudDataPlan, result: CloudDataResult, *, guard: AccessGuard) -> CloudDataResult:
    """Rebuild from private Cloud-local captured images before using results.

    This prevents model_copy/model_construct or later list mutation from turning
    a previously returned result into different data under the same provenance.
    It does not authenticate injected provider claims or the Python host.
    """
    _require(type(result) is CloudDataResult and type(result.mapping_guard) is CloudMappingGuard
             and result.mapping_guard._origin is guard, "ORIGINAL_RESULT_GUARD_REQUIRED")
    resolution = _resolution(_preflight(plan, guard), result.resolution_payload)
    rebuilt = _bundle(plan, resolution, result.snapshot_payload, guard)
    _require(_checked(CloudResolution, result.resolution) == resolution
             and _checked(StandardDataBundle, result.bundle) == rebuilt.bundle
             and result.availability_basis == rebuilt.availability_basis, "MUTATED_CLOUD_RESULT")
    return rebuilt


def build_cloud_bundle(plan: CloudDataPlan, *, guard: AccessGuard, resolver: Callable,
                       fetcher: Callable) -> CloudDataResult:
    """Call injected provider callbacks only after original-alias preflight.

    Resolver/fetcher assertions do not authenticate the provider or grant runtime
    permission. This explicit invocation itself must be separately authorized by
    the Cloud application; the module reserves no task and dispatches no tools.
    """
    plan = _preflight(plan, guard)
    _require(callable(resolver) and callable(fetcher), "PROVIDER_CALLBACK_REQUIRED")
    resolution = _resolution(plan, resolver(_checked(CloudDataPlan, plan)))
    _preflight(plan, guard)
    snapshot = fetcher(_checked(CloudDataPlan, plan), _checked(CloudResolution, resolution))
    return _bundle(plan, resolution, snapshot, guard)


@dataclass(frozen=True)
class SDKHistory:
    """Adapter-observed SDK subscription/query settings; data stays in Cloud."""

    bars: Iterable[object]
    normalization: str
    fill_forward: bool
    extended_market_hours: bool


def _number(value):
    _require(not isinstance(value, bool) and isinstance(value, (Real, Decimal)), "INVALID_SDK_NUMBER")
    number = float(value)
    _require(math.isfinite(number), "INVALID_SDK_NUMBER")
    return number


def extract_sdk_daily_snapshot(plan: CloudDataPlan, resolution: CloudResolution, context: CloudSnapshotContext,
                               *, guard: AccessGuard, history_factory: Callable) -> CloudDailySnapshot:
    """Normalize explicit native TradeBar fields, never fabricate metadata.

    All sessions/identity/availability/version knowledge comes from explicit
    provider evidence. Offline callers inject SYNTHETIC mock SDK objects only.
    The factory owns explicit typed History/RAW/no-fill setup. Relevant official
    SDK time/response semantics (no provider call is made by reading these):
    https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/history-responses
    https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/time-modeling/periods
    """
    plan = _preflight(plan, guard)
    resolution = _resolution(plan, resolution)
    context = _checked(CloudSnapshotContext, context)
    _context(plan, resolution, context)
    _require(callable(history_factory), "PROVIDER_CALLBACK_REQUIRED")
    history = history_factory(_checked(CloudDataPlan, plan), _checked(CloudResolution, resolution))
    _require(isinstance(history, SDKHistory) and history.normalization == "raw"
             and history.fill_forward is False and history.extended_market_hours is False, "SDK_RAW_SETTINGS_REQUIRED")
    by_day = {row.session: row for row in context.sessions}
    _require(len(by_day) == len(context.sessions), "DUPLICATE_PROVIDER_SESSION")
    sids = {row.sid for row in resolution.aliases}
    available = {(row.session, row.sid): row.available_at for row in context.availability}
    bars = []
    zone = ZoneInfo(ZONE)
    for native in history.bars:
        _require(len(bars) < plan.max_history_rows, "SDK_ROW_LIMIT")
        sid = str(native.symbol.id)
        _require(sid in sids and native.is_fill_forward is False, "SDK_IDENTITY_OR_FILL_FORWARD_DENIED")
        opening, closing = native.time, native.end_time
        _require(isinstance(opening, datetime) and isinstance(closing, datetime), "SDK_EVENT_TIME_REQUIRED")
        opening = opening.replace(tzinfo=zone) if opening.tzinfo is None else opening.astimezone(zone)
        closing = closing.replace(tzinfo=zone) if closing.tzinfo is None else closing.astimezone(zone)
        day = closing.date()
        _require(day in by_day and plan.request.start <= day <= plan.request.end
                 and opening.date() == day and utc(opening) <= utc(by_day[day].open_at)
                 and utc(closing) == utc(by_day[day].close_at), "SDK_SESSION_BOUNDARY_DENIED")
        when = closing if context.availability_basis == "BAR_END_PROXY" else available.get((day, sid))
        _require(when is not None, "PROVIDER_AVAILABILITY_REQUIRED")
        bars.append(DailyBar(sid=sid, session=day, available_at=when,
            **{field: _number(getattr(native, field)) for field in STANDARD_FIELDS}))
    snapshot = CloudDailySnapshot(**context.model_dump(mode="python"), bars=bars)
    _bundle(plan, resolution, snapshot, guard)  # Validate every metadata/time/data invariant before returning.
    return snapshot


S2_CLOUD_DATA_CONTRACTS = {model.__name__: model for model in (
    CloudSourceBinding, CloudDataPlan, CloudAliasBinding, CloudMetadataCoverage,
    CloudResolution, CloudSnapshotContext, CloudDailySnapshot,
)}
