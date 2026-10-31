"""S1 synthetic point-in-time inputs and deterministic engineering outputs."""
from __future__ import annotations

import json
import keyword
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, AwareDatetime, Field, StringConstraints, field_validator, model_validator

from .contracts import (
    FactorSpec, FiniteFloat, GitCommit, NonNegativeInt, PositiveInt, ProtocolDate, Sha256, StrictContract, Text,
)
from .jsonio import read_json_object

def _field_identifier(value: str) -> str:
    if keyword.iskeyword(value):
        raise ValueError("field identifiers must not be language keywords")
    return value


FieldName = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$"), AfterValidator(_field_identifier)]


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("an aware datetime is required")
    return value.astimezone(timezone.utc)


def unique(values: list[str], name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must be unique")


class Session(StrictContract):
    session: ProtocolDate
    open_at: AwareDatetime
    close_at: AwareDatetime
    signal_at: AwareDatetime

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if not utc(self.open_at) < utc(self.close_at) <= utc(self.signal_at):
            raise ValueError("session requires open_at < close_at <= signal_at")
        return self


class Observation(StrictContract):
    session: ProtocolDate
    asset: Text
    available_at: AwareDatetime
    values: dict[FieldName, FiniteFloat | None] = Field(min_length=1, max_length=16)


class MarketPanel(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    data_id: Text
    source: Literal["synthetic_fixture"] = "synthetic_fixture"
    timezone: Text
    sessions: list[Session] = Field(min_length=1, max_length=256)
    assets: list[Text] = Field(min_length=1, max_length=32)
    fields: list[FieldName] = Field(min_length=1, max_length=16)
    observations: list[Observation] = Field(max_length=8192)

    @model_validator(mode="after")
    def consistent_panel(self) -> Self:
        try:
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must identify an available IANA zone") from exc
        unique(self.assets, "assets")
        unique(self.fields, "fields")
        dates = [item.session for item in self.sessions]
        if dates != sorted(set(dates)):
            raise ValueError("sessions must be unique and strictly ordered by date")
        for item in self.sessions:
            if (item.open_at.astimezone(zone).date() != item.session
                    or item.close_at.astimezone(zone).date() != item.session):
                raise ValueError("session open/close dates must match the declared market timezone")
        for current, following in zip(self.sessions, self.sessions[1:]):
            if utc(current.signal_at) >= utc(following.open_at):
                raise ValueError("each signal must precede the next session open")
        by_date = {item.session: item for item in self.sessions}
        seen: set[tuple[date, str]] = set()
        for item in self.observations:
            key = (item.session, item.asset)
            if key in seen:
                raise ValueError("an asset/session observation must not be duplicated or revised in place")
            seen.add(key)
            if item.session not in by_date or item.asset not in self.assets:
                raise ValueError("observation session/asset is outside the declared panel")
            if set(item.values) != set(self.fields):
                raise ValueError("observation values must contain exactly the declared fields; use null for missing")
            if utc(item.available_at) < utc(by_date[item.session].close_at):
                raise ValueError("daily bar fields cannot be available before the session close")
        return self


class AuditLimits(StrictContract):
    max_nodes: Annotated[int, Field(strict=True, ge=1, le=256)] = 128
    max_depth: Annotated[int, Field(strict=True, ge=1, le=32)] = 16
    max_parameters: Annotated[int, Field(strict=True, ge=0, le=64)] = 16
    max_window: Annotated[int, Field(strict=True, ge=1, le=128)] = 64
    max_lookback_sessions: Annotated[int, Field(strict=True, ge=1, le=512)] = 256
    max_work_units: Annotated[int, Field(strict=True, ge=1, le=5_000_000)] = 1_000_000


class NumericPolicy(StrictContract):
    missing: Literal["propagate"] = "propagate"
    min_periods: Literal["full_window"] = "full_window"
    division_by_zero: Literal["missing"] = "missing"
    nonfinite_result: Literal["missing"] = "missing"
    std_ddof: Literal[0] = 0
    rank_ties: Literal["average"] = "average"
    rank_scale: Literal["zero_to_one"] = "zero_to_one"
    singleton_rank: Literal[0.5] = 0.5
    absolute_tolerance: Annotated[float, Field(strict=True, gt=0, le=1e-6, allow_inf_nan=False)] = 1e-12
    relative_tolerance: Annotated[float, Field(strict=True, gt=0, le=1e-6, allow_inf_nan=False)] = 1e-12

    @field_validator("std_ddof", mode="before")
    @classmethod
    def integer_ddof(cls, value: object) -> object:
        if type(value) is not int or value != 0:
            raise ValueError("std_ddof must be the integer 0")
        return value


class LabelRule(StrictContract):
    horizon_sessions: Annotated[int, Field(strict=True, ge=1, le=128)]
    entry_rule: Literal["next_session_open"] = "next_session_open"
    exit_rule: Literal["open_after_h_sessions"] = "open_after_h_sessions"
    return_type: Literal["price_return", "excess_price_return"] = "price_return"
    price_field: Literal["open"] = "open"
    benchmark_asset: Text | None = None

    @model_validator(mode="after")
    def benchmark_consistency(self) -> Self:
        if (self.return_type == "excess_price_return") != (self.benchmark_asset is not None):
            raise ValueError("excess_price_return requires a benchmark; price_return must not declare one")
        return self


class S1Config(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S1"] = "S1"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    panel: MarketPanel
    universe: list[Text] = Field(min_length=1, max_length=32)
    factor: FactorSpec
    audit_limits: AuditLimits = Field(default_factory=AuditLimits)
    numerics: NumericPolicy = Field(default_factory=NumericPolicy)
    label: LabelRule
    as_of: AwareDatetime
    pending_decisions: list[Text] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent_request(self) -> Self:
        unique(self.universe, "universe")
        unique(self.pending_decisions, "pending_decisions")
        if not set(self.universe).issubset(self.panel.assets):
            raise ValueError("universe must be contained in panel assets")
        if not set(self.factor.required_fields).issubset(self.panel.fields):
            raise ValueError("factor fields must exist in the panel")
        if self.label.price_field not in self.panel.fields:
            raise ValueError("label price_field must exist in the panel")
        if self.label.benchmark_asset is not None and (
                self.label.benchmark_asset not in self.panel.assets
                or self.label.benchmark_asset in self.universe):
            raise ValueError("benchmark must be a panel asset outside the factor/target universe")
        if self.factor.lookback_sessions > self.audit_limits.max_lookback_sessions:
            raise ValueError("declared lookback exceeds the engineering limit")
        return self


class AuditSummary(StrictContract):
    normalized_expression: Text
    fields: list[FieldName] = Field(min_length=1)
    lookback_sessions: PositiveInt
    node_count: PositiveInt
    max_depth: PositiveInt
    parameter_count: Annotated[int, Field(strict=True, ge=0)]

    @field_validator("fields")
    @classmethod
    def fields_are_unique(cls, value: list[str]) -> list[str]:
        unique(value, "audit fields")
        return value


class FactorValue(StrictContract):
    session: ProtocolDate
    asset: Text
    signal_at: AwareDatetime
    value: FiniteFloat | None


class FactorResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S1"] = "S1"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    data_id: Text
    factor_id: Text
    as_of: AwareDatetime
    mode: Literal["batch", "incremental"]
    audit: AuditSummary
    rows: list[FactorValue] = Field(max_length=8192)

    @model_validator(mode="after")
    def ordered_causal_rows(self) -> Self:
        keys = [(item.session, item.asset) for item in self.rows]
        if keys != sorted(set(keys)):
            raise ValueError("factor rows must be unique and ordered by session/asset")
        if any(utc(item.signal_at) > utc(self.as_of) for item in self.rows):
            raise ValueError("factor output signal cannot be later than as_of")
        return self


class LabelValue(StrictContract):
    session: ProtocolDate
    asset: Text
    signal_at: AwareDatetime
    entry_at: AwareDatetime | None
    exit_at: AwareDatetime | None
    mature_at: AwareDatetime | None
    value: FiniteFloat | None
    status: Literal["MATURED", "NOT_MATURED", "INSUFFICIENT_FUTURE", "MISSING_INPUT"]

    @model_validator(mode="after")
    def consistent_value(self) -> Self:
        if self.entry_at is not None and utc(self.signal_at) >= utc(self.entry_at):
            raise ValueError("label entry must follow the signal")
        if self.exit_at is not None and (
                self.entry_at is None or utc(self.entry_at) >= utc(self.exit_at)):
            raise ValueError("label exit must follow entry")
        if self.mature_at is not None and (
                self.exit_at is None or utc(self.mature_at) < utc(self.exit_at)):
            raise ValueError("label cannot mature before exit")
        if self.status == "MATURED":
            if self.value is None or self.entry_at is None or self.exit_at is None or self.mature_at is None:
                raise ValueError("MATURED requires value, entry, exit and maturity")
        elif self.value is not None:
            raise ValueError("only a MATURED label may disclose its value")
        return self


class LabelResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S1"] = "S1"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    data_id: Text
    as_of: AwareDatetime
    rule: LabelRule
    rows: list[LabelValue] = Field(max_length=8192)

    @model_validator(mode="after")
    def no_early_values(self) -> Self:
        keys = [(item.session, item.asset) for item in self.rows]
        if keys != sorted(set(keys)):
            raise ValueError("label rows must be unique and ordered by session/asset")
        for item in self.rows:
            if utc(item.signal_at) > utc(self.as_of):
                raise ValueError("label target signal is later than as_of")
            if item.status == "MATURED" and utc(item.mature_at) > utc(self.as_of):
                raise ValueError("a label value cannot be disclosed before maturity")
            if item.status == "NOT_MATURED" and item.mature_at is not None and utc(item.mature_at) <= utc(self.as_of):
                raise ValueError("NOT_MATURED cannot claim a maturity already reached")
        return self


class S1RunManifest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S1"] = "S1"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    run_id: Text
    kind: Literal["factors_batch", "factors_incremental", "labels"]
    config_sha256: Sha256
    result_sha256: Sha256
    code_commit: GitCommit | None
    working_tree_dirty: bool
    created_at: AwareDatetime
    platform_status: Literal["NOT_RUN"] = "NOT_RUN"
    economic_status: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    review_status: Literal["PENDING"] = "PENDING"

    @model_validator(mode="after")
    def known_source(self) -> Self:
        if self.code_commit is None and not self.working_tree_dirty:
            raise ValueError("unknown source must not be recorded as clean")
        return self


class S1ReviewReceipt(StrictContract):
    """S1 receipt consistency; cannot authenticate identities or grant Git actions."""
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S1"] = "S1"
    audited_commit: GitCommit
    reviewer: Text
    decision: Literal["PENDING", "APPROVED", "CHANGES_REQUIRED"] = "PENDING"
    engineering_status: Literal["PASS", "FAIL", "INCOMPLETE"] = "INCOMPLETE"
    platform_status: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    economic_status: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    blocking_findings: NonNegativeInt = 0
    evidence: list[Text] = Field(default_factory=list)
    reviewed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def consistent_decision(self) -> Self:
        unique(self.evidence, "review evidence")
        if self.decision == "APPROVED" and (
                self.engineering_status != "PASS" or self.blocking_findings != 0
                or not self.evidence or self.reviewed_at is None):
            raise ValueError("S1 APPROVED requires engineering PASS, zero blockers, evidence and an aware review time")
        return self


S1_REQUIRED_CHECKS = {
    "pip_check": 0, "pytest": 0, "audit": 0, "schemas_s0": 0, "schemas_s1": 0,
    "factors_batch": 0, "factors_incremental": 0, "labels": 0, "reference_check": 0,
    "pending_review_gate": 3,
}


class S1CheckRecord(StrictContract):
    name: Literal["pip_check", "pytest", "audit", "schemas_s0", "schemas_s1", "factors_batch",
                  "factors_incremental", "labels", "reference_check", "pending_review_gate"]
    command: list[Text] = Field(min_length=1)
    exit_code: int
    expected_exit_code: int
    passed: bool
    started_at_utc: AwareDatetime
    duration_seconds: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def consistent_check(self) -> Self:
        if self.expected_exit_code != S1_REQUIRED_CHECKS[self.name]:
            raise ValueError("check expected exit code does not match the fixed S1 verification protocol")
        if self.passed != (self.exit_code == self.expected_exit_code):
            raise ValueError("check passed flag must reflect the observed process exit code")
        return self


class S1Verification(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S1"] = "S1"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    implementation_commit: GitCommit
    working_tree_dirty: bool
    working_tree_dirty_start: bool
    working_tree_dirty_end: bool
    code_commit_end: GitCommit
    generated_at_utc: AwareDatetime
    python: Text
    python_executable: Text
    platform: Text
    dependency_lock_sha256: Sha256
    subprocess_output_encoding: Text
    checks: list[S1CheckRecord] = Field(min_length=10, max_length=10)
    developer_checks_passed: bool
    independent_review: Literal["PENDING"] = "PENDING"
    platform_integration: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    economic_evidence: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    next_stage_authorized: Literal[False] = False

    @model_validator(mode="after")
    def evidence_consistency(self) -> Self:
        names = [item.name for item in self.checks]
        unique(names, "verification checks")
        if set(names) != set(S1_REQUIRED_CHECKS):
            raise ValueError("the ten required S1 checks must be complete")
        if self.working_tree_dirty != (self.working_tree_dirty_start or self.working_tree_dirty_end):
            raise ValueError("working_tree_dirty must combine both source snapshots")
        actual = (all(item.passed for item in self.checks) and not self.working_tree_dirty
                  and self.implementation_commit == self.code_commit_end)
        if self.developer_checks_passed != actual:
            raise ValueError("developer result must reflect complete checks and unchanged clean source")
        return self

    @field_validator("next_stage_authorized", mode="before")
    @classmethod
    def no_numeric_authorization(cls, value: object) -> object:
        if type(value) is not bool or value is not False:
            raise ValueError("next_stage_authorized must be boolean false")
        return value


S1_CONTRACT_MODELS = {model.__name__: model for model in (
    Session, Observation, MarketPanel, AuditLimits, NumericPolicy, LabelRule,
    S1Config, AuditSummary, FactorValue, FactorResult, LabelValue, LabelResult, S1RunManifest,
    S1ReviewReceipt, S1CheckRecord, S1Verification,
)}


def load_s1_config(path: str | Path) -> S1Config:
    return S1Config.model_validate_json(json.dumps(read_json_object(path), allow_nan=False))


def canonical_s1_bytes(config: S1Config) -> bytes:
    checked = S1Config.model_validate(config.model_dump(mode="python"))
    return json.dumps(checked.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
