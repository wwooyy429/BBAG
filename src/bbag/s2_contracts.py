"""S2 stage configuration and handoff contracts; configuration is not authority."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .contracts import GitCommit, Sha256, StrictContract, Text
from .jsonio import read_json_object
from .s2_access import AccessPolicy, DataRequest, S2_ACCESS_CONTRACTS
from .s2_data import (LiquidityRule, QualityReport, QualityRule, StandardDataBundle,
                      UniverseResult, S2_DATA_CONTRACTS, canonical_request_sha256)
from .s2_quantconnect import S2_QC_CONTRACTS
from .s2_tasks import S2_TASK_CONTRACTS, TaskBudget, TaskRecord, TaskUsage


class S2Config(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    config_id: Text
    policy: AccessPolicy
    request: DataRequest
    task_budget: TaskBudget
    bundle: StandardDataBundle | None = None
    liquidity: LiquidityRule = Field(default_factory=LiquidityRule)
    quality: QualityRule = Field(default_factory=QualityRule)
    pending_decisions: list[Text] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        policy, request = self.policy, self.request
        if request.provider != policy.provider or request.purpose not in policy.allowed_purposes:
            raise ValueError("request provider/purpose must match policy")
        if request.start < policy.development.start or request.end > policy.development.end:
            raise ValueError("configured request is outside development dates")
        if not set(request.assets).issubset(policy.allowed_assets) or not set(request.fields).issubset(policy.allowed_fields):
            raise ValueError("configured request assets/fields exceed policy")
        if request.provider == "SYNTHETIC":
            if self.bundle is None or self.task_budget.response_class != "SYNTHETIC_TEST":
                raise ValueError("synthetic run needs its synthetic bundle and task class")
            if self.bundle.request != request:
                raise ValueError("synthetic bundle must bind the exact configured request")
        elif (self.bundle is not None or request.output_mode != "CONTROL_ONLY"
              or self.task_budget.response_class != "CONTROL_ONLY"):
            raise ValueError("real cloud config carries control plans only, never market observations")
        if len(self.pending_decisions) != len(set(self.pending_decisions)):
            raise ValueError("pending decisions must be unique")
        return self


class S2SyntheticResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    config_sha256: Sha256
    request_sha256: Sha256
    data_provider: Literal["SYNTHETIC"] = "SYNTHETIC"
    universe: UniverseResult
    quality: QualityReport
    projected_s1_panel_sha256: Sha256
    task: TaskRecord
    usage: TaskUsage
    platform_execution: Literal["NOT_RUN"] = "NOT_RUN"
    economic_evidence: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"


class S2RunManifest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    run_id: Text
    kind: Literal["synthetic_data_and_task_control"] = "synthetic_data_and_task_control"
    config_sha256: Sha256
    result_sha256: Sha256
    code_commit: GitCommit
    working_tree_dirty: bool
    created_at: AwareDatetime
    platform_execution: Literal["NOT_RUN"] = "NOT_RUN"
    independent_review: Literal["PENDING"] = "PENDING"


class S2ReviewReceipt(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    audited_commit: GitCommit
    reviewer: Text
    decision: Literal["PENDING", "CHANGES_REQUIRED", "APPROVED"] = "PENDING"
    engineering_status: Literal["PASS", "FAIL", "INCOMPLETE"] = "INCOMPLETE"
    platform_status: Literal["PASS", "FAIL", "NOT_RUN"] = "NOT_RUN"
    economic_status: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    blocking_findings: Annotated[int, Field(strict=True, ge=0)] = 0
    evidence: list[Text] = Field(default_factory=list)
    reviewed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def complete_approval(self) -> Self:
        if self.decision == "APPROVED" and (self.engineering_status != "PASS" or self.platform_status != "PASS"
                or self.blocking_findings != 0 or not self.evidence or self.reviewed_at is None):
            raise ValueError("S2 approval requires engineering/platform PASS, zero blockers and actual review evidence/time")
        if len(self.evidence) != len(set(self.evidence)):
            raise ValueError("review evidence must not be duplicated")
        return self


S2_REQUIRED_CHECKS = ("pip_check", "pytest", "schemas_s0", "schemas_s1", "schemas_s2",
                      "config_synthetic", "config_cloud", "synthetic_run", "reference_check", "pending_review_gate")


class S2CheckRecord(StrictContract):
    name: Literal["pip_check", "pytest", "schemas_s0", "schemas_s1", "schemas_s2", "config_synthetic",
                  "config_cloud", "synthetic_run", "reference_check", "pending_review_gate"]
    command: list[Text] = Field(min_length=1)
    started_at_utc: AwareDatetime
    duration_seconds: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
    exit_code: int
    expected_exit_code: int
    passed: bool

    @model_validator(mode="after")
    def truthful_exit(self) -> Self:
        expected = 3 if self.name == "pending_review_gate" else 0
        if self.expected_exit_code != expected or self.passed != (self.exit_code == expected):
            raise ValueError("check verdict must reflect the fixed protocol and actual exit")
        return self


class S2Verification(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    implementation_commit: GitCommit
    code_commit_end: GitCommit
    working_tree_dirty_start: bool
    working_tree_dirty_end: bool
    generated_at_utc: AwareDatetime
    python: Text
    platform: Text
    dependency_lock_sha256: Sha256
    checks: list[S2CheckRecord] = Field(min_length=10, max_length=10)
    developer_checks_passed: bool
    independent_review: Literal["PENDING"] = "PENDING"
    platform_integration: Literal["NOT_RUN"] = "NOT_RUN"
    economic_evidence: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    next_stage_authorized: Literal[False] = False

    @field_validator("next_stage_authorized", mode="before")
    @classmethod
    def no_numeric_authorization(cls, value: object) -> object:
        if type(value) is not bool or value is not False:
            raise ValueError("verification requires boolean false, never stage authorization")
        return value

    @model_validator(mode="after")
    def truthful_protocol(self) -> Self:
        if sorted(item.name for item in self.checks) != sorted(S2_REQUIRED_CHECKS):
            raise ValueError("the ten S2 checks must be complete and unique")
        actual = (all(item.passed for item in self.checks) and not self.working_tree_dirty_start
                  and not self.working_tree_dirty_end and self.implementation_commit == self.code_commit_end)
        if self.developer_checks_passed != actual:
            raise ValueError("verification must reflect checks and unchanged clean source")
        if type(self.next_stage_authorized) is not bool or self.next_stage_authorized is not False:
            raise ValueError("verification never authorizes a next stage")
        return self


S2_CONTRACT_MODELS = {**S2_ACCESS_CONTRACTS, **S2_DATA_CONTRACTS, **S2_QC_CONTRACTS, **S2_TASK_CONTRACTS,
    **{model.__name__: model for model in (S2Config, S2SyntheticResult, S2RunManifest, S2ReviewReceipt,
                                         S2CheckRecord, S2Verification)}}


def load_s2_config(path: str | Path) -> S2Config:
    return S2Config.model_validate_json(json.dumps(read_json_object(path), allow_nan=False))


def canonical_s2_config(config: S2Config) -> bytes:
    config = S2Config.model_validate(config)
    return json.dumps(config.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
