"""Fixed control receipts for the Cloud-local standardization bridge.

Provider callbacks run only through the bridge's original-alias preflight.
This module neither imports an SDK nor reserves or submits a platform task.
Its local checks are not independent platform acceptance or provider truth.
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import gc
import io
import logging
from pathlib import Path
import sys
import threading
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


class _OutputIsolationRejected(ValueError):
    """Closed failure before provider callbacks in unsupported host contexts."""


_OUTPUT_LOCK = threading.Lock()
_LOGGING_METHODS = (
    (logging.Logger, "isEnabledFor"), (logging.Logger, "_log"), (logging.Logger, "handle"),
    (logging.Logger, "warn"), (logging.LoggerAdapter, "warn"),
    (logging.Handler, "handle"), (logging.StreamHandler, "emit"),
    *((logging, name) for name in ("log", "debug", "info", "warning", "warn", "error", "exception", "critical", "fatal")),
    (logging, "basicConfig"),
    (threading.Thread, "start"),
)
_LOGGER_METHODS = frozenset(("debug", "info", "warning", "warn", "error", "exception", "critical",
    "fatal", "log", "_log", "handle", "callHandlers", "makeRecord", "isEnabledFor", "getEffectiveLevel"))
_ADAPTER_METHODS = frozenset(("debug", "info", "warning", "warn", "error", "exception", "critical",
    "fatal", "log", "_log", "isEnabledFor", "process"))


def _discard_log(*args, **kwargs):
    return None


def _logging_disabled(*args, **kwargs):
    return False


def _thread_start_rejected(*args, **kwargs):
    raise _OutputIsolationRejected("OUTPUT_ISOLATION_UNAVAILABLE")


def _stock_method(cls, name):
    method = cls.__dict__.get(name)
    code = getattr(method, "__code__", None)
    module = threading if cls is threading.Thread else logging
    qualified = name if cls is logging else cls.__name__ + "." + name
    aliases = {qualified, qualified.rsplit(".", 1)[0] + ".critical"} if name == "fatal" and cls is not logging else {qualified}
    if name == "fatal" and cls is logging:
        aliases.add("critical")
    if (code is None or Path(code.co_filename).resolve() != Path(module.__file__).resolve()
            or getattr(method, "__module__", None) != module.__name__
            or getattr(method, "__qualname__", None) not in aliases):
        raise _OutputIsolationRejected("OUTPUT_ISOLATION_UNAVAILABLE")
    return method


def _logging_preflight() -> tuple[list[tuple[object, str, object]], list[tuple[dict, object]]]:
    """Support stock synchronous logging, including direct unregistered Logger.

    This is an application output boundary, not isolation from arbitrary Python,
    native file descriptors, tracing hooks or code that replaces logging APIs.
    Callbacks must use the stock synchronous API, without replacing logging
    implementations, dynamically reconfiguring the host or using native IO.
    """
    if (threading.current_thread() is not threading.main_thread()
            or len(threading.enumerate()) != 1 or sys.gettrace() is not None or sys.getprofile() is not None
            or gc.get_freeze_count() != 0
            or type(logging.root) is not logging.RootLogger
            or type(logging.root.manager) is not logging.Manager
            or type(logging.root.manager.loggerDict) is not dict
            or logging.root.manager.root is not logging.root
            or logging.root.manager.loggerClass not in {None, logging.Logger}
            or logging.getLoggerClass() is not logging.Logger
            or logging.getLogRecordFactory() is not logging.LogRecord):
        raise _OutputIsolationRejected("OUTPUT_ISOLATION_UNAVAILABLE")
    if _LOGGER_METHODS.intersection(logging.RootLogger.__dict__):
        raise _OutputIsolationRejected("OUTPUT_ISOLATION_UNAVAILABLE")
    methods = []
    for cls, name in _LOGGING_METHODS:
        methods.append((cls, name, _stock_method(cls, name)))
    for cls, names in ((logging.Logger, _LOGGER_METHODS | {"__init__"}), (logging.LoggerAdapter, _ADAPTER_METHODS | {"__init__"}),
                       (logging, {"getLogger"}), (logging.Manager, {"getLogger"})):
        for name in names:
            if cls is logging.LoggerAdapter and name == "fatal" and name not in cls.__dict__:
                continue  # Some supported stdlib versions expose critical only.
            _stock_method(cls, name)
    # loggerDict omits logging.Logger(...) objects. GC-visible ordinary Logger
    # instances cover that public constructor without touching manager.disable
    # or its registry-only cache invalidation. No object repr/callback is used.
    streams = []
    for value in gc.get_objects():
        cls = type(value)
        if issubclass(cls, logging.Logger):
            if cls not in {logging.Logger, logging.RootLogger}:
                raise _OutputIsolationRejected("OUTPUT_ISOLATION_UNAVAILABLE")
            state = vars(value)
            if (_LOGGER_METHODS.intersection(state) or value.manager is not logging.root.manager
                    or type(value._cache) is not dict):
                raise _OutputIsolationRejected("OUTPUT_ISOLATION_UNAVAILABLE")
        elif issubclass(cls, logging.LoggerAdapter):
            if (cls is not logging.LoggerAdapter or _ADAPTER_METHODS.intersection(vars(value))
                    or type(vars(value).get("logger")) not in {logging.Logger, logging.RootLogger}):
                raise _OutputIsolationRejected("OUTPUT_ISOLATION_UNAVAILABLE")
        elif cls in {logging.StreamHandler, logging.FileHandler}:
            state = vars(value)
            if "stream" not in state:
                raise _OutputIsolationRejected("OUTPUT_ISOLATION_UNAVAILABLE")
            streams.append((state, state["stream"]))
    return methods, streams


@contextmanager
def _isolated_output():
    """Suppress ordinary logging before records, filters or handlers execute.

    A process-wide non-reentrant gate rejects overlapping/nested calls. Reject
    unsupported hosts before resolver/fetcher IO. Restore exact original method
    objects and sys streams even when a callback raises BaseException. Handler
    streams of existing stock StreamHandler/FileHandler are redirected through
    their plain instance dictionary without flush/close, then restored. Other
    logger/handler configuration, manager.disable and caches stay unchanged.
    Module convenience calls are sealed before automatic basicConfig, and new
    Python Thread.start is rejected, so work cannot outlive this synchronous
    output scope. Arbitrary saved function references/native IO are not sandboxed.
    """
    if not _OUTPUT_LOCK.acquire(blocking=False):
        raise _OutputIsolationRejected("OUTPUT_ISOLATION_UNAVAILABLE")
    methods = []
    streams = []
    try:
        methods, streams = _logging_preflight()
        sink = _DiscardOutput()
        with redirect_stdout(sink), redirect_stderr(sink):
            try:
                for state, _ in streams:
                    state["stream"] = sink
                for cls, name, _ in methods:
                    replacement = (_thread_start_rejected if cls is threading.Thread else
                                   _logging_disabled if name == "isEnabledFor" else _discard_log)
                    setattr(cls, name, replacement)
                yield
            finally:
                for cls, name, original in reversed(methods):
                    setattr(cls, name, original)
                for state, stream in streams:
                    state["stream"] = stream
    finally:
        _OUTPUT_LOCK.release()


def run_cloud_data_check(plan: CloudDataPlan, *, guard: AccessGuard,
                         resolver: Callable, fetcher: Callable) -> CloudStandardizationReceipt:
    """Return fixed checks only; never return SIDs, market values or data hashes.

    Separate runtime task reservation, current source verification and owner
    execution authorization are required before a real Cloud application calls
    this function. Callback assertions are not authenticated provider evidence.
    Offline tests supply synthetic mocks; execution_location is a declaration,
    not proof that this Python process is hosted by QuantConnect.
    Output isolation supports synchronous, single-main-thread stock Python
    logging. Unsupported hooks, logger implementations or overlapping calls are
    rejected before provider IO. Arbitrary Python/native bypass is not sandboxed.
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
        with _isolated_output():
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
    except BaseException:
        return CloudStandardizationReceipt(binding=binding, outcome="ERROR", error_category="STANDARDIZATION_REJECTED")


S2_CLOUD_CHECK_CONTRACTS = {model.__name__: model for model in (
    CloudStandardizationChecks, CloudStandardizationReceipt,
)}
