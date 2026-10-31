"""Offline, externally anchored campaign continuation arithmetic; never dispatch.

The caller must retain the baseline digest and the complete list of later
synthetic ledgers outside the plan. These context anchors and an allowlisted
owner-document digest are application trust inputs, not identity authentication
or an OS sandbox. No constructor here creates a ledger, grant or reservation.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Annotated, Literal, Self, Sequence

from pydantic import Field, field_validator, model_validator

from .contracts import GitCommit, Sha256, StrictContract, Text
from .s2_readiness import CampaignCaps, CampaignReadiness, ExpectedLedger, ReadinessError, inspect_campaign
from .s2_tasks import canonical_task_bytes

Increment = Annotated[int, Field(strict=True, ge=0, le=32)]
Count = Annotated[int, Field(strict=True, ge=0, le=10000)]
Operation = Literal["compile", "research_check"]


class ContinuationAttemptCounts(StrictContract):
    compile: Count
    research_check: Count


class OwnerBudgetIncrement(StrictContract):
    """Total additions above the original 2/3 caps, never per-call top-ups.

    OWNER_APPROVED text alone confers no authority.
    """

    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT", "OWNER_APPROVED"] = "DRAFT"
    baseline_sha256: Sha256
    source_commit: GitCommit
    config_sha256: Sha256
    approval_reference: Text
    compile_increment: Increment = 0
    research_increment: Increment = 0
    max_new_cost_microusd: Literal[0] = 0
    scope: Literal["EXISTING_WHITELIST_ONLY"] = "EXISTING_WHITELIST_ONLY"

    @field_validator("max_new_cost_microusd", mode="before")
    @classmethod
    def integer_zero(cls, value: object) -> object:
        if type(value) is not int or value != 0:
            raise ValueError("new-cost allowance must remain an explicit integer zero")
        return value


class ContinuationPlan(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    source_commit: GitCommit
    config_sha256: Sha256
    historical_campaign: CampaignReadiness
    additional_synthetic_ledgers: list[ExpectedLedger] = Field(default_factory=list, max_length=32)
    increment: OwnerBudgetIncrement | None = None
    preserve_research_attempts: Literal[1] = 1

    @field_validator("preserve_research_attempts", mode="before")
    @classmethod
    def fixed_reserve(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("the existing last Research attempt must remain reserved")
        return value

    @model_validator(mode="after")
    def original_history(self) -> Self:
        history = self.historical_campaign
        if (len(history.ledgers) != 2 or len(history.attempts) != 4
                or history.caps.compile_attempt_limit != 2 or history.caps.research_attempt_limit != 3
                or history.used_attempts != {"compile": 2, "research_check": 2}
                or history.billing_unknown_attempt_count != 4
                or any(attempt.actual_cost_microusd is not None for attempt in history.attempts)):
            raise ValueError("retain the complete two-ledger, four-attempt original campaign and its unknown bills")
        return self


class ContinuationPreview(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["OFFLINE_PREVIEW_ONLY"] = "OFFLINE_PREVIEW_ONLY"
    source_commit: GitCommit
    config_sha256: Sha256
    baseline_sha256: Sha256
    historical_campaign: CampaignReadiness
    additional_synthetic_campaigns: list[CampaignReadiness]
    authorization_sha256: Sha256 | None
    authorization_recognized: bool
    effective_increment: ContinuationAttemptCounts
    cumulative_caps: ContinuationAttemptCounts
    used_attempts: ContinuationAttemptCounts
    remaining_attempts: ContinuationAttemptCounts
    available_attempts: ContinuationAttemptCounts
    preserved_research_attempts: Literal[1] = 1
    research_reserve_intact: bool
    within_cumulative_caps: bool
    billing_unknown_attempt_count: Count
    known_cost_microusd: Count
    reserved_unknown_cost_microusd: Count
    actual_billing_total_microusd: Count | None
    max_new_cost_microusd: Literal[0] = 0
    plan_not_runtime_grant: Literal[True] = True
    fresh_history_recheck_required: Literal[True] = True
    external_context_not_identity_authentication: Literal[True] = True
    dispatch_allowed: Literal[False] = False
    runtime_grant_enrolled: Literal[False] = False
    new_runtime_ledger_created: Literal[False] = False
    new_task_reserved: Literal[False] = False
    real_additional_ledger_support: Literal[False] = False
    independent_review: Literal["PENDING"] = "PENDING"
    platform_integration: Literal["NOT_RUN"] = "NOT_RUN"
    economic_evidence: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"

    @field_validator("max_new_cost_microusd", "preserved_research_attempts", mode="before")
    @classmethod
    def fixed_integer_controls(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("budget and reserve controls require explicit integers")
        return value

    @field_validator("plan_not_runtime_grant", "fresh_history_recheck_required",
                     "external_context_not_identity_authentication", "dispatch_allowed",
                     "runtime_grant_enrolled", "new_runtime_ledger_created", "new_task_reserved",
                     "real_additional_ledger_support", mode="before")
    @classmethod
    def boolean_flags(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("control flags require explicit booleans")
        return value

    @model_validator(mode="after")
    def accounting(self) -> Self:
        ContinuationPlan(source_commit=self.source_commit, config_sha256=self.config_sha256,
                         historical_campaign=self.historical_campaign)
        histories = [self.historical_campaign, *self.additional_synthetic_campaigns]
        entries = [attempt for history in histories for attempt in history.attempts]
        if (self.baseline_sha256 != campaign_baseline_sha256(self.historical_campaign)
                or self.effective_increment.compile > 32 or self.effective_increment.research_check > 32
                or self.authorization_recognized and self.authorization_sha256 is None
                or len({a.local_attempt_id for a in entries}) != len(entries)
                or any(history.caps.response_class != "SYNTHETIC_TEST"
                       or any(a.source_commit != self.source_commit or a.config_sha256 != self.config_sha256
                              for a in history.attempts) for history in self.additional_synthetic_campaigns)):
            raise ValueError("continuation provenance and bounded increments must remain intact")
        used = {op: sum(attempt.operation == op for attempt in entries) for op in ("compile", "research_check")}
        caps = {"compile": 2 + self.effective_increment.compile,
                "research_check": 3 + self.effective_increment.research_check}
        remaining = {op: max(0, caps[op] - used[op]) for op in caps}
        within = all(used[op] <= caps[op] for op in caps)
        intact = remaining["research_check"] >= 1
        available = {"compile": 0, "research_check": 0}
        if self.authorization_recognized and within and intact:
            available = {"compile": remaining["compile"], "research_check": remaining["research_check"] - 1}
        unknown = [attempt for attempt in entries if attempt.actual_cost_microusd is None]
        known = sum(attempt.actual_cost_microusd or 0 for attempt in entries)
        if (self.used_attempts.model_dump() != used or self.cumulative_caps.model_dump() != caps
                or self.remaining_attempts.model_dump() != remaining or self.available_attempts.model_dump() != available
                or self.within_cumulative_caps != within or self.research_reserve_intact != intact
                or self.billing_unknown_attempt_count != len(unknown) or self.known_cost_microusd != known
                or self.reserved_unknown_cost_microusd != sum(a.reserved_cost_microusd for a in unknown)
                or self.actual_billing_total_microusd != (None if unknown else known)
                or not self.authorization_recognized and self.effective_increment.model_dump() != {"compile": 0, "research_check": 0}):
            raise ValueError("continuation arithmetic must retain every attempt, reserve and unknown bill")
        return self


S2_CONTINUATION_CONTRACTS = {model.__name__: model for model in (
    ContinuationAttemptCounts, OwnerBudgetIncrement, ContinuationPlan, ContinuationPreview,
)}


def _checked(model, value):
    if not isinstance(value, model):
        raise ValueError("continuation inputs require their explicit typed contracts")
    # Instance revalidation also sees forbidden attributes injected by copy;
    # dumping first would silently discard those unknown model attributes.
    checked = model.model_validate(value)
    return model.model_validate(checked.model_dump(mode="python"))


def _digest(value: StrictContract) -> str:
    return hashlib.sha256(canonical_task_bytes(value)).hexdigest()


def campaign_baseline_sha256(history: CampaignReadiness) -> str:
    """Digest retained context, never a substitute for a fresh inspection."""
    return _digest(_checked(CampaignReadiness, history))


def owner_increment_sha256(increment: OwnerBudgetIncrement) -> str:
    return _digest(_checked(OwnerBudgetIncrement, increment))


def _anchor_key(anchor: ExpectedLedger, root: Path):
    def path(value):
        item = Path(value)
        return (root/item).resolve() if not item.is_absolute() else item.resolve()
    return path(anchor.database), path(anchor.metadata_directory), anchor.expected_sha256


def inspect_continuation(
    plan: ContinuationPlan, *, expected_baseline_sha256: str,
    required_synthetic_ledgers: Sequence[ExpectedLedger], root: Path | None = None,
    trusted_owner_authorization_sha256s: frozenset[str] = frozenset(),
) -> ContinuationPreview:
    """Reinspect the full retained chain, then calculate a non-runtime preview.

    ``expected_baseline_sha256`` and ``required_synthetic_ledgers`` must come
    from retained caller context. Deleting later history from both caller
    context and JSON cannot be detected by an unprivileged local library.
    Additional real ledgers and dispatch require a separately reviewed runtime
    boundary; this function only accepts later SYNTHETIC_TEST histories.
    """
    plan = _checked(ContinuationPlan, plan)
    sha_pattern = r"[0-9a-f]{64}"
    if (type(expected_baseline_sha256) is not str or re.fullmatch(sha_pattern, expected_baseline_sha256) is None
            or type(trusted_owner_authorization_sha256s) is not frozenset
            or any(type(item) is not str or re.fullmatch(sha_pattern, item) is None
                   for item in trusted_owner_authorization_sha256s)):
        raise ValueError("external trust inputs require exact lowercase digest anchors")
    if not isinstance(required_synthetic_ledgers, (list, tuple)) or len(required_synthetic_ledgers) > 32:
        raise ValueError("supply the complete retained additional synthetic ledger list")
    required = [_checked(ExpectedLedger, item) for item in required_synthetic_ledgers]
    if root is not None and not isinstance(root, Path):
        raise ValueError("continuation root must be an explicit filesystem Path")
    base = (root or Path.cwd()).resolve()
    original = plan.historical_campaign
    if campaign_baseline_sha256(original) != expected_baseline_sha256:
        raise ValueError("original campaign does not match its externally retained baseline digest")
    declared_keys = [_anchor_key(item, base) for item in plan.additional_synthetic_ledgers]
    required_keys = [_anchor_key(item, base) for item in required]
    if len(required_keys) != len(set(required_keys)) or set(declared_keys) != set(required_keys) or len(declared_keys) != len(required_keys):
        raise ValueError("supply every retained additional ledger exactly once")
    all_anchors = [*original.caps.expected_ledgers, *required]
    keys = [_anchor_key(item, base) for item in all_anchors]
    if len({key[0] for key in keys}) != len(keys) or len({key[1] for key in keys}) != len(keys) or len({key[2] for key in keys}) != len(keys):
        raise ValueError("copied or duplicated ledger/metadata anchors cannot create fresh allowance")
    increment = plan.increment
    authorization_sha = owner_increment_sha256(increment) if increment is not None else None
    if increment is not None and (increment.baseline_sha256 != expected_baseline_sha256
            or increment.source_commit != plan.source_commit or increment.config_sha256 != plan.config_sha256):
        raise ValueError("budget proposal must bind the retained baseline and exact target source/config")
    recognized = (increment is not None and increment.status == "OWNER_APPROVED"
                  and authorization_sha in trusted_owner_authorization_sha256s)
    effective = {"compile": increment.compile_increment if recognized else 0,
                 "research_check": increment.research_increment if recognized else 0}

    def fresh():
        baseline = inspect_campaign(original.caps, root=base)
        if campaign_baseline_sha256(baseline) != expected_baseline_sha256:
            raise ValueError("fresh original history differs from retained full source/config/metadata provenance")
        later, pending = [], []
        for anchor in required:
            data = original.caps.model_dump(mode="python")
            data.update(expected_ledgers=[anchor.model_dump(mode="python")], response_class="SYNTHETIC_TEST")
            try:
                item = inspect_campaign(CampaignCaps.model_validate(data), root=base)
            except ReadinessError as error:
                if (error.unknown_attempts is None or error.active_attempts is None
                        or error.unknown_billing_attempts is None):
                    raise
                pending.append(error)
                continue
            if any(a.source_commit != plan.source_commit or a.config_sha256 != plan.config_sha256 for a in item.attempts):
                raise ValueError("additional synthetic attempts must bind the exact continuation source/config")
            later.append(item)
        if pending:
            raise ReadinessError("unresolved/active continuation attempts block planning; original unknown bills remain retained",
                unknown_attempts=sum(error.unknown_attempts for error in pending),
                active_attempts=sum(error.active_attempts for error in pending),
                unknown_billing_attempts=baseline.billing_unknown_attempt_count
                    + sum(item.billing_unknown_attempt_count for item in later)
                    + sum(error.unknown_billing_attempts for error in pending))
        identities = [a.local_attempt_id for history in [baseline, *later] for a in history.attempts]
        if len(identities) != len(set(identities)):
            raise ValueError("copied attempt identity cannot supply additional history")
        return baseline, later

    baseline, later = fresh()
    again, later_again = fresh()
    if canonical_task_bytes(baseline) != canonical_task_bytes(again) or [canonical_task_bytes(x) for x in later] != [canonical_task_bytes(x) for x in later_again]:
        raise ValueError("campaign changed across continuation checks; no stale preview may be reused")
    entries = [a for history in [baseline, *later] for a in history.attempts]
    used = {op: sum(a.operation == op for a in entries) for op in ("compile", "research_check")}
    caps = {"compile": 2 + effective["compile"], "research_check": 3 + effective["research_check"]}
    remaining = {op: max(0, caps[op] - used[op]) for op in caps}
    within = all(used[op] <= caps[op] for op in caps)
    intact = remaining["research_check"] >= 1
    available = {"compile": 0, "research_check": 0}
    if recognized and within and intact:
        available = {"compile": remaining["compile"], "research_check": remaining["research_check"] - 1}
    unknown = [a for a in entries if a.actual_cost_microusd is None]
    known = sum(a.actual_cost_microusd or 0 for a in entries)
    return ContinuationPreview(source_commit=plan.source_commit, config_sha256=plan.config_sha256,
        baseline_sha256=expected_baseline_sha256, historical_campaign=baseline, additional_synthetic_campaigns=later,
        authorization_sha256=authorization_sha, authorization_recognized=recognized, effective_increment=effective,
        cumulative_caps=caps, used_attempts=used, remaining_attempts=remaining, available_attempts=available,
        research_reserve_intact=intact, within_cumulative_caps=within, billing_unknown_attempt_count=len(unknown),
        known_cost_microusd=known, reserved_unknown_cost_microusd=sum(a.reserved_cost_microusd for a in unknown),
        actual_billing_total_microusd=None if unknown else known)


def check_continuation_budget(plan: ContinuationPlan, operation: Operation, **context) -> ContinuationPreview:
    """Check a planning precondition after fresh inspection, never grant dispatch."""
    if type(operation) is not str or operation not in {"compile", "research_check"}:
        raise ValueError("unsupported continuation operation")
    result = inspect_continuation(plan, **context)
    if getattr(result.available_attempts, operation) < 1:
        raise ValueError("no continuation budget is available; the last Research attempt remains reserved")
    return result
