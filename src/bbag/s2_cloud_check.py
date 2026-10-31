"""Fixed control receipts for the Cloud-local standardization bridge.

Provider callbacks run only through the bridge's original-alias preflight.
This module neither imports an SDK nor reserves or submits a platform task.
Its local checks are not independent platform acceptance or provider truth.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from typing import Callable, Literal, Self

from pydantic import model_validator

from .contracts import StrictContract
from .s2_access import AccessGuard
from .s2_cloud_data import CloudDataPlan, CloudSourceBinding, build_cloud_bundle, cloud_plan_binding, revalidate_cloud_result
from .s2_data import data_quality

class CloudStandardizationChecks(StrictContract):
    alias_sid_binding: Literal["PASS"] = "PASS"
    raw_standardization: Literal["PASS"] = "PASS"
    declared_calendar_consistency: Literal["PASS"] = "PASS"
    declared_snapshot_clock: Literal["PASS"] = "PASS"
    requested_field_coverage: Literal["PASS", "FAIL"]
    membership_quality_coverage: Literal["PASS", "FAIL", "UNVERIFIED"]
    membership_records: Literal["PASS", "UNVERIFIED"]
    corporate_action_records: Literal["PASS", "UNVERIFIED"]
    provider_disclosure_revision_truth: Literal["UNVERIFIED"] = "UNVERIFIED"
    provider_identity_history_truth: Literal["UNVERIFIED"] = "UNVERIFIED"
    cloud_cache_persistence: Literal["NOT_RUN"] = "NOT_RUN"
    platform_recovery: Literal["NOT_RUN"] = "NOT_RUN"


class CloudStandardizationReceipt(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    binding: CloudSourceBinding
    outcome: Literal["CHECKS_COMPLETED", "ERROR"]
    checks: CloudStandardizationChecks | None = None
    error_category: Literal["STANDARDIZATION_REJECTED"] | None = None
    evidence_class: Literal["DECLARED_INPUT_CHECKS"] = "DECLARED_INPUT_CHECKS"
    provider_authentication: Literal["NOT_AUTHENTICATED"] = "NOT_AUTHENTICATED"
    platform_integration: Literal["NOT_RUN"] = "NOT_RUN"
    independent_review: Literal["PENDING"] = "PENDING"
    economic_evidence: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.outcome == "ERROR":
            if self.checks is not None or self.error_category is None:
                raise ValueError("an error receipt carries only its closed category")
        elif self.checks is None or self.error_category is not None:
            raise ValueError("completed checks require their fixed check set")
        return self


class CloudCheckRejected(ValueError):
    """Unbound input has no publishable receipt; expose only INVALID_REQUEST."""


class _DiscardOutput(io.TextIOBase):
    def write(self, value):
        return len(value)

    def flush(self):
        return None


def run_cloud_data_check(plan: CloudDataPlan, *, guard: AccessGuard,
                         resolver: Callable, fetcher: Callable) -> CloudStandardizationReceipt:
    """Return fixed checks only; never return SIDs, market values or data hashes.

    Separate runtime task reservation, current source verification and owner
    execution authorization are required before a real Cloud application calls
    this function. Callback assertions are not authenticated provider evidence.
    Offline tests supply synthetic mocks; execution_location is a declaration,
    not proof that this Python process is hosted by QuantConnect.
    """
    try:
        if not isinstance(plan, CloudDataPlan):
            raise ValueError("typed plan required")
        plan = CloudDataPlan.model_validate(plan)
        plan = CloudDataPlan.model_validate(plan.model_dump(mode="python", warnings=False))
        binding = cloud_plan_binding(plan)
    except Exception:
        raise CloudCheckRejected("INVALID_REQUEST") from None
    try:
        with redirect_stdout(_DiscardOutput()), redirect_stderr(_DiscardOutput()):
            result = build_cloud_bundle(plan, guard=guard, resolver=resolver, fetcher=fetcher)
            result = revalidate_cloud_result(plan, result, guard=guard)
            bundle = result.bundle
            quality = data_quality(bundle, bundle.request, result.mapping_guard)
            bars = {(bar.session, bar.sid): bar for bar in bundle.bars}
            # This is the declared calendar's Cartesian coverage, not proof that
            # the provider supplied every actual exchange session.
            expected = [(day.session, sid) for day in bundle.sessions for sid in bundle.request.assets]
            complete = bool(expected) and all(key in bars and all(
                getattr(bars[key], field) is not None for field in bundle.request.fields) for key in expected)
            coverage = result.resolution.coverage
            membership_coverage = "UNVERIFIED"
            if coverage.membership == "PROVIDED_NONEMPTY":
                membership_coverage = "PASS" if quality.expected_bars > 0 and quality.complete_bars == quality.expected_bars else "FAIL"
            checks = CloudStandardizationChecks(
                requested_field_coverage="PASS" if complete else "FAIL",
                membership_quality_coverage=membership_coverage,
                membership_records="PASS" if coverage.membership == "PROVIDED_NONEMPTY" else "UNVERIFIED",
                corporate_action_records="PASS" if coverage.corporate_actions == "PROVIDED_NONEMPTY" else "UNVERIFIED")
        return CloudStandardizationReceipt(binding=binding, outcome="CHECKS_COMPLETED", checks=checks)
    except Exception:
        return CloudStandardizationReceipt(binding=binding, outcome="ERROR", error_category="STANDARDIZATION_REJECTED")


S2_CLOUD_CHECK_CONTRACTS = {model.__name__: model for model in (
    CloudStandardizationChecks, CloudStandardizationReceipt,
)}
