"""Control-only QuantConnect callback bridge; market data stays in Cloud.

This module has no credentials or QuantConnect client. A caller supplies the
tool callback after separately checking the available connector and project.
Imported metadata and callback claims are never authenticated by this bridge.
No prices, histories, data hashes, plots, or arbitrary provider logs are accepted.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Annotated, Callable, Literal, Self

from pydantic import Field, ValidationError, model_validator

from .contracts import GitCommit, Sha256, StrictContract
from .s2_access import AccessGuard, DataRequest

if TYPE_CHECKING:
    from .s2_tasks import TaskResponse


CheckName = Literal[
    "data_presence", "requested_bounds", "field_availability", "normalization_raw",
    "point_in_time", "identity_consistency", "no_data_export",
]
CheckStatus = Literal["ACCEPTED", "RUNNING", "COMPLETED", "ERROR", "UNKNOWN"]
ErrorCategory = Literal[
    "NONE", "INVALID_REQUEST", "ACCESS_DENIED", "CAPABILITY_UNAVAILABLE",
    "PROVIDER_ERROR", "TIMEOUT", "MALFORMED_RESPONSE",
]
PositiveProjectId = Annotated[int, Field(strict=True, ge=1)]
ALL_CHECKS: list[CheckName] = [
    "data_presence", "requested_bounds", "field_availability", "normalization_raw",
    "point_in_time", "identity_consistency", "no_data_export",
]


class QuantConnectCapabilities(StrictContract):
    """A declared connector inventory, not an authenticated platform result."""

    schema_version: Literal["1.0"] = "1.0"
    transport: Literal["assistant_tool_callback"] = "assistant_tool_callback"
    evidence_class: Literal["UNAUTHENTICATED_DECLARATION"] = "UNAUTHENTICATED_DECLARATION"
    operations: list[Literal["research_check"]] = Field(default_factory=lambda: ["research_check"], min_length=1)
    project_ids: list[PositiveProjectId] = Field(min_length=1, max_length=50)
    checks: list[CheckName] = Field(default_factory=lambda: list(ALL_CHECKS), min_length=1, max_length=7)

    @model_validator(mode="after")
    def no_ambiguous_capabilities(self) -> Self:
        for values in (self.operations, self.project_ids, self.checks):
            if len(values) != len(set(values)):
                raise ValueError("connector capabilities must not contain duplicates")
        return self


class QuantConnectCheckRequest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    operation: Literal["research_check"] = "research_check"
    project_id: PositiveProjectId
    data: DataRequest
    source_commit: GitCommit
    config_sha256: Sha256
    resolution: Literal["daily"] = "daily"
    normalization: Literal["raw"] = "raw"
    data_retention: Literal["cloud_only"] = "cloud_only"
    output_policy: Literal["verdicts_only"] = "verdicts_only"
    checks: list[CheckName] = Field(default_factory=lambda: ["no_data_export"], min_length=1, max_length=7)
    max_history_rows: Annotated[int, Field(strict=True, ge=1, le=128)] = 32

    @model_validator(mode="after")
    def bounded_control_only(self) -> Self:
        if self.data.output_mode != "CONTROL_ONLY" or self.data.purpose != self.purpose:
            raise ValueError("QuantConnect bridge requires ENGINEERING_TEST CONTROL_ONLY requests")
        if len(self.data.assets) > 4 or (self.data.end - self.data.start).days > 31:
            raise ValueError("cloud engineering checks are limited to four assets and 32 calendar dates")
        if len(self.checks) != len(set(self.checks)) or "no_data_export" not in self.checks:
            raise ValueError("checks must be unique and include no_data_export")
        return self


class QuantConnectCheckResponse(StrictContract):
    """The complete output allowlist: no free text or market observations."""

    schema_version: Literal["1.0"] = "1.0"
    operation: Literal["research_check"] = "research_check"
    project_id: PositiveProjectId
    request_sha256: Sha256
    source_commit: GitCommit
    config_sha256: Sha256
    status: CheckStatus
    error_category: ErrorCategory = "NONE"
    check_results: dict[CheckName, bool] = Field(default_factory=dict, max_length=7)

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        if self.status == "COMPLETED":
            if "no_data_export" not in self.check_results or self.error_category != "NONE":
                raise ValueError("completed checks require no_data_export verdict and no execution error")
        elif self.check_results:
            raise ValueError("unfinished checks must not contain completed verdicts")
        if self.status == "ERROR" and self.error_category == "NONE":
            raise ValueError("failed execution requires a bounded error category")
        if self.status in ("ACCEPTED", "RUNNING") and self.error_category != "NONE":
            raise ValueError("accepted/running checks must not claim an execution error")
        return self


class QuantConnectCheckResult(StrictContract):
    response: QuantConnectCheckResponse
    transport_evidence: Literal["CALLBACK_UNVERIFIED", "IMPORTED_UNVERIFIED", "CACHE_REUSE_UNVERIFIED"]
    platform_integration: Literal["NOT_RUN"] = "NOT_RUN"
    evidence_authentication: Literal["NOT_AUTHENTICATED"] = "NOT_AUTHENTICATED"
    market_data_export: Literal["NOT_SUPPORTED"] = "NOT_SUPPORTED"


def canonical_qc_request(request: QuantConnectCheckRequest) -> bytes:
    if not isinstance(request, QuantConnectCheckRequest):
        raise ValueError("a typed QuantConnect check request is required")
    request = QuantConnectCheckRequest.model_validate(request)
    return _canonical(request.model_dump(mode="json"))


def qc_request_sha256(request: QuantConnectCheckRequest) -> str:
    return hashlib.sha256(canonical_qc_request(request)).hexdigest()


def normalize_qc_check_response(
    response: QuantConnectCheckResponse, *, platform_task_id: str | None = None,
    invocation_id: str | None = None, cell_index: int | None = None,
    actual_cost_microusd: int | None = None,
) -> TaskResponse:
    """Produce LOCAL_NORMALIZED_METADATA for the durable task ledger.

    Optional identifiers and cost must come from separately observed connector
    metadata, never from invented stand-ins. No identifier is synthesized here.
    With none supplied, the ledger retains an unverified/unknown execution even
    when this imported response claims COMPLETED. This is not the raw response.
    """
    from .s2_tasks import TaskResponse

    response = QuantConnectCheckResponse.model_validate(response)
    statuses = {"ACCEPTED": "accepted", "RUNNING": "running", "COMPLETED": "completed",
                "ERROR": "error", "UNKNOWN": "UNKNOWN"}
    finished = response.status == "COMPLETED"
    return TaskResponse(
        operation="research_check", source_commit=response.source_commit,
        config_sha256=response.config_sha256, request_sha256=response.request_sha256,
        response_class="CONTROL_ONLY", project_id=response.project_id,
        status=statuses[response.status], platform_task_id=platform_task_id,
        invocation_id=invocation_id, cell_index=cell_index,
        success=True if finished else False if response.status == "ERROR" else None,
        check_passed=all(response.check_results.values()) if finished else None,
        error_category=response.error_category, actual_cost_microusd=actual_cost_microusd,
    )


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous metadata")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError("non-finite metadata")


def _read_verdicts(raw: bytes) -> QuantConnectCheckResponse:
    # Errors deliberately do not echo provider input or exception text. A
    # malformed callback could otherwise leak market data through diagnostics.
    try:
        if type(raw) is not bytes or len(raw) > 65_536:
            raise ValueError("invalid metadata bytes")
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if not isinstance(payload, dict):
            raise ValueError("invalid metadata object")
        # JSON mode permits ISO values in shared contracts while strict scalar
        # validation rejects string/number substitutions for bool verdicts.
        return QuantConnectCheckResponse.model_validate_json(_canonical(payload))
    except (ValueError, TypeError, UnicodeError, ValidationError, RecursionError):
        raise ValueError("provider response is not valid verdict-only metadata") from None


class QuantConnectDispatchError(RuntimeError):
    """A callback failure has unknown submission outcome; never blind-retry."""

    def __init__(self, category: Literal["TIMEOUT", "PROVIDER_ERROR", "MALFORMED_RESPONSE"]) -> None:
        self.error_category = category
        self.execution_status = "UNKNOWN"
        super().__init__("provider callback outcome unknown; reconcile the existing task before retrying")


class QuantConnectGateway:
    """Dispatch control plans through an injected, separately audited bridge.

    No method imports or exports market data. Metadata import validates only
    claims and never authenticates their source. The caller must use the durable
    task ledger to prevent retries after unknown outcomes; this class never
    retries a callback. Use dispatch inside a reserved ledger attempt; use
    cached_result before reserving an attempt to reuse evidence without counting
    another execution. Cache entries are exact-request verdicts, never prefixes.
    """

    def __init__(self, capabilities: QuantConnectCapabilities, guard: AccessGuard,
                 callback: Callable[[bytes], bytes] | None = None) -> None:
        self.capabilities = QuantConnectCapabilities.model_validate(capabilities)
        if not isinstance(guard, AccessGuard):
            raise ValueError("a concrete S2 access guard is required")
        if callback is not None and not callable(callback):
            raise ValueError("callback must be callable")
        self.guard = guard
        self.callback = callback
        self._cache: dict[str, bytes] = {}

    def _authorize(self, request: QuantConnectCheckRequest, operation: str) -> QuantConnectCheckRequest:
        request = QuantConnectCheckRequest.model_validate(request)
        capabilities = QuantConnectCapabilities.model_validate(self.capabilities)
        if request.project_id not in capabilities.project_ids or request.operation not in capabilities.operations:
            raise ValueError("request exceeds declared connector capabilities")
        if not set(request.checks).issubset(capabilities.checks):
            raise ValueError("requested checks exceed declared connector capabilities")
        self.guard.authorize(request.data, operation=operation)
        return request

    def _response(self, request: QuantConnectCheckRequest, raw: bytes) -> QuantConnectCheckResponse:
        response = _read_verdicts(raw)
        if (response.project_id != request.project_id or response.source_commit != request.source_commit
                or response.config_sha256 != request.config_sha256
                or response.request_sha256 != qc_request_sha256(request)):
            raise ValueError("provider metadata does not match the authorized request")
        if response.status == "COMPLETED" and set(response.check_results) != set(request.checks):
            raise ValueError("provider verdicts do not match the requested check set")
        return response

    def cached_result(self, request: QuantConnectCheckRequest) -> QuantConnectCheckResult | None:
        request = self._authorize(request, "cache_read")
        raw = self._cache.get(qc_request_sha256(request))
        if raw is None:
            return None
        return QuantConnectCheckResult(response=self._response(request, raw), transport_evidence="CACHE_REUSE_UNVERIFIED")

    def check(self, request: QuantConnectCheckRequest) -> QuantConnectCheckResult:
        """Reuse authorized cached verdicts or dispatch once on a cache miss."""
        request = self._authorize(request, "cloud_dispatch")
        cached = self.cached_result(request)
        if cached is not None:
            return cached
        return self.dispatch(request)

    def dispatch(self, request: QuantConnectCheckRequest) -> QuantConnectCheckResult:
        """Invoke once without cache reuse, inside a durable reserved attempt."""
        # A ledger reservation must correspond to this callback invocation, not
        # to a cache hit. Recheck immediately before provider I/O.
        request = self._authorize(request, "cloud_dispatch")
        if self.callback is None:
            raise ValueError("no QuantConnect callback is configured")
        try:
            raw = self.callback(canonical_qc_request(request))
        except TimeoutError:
            raise QuantConnectDispatchError("TIMEOUT") from None
        except Exception:
            raise QuantConnectDispatchError("PROVIDER_ERROR") from None
        try:
            return self._accept_metadata(request, raw, "CALLBACK_UNVERIFIED")
        except ValueError:
            raise QuantConnectDispatchError("MALFORMED_RESPONSE") from None

    def _accept_metadata(self, request: QuantConnectCheckRequest, raw: bytes,
                         evidence: Literal["CALLBACK_UNVERIFIED", "IMPORTED_UNVERIFIED"]) -> QuantConnectCheckResult:
        request = self._authorize(request, "cloud_dispatch")
        response = self._response(request, raw)
        # Only finished checks can be reused. Accepted, failed and unknown
        # submissions remain task-ledger concerns, never successful data cache.
        if response.status == "COMPLETED" and evidence == "CALLBACK_UNVERIFIED":
            self._authorize(request, "cache_write")
            self._cache[qc_request_sha256(request)] = _canonical(response.model_dump(mode="json"))
        return QuantConnectCheckResult(response=response, transport_evidence=evidence)

    def accept_metadata(self, request: QuantConnectCheckRequest, raw: bytes) -> QuantConnectCheckResult:
        """Validate imported claims without a live-execution claim or cache write."""
        return self._accept_metadata(request, raw, "IMPORTED_UNVERIFIED")

    def release_metadata(self, request: QuantConnectCheckRequest, result: QuantConnectCheckResult) -> bytes:
        # This releases the strict CONTROL_ONLY channel, never a data export.
        request = self._authorize(request, "cloud_dispatch")
        result = QuantConnectCheckResult.model_validate(result)
        raw = _canonical(result.response.model_dump(mode="json"))
        self._response(request, raw)
        return _canonical(result.model_dump(mode="json"))


S2_QC_CONTRACTS = {
    "QuantConnectCapabilities": QuantConnectCapabilities,
    "QuantConnectCheckRequest": QuantConnectCheckRequest,
    "QuantConnectCheckResponse": QuantConnectCheckResponse,
    "QuantConnectCheckResult": QuantConnectCheckResult,
}
