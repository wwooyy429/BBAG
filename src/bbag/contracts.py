"""Strict stage-zero contracts; these models execute no research or trading."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path, PureWindowsPath
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .jsonio import read_json_object


Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
Sha256 = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[0-9a-fA-F]{64}$", to_lower=True)
]
GitCommit = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[0-9a-fA-F]{40}$", to_lower=True)
]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
ZeroBudget = Annotated[int, Field(strict=True, ge=0, le=0)]
FiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]
NonNegativeFloat = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
PositiveFloat = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
MetricName = Literal["rank_ic", "ic", "coverage", "turnover", "net_excess_return"]


def _parse_date(value: Any) -> date:
    # datetime is a date subclass, but discarding its time would hide a boundary error.
    if type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return date.fromisoformat(value)
    raise ValueError("expected a date or an ISO YYYY-MM-DD date string")


ProtocolDate = Annotated[date, BeforeValidator(_parse_date)]


def _unique(values: list[Any], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")


class StrictContract(BaseModel):
    """Disallow implicit scalar coercion, unknown keys and non-finite numbers."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_assignment=True,
        validate_default=True,
        revalidate_instances="always",
        allow_inf_nan=False,
    )


class TimeRange(StrictContract):
    """A closed date interval; both start and end are included."""

    start: ProtocolDate
    end: ProtocolDate

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start > self.end:
            raise ValueError("time range start must be on or before end")
        return self


class DataSplitSpec(StrictContract):
    development: TimeRange
    final_test: TimeRange

    @model_validator(mode="after")
    def ordered_non_overlapping(self) -> Self:
        if self.development.end >= self.final_test.start:
            raise ValueError("development must end strictly before final_test starts")
        return self

    def check_development_range(self, start: date | str, end: date | str) -> None:
        """Validate a proposed development query; this is not runtime access control."""
        checked_split = type(self).model_validate(self.model_dump(mode="python"))
        requested = TimeRange(start=start, end=end)
        if (
            requested.start < checked_split.development.start
            or requested.end > checked_split.development.end
        ):
            raise ValueError("requested range falls outside the development interval")


class DataSpec(StrictContract):
    data_id: Text
    source: Literal["synthetic_fixture"]
    market: Literal["USA"]
    frequency: Literal["daily"]
    timezone: Text
    fields: list[Text] = Field(min_length=1)
    normalization: Literal["raw_with_corporate_actions"]
    universe_rule: Text

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must identify an available IANA time zone") from exc
        return value

    @field_validator("fields")
    @classmethod
    def unique_fields(cls, value: list[str]) -> list[str]:
        _unique(value, "fields")
        return value


class FactorSpec(StrictContract):
    factor_id: Text
    hypothesis: Text
    formula: Text
    required_fields: list[Text] = Field(min_length=1)
    lookback_sessions: PositiveInt
    expected_direction: Literal["positive", "negative", "undetermined"]

    @field_validator("required_fields")
    @classmethod
    def unique_required_fields(cls, value: list[str]) -> list[str]:
        _unique(value, "required_fields")
        return value


class LabelSpec(StrictContract):
    entry_rule: Literal["next_session_open"]
    exit_rule: Literal["hold_h_sessions"]
    horizon_sessions: PositiveInt
    return_type: Literal["excess_total_return"]
    benchmark: Text


class ExecutionSpec(StrictContract):
    signal_rule: Literal["after_data_available"]
    entry_rule: Literal["next_session_open"]
    exit_rule: Literal["hold_h_sessions"]
    rebalance_sessions: PositiveInt
    max_position_weight: Annotated[float, Field(strict=True, gt=0, le=1, allow_inf_nan=False)]
    gross_exposure_limit: PositiveFloat
    commission_bps: NonNegativeFloat
    slippage_bps: NonNegativeFloat
    allow_short: bool


class EvaluationSpec(StrictContract):
    metrics: list[MetricName] = Field(min_length=1)
    thresholds: dict[MetricName, FiniteFloat] = Field(min_length=1)
    validation_method: Literal["chronological_walk_forward"]
    train_sessions: PositiveInt
    validation_sessions: PositiveInt
    final_results_policy: Literal["isolated_with_derivatives"]

    @model_validator(mode="after")
    def consistent_thresholds(self) -> Self:
        _unique(self.metrics, "metrics")
        if not set(self.thresholds).issubset(self.metrics):
            raise ValueError("threshold keys must be included in metrics")
        for metric, value in self.thresholds.items():
            if metric == "coverage" and not 0 <= value <= 1:
                raise ValueError("coverage threshold must be between 0 and 1")
            if metric in {"ic", "rank_ic"} and not -1 <= value <= 1:
                raise ValueError("IC thresholds must be between -1 and 1")
            if metric == "turnover" and value < 0:
                raise ValueError("turnover threshold must be non-negative")
        return self


class BudgetSpec(StrictContract):
    max_candidates: PositiveInt
    max_model_calls: ZeroBudget
    max_platform_jobs: ZeroBudget
    max_optimization_jobs: ZeroBudget
    max_runtime_seconds: PositiveInt
    max_spend_usd: ZeroBudget


class ResearchProtocol(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    protocol_id: Text
    title: Text
    stage: Literal["S0"] = "S0"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    data: DataSpec
    factor: FactorSpec
    label: LabelSpec
    execution: ExecutionSpec
    evaluation: EvaluationSpec
    budget: BudgetSpec
    data_split: DataSplitSpec
    pending_decisions: list[Text]

    @model_validator(mode="after")
    def cross_contract_consistency(self) -> Self:
        absent = set(self.factor.required_fields) - set(self.data.fields)
        if absent:
            raise ValueError(f"factor requires unavailable fields: {sorted(absent)}")
        if self.label.entry_rule != self.execution.entry_rule:
            raise ValueError("label and execution entry rules must match")
        if self.label.exit_rule != self.execution.exit_rule:
            raise ValueError("label and execution exit rules must match")
        _unique(self.pending_decisions, "pending_decisions")
        return self


class ExperimentRecord(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: Text
    stage: Literal["S0"] = "S0"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    protocol_sha256: Sha256
    code_commit: GitCommit | None
    working_tree_dirty: bool
    created_at: AwareDatetime
    validation_status: Literal["VALIDATED"] = "VALIDATED"
    platform_status: Literal["NOT_RUN"] = "NOT_RUN"
    economic_status: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    review_status: Literal["PENDING"] = "PENDING"

    @model_validator(mode="after")
    def unknown_source_is_not_clean(self) -> Self:
        if self.code_commit is None and not self.working_tree_dirty:
            raise ValueError("unknown source commit must not be represented as clean")
        return self


class ArtifactRef(StrictContract):
    path: Annotated[str, StringConstraints(strict=True, min_length=1)]
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        windows = PureWindowsPath(value)
        if windows.drive or windows.root or value != value.strip():
            raise ValueError("artifact path must be an unambiguous relative file path")
        normalized = value.replace("\\", "/")
        parts = normalized.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("artifact path must not contain empty, dot or parent components")
        reserved = {"CON", "PRN", "AUX", "NUL"} | {
            f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
        }
        for part in parts:
            if (
                any(ord(char) < 32 or char in '<>:"|?*' for char in part)
                or part.endswith((".", " "))
                or part.split(".")[0].upper() in reserved
            ):
                raise ValueError("artifact path contains an unsafe file-name component")
        return normalized


class ReleaseManifest(StrictContract):
    release_id: Text
    code_commit: GitCommit
    config_sha256: Sha256
    artifacts: list[ArtifactRef] = Field(min_length=1)
    review_status: Literal["PENDING"] = "PENDING"

    @field_validator("artifacts")
    @classmethod
    def unique_artifact_paths(cls, value: list[ArtifactRef]) -> list[ArtifactRef]:
        _unique([item.path.casefold() for item in value], "artifact paths")
        return value


class ReviewReceipt(StrictContract):
    """Receipt consistency only: this does not authenticate a reviewer or authorize Git."""

    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S0"] = "S0"
    audited_commit: GitCommit
    reviewer: Text
    decision: Literal["PENDING", "APPROVED", "CHANGES_REQUIRED"] = "PENDING"
    engineering_status: Literal["PASS", "FAIL", "INCOMPLETE"] = "INCOMPLETE"
    platform_status: Literal["PASS", "FAIL", "NOT_RUN", "NOT_APPLICABLE"] = "NOT_RUN"
    economic_status: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    blocking_findings: NonNegativeInt = 0
    evidence: list[Text] = Field(default_factory=list)
    reviewed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def consistent_approval(self) -> Self:
        _unique(self.evidence, "evidence")
        if self.decision == "APPROVED" and (
            self.engineering_status != "PASS"
            or self.platform_status != "NOT_APPLICABLE"
            or self.blocking_findings != 0
            or not self.evidence
            or self.reviewed_at is None
        ):
            raise ValueError(
                "S0 APPROVED requires engineering PASS, platform NOT_APPLICABLE, zero blocking findings, "
                "non-empty evidence and an aware reviewed_at"
            )
        return self


CONTRACT_MODELS: dict[str, type[StrictContract]] = {
    model.__name__: model
    for model in (
        DataSpec,
        FactorSpec,
        LabelSpec,
        ExecutionSpec,
        EvaluationSpec,
        ExperimentRecord,
        ReleaseManifest,
        ResearchProtocol,
        ReviewReceipt,
        TimeRange,
        DataSplitSpec,
        BudgetSpec,
        ArtifactRef,
    )
}


def load_protocol(path: str | Path) -> ResearchProtocol:
    """Read a UTF-8 JSON draft protocol, rejecting duplicate keys and NaN/Infinity."""
    return ResearchProtocol.model_validate(read_json_object(path))


def canonical_protocol_bytes(protocol: ResearchProtocol) -> bytes:
    """Serialize a revalidated protocol deterministically for provenance hashes."""
    checked = ResearchProtocol.model_validate(protocol.model_dump(mode="python"))
    return json.dumps(
        checked.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
