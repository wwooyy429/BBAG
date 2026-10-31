"""Read-only inspection of an explicitly anchored S2 control-ledger campaign.

This module never creates a TaskLedger, reserves an attempt, enrolls a runtime
grant, or contacts a provider. Input limits and scope are declarations supplied
by the caller; a successful offline snapshot is not permission to dispatch.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Annotated, Literal, Sequence, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from .contracts import GitCommit, NonNegativeInt, PositiveInt, Sha256, StrictContract, Text
from .jsonio import parse_json_object
from .s2_tasks import (
    RawMetadataRef, SynchronousResultMetadata, TaskAttempt, TaskBudget,
    TaskReconciliation, TaskRecord, TaskResponse, TaskSpec, canonical_task_bytes,
    task_configuration_id, task_request_id,
)

ComputeOperation = Literal["compile", "research_check"]
_TERMINAL = {"completed", "error", "incomplete"}
_ORDER = {"accepted": 1, "compiling": 2, "compiled": 3, "running": 4,
          "run_finished": 5, "result_available": 6, "completed": 7}
_TABLES = {"ledger_meta", "configurations", "tasks", "correlations", "attempts", "events"}
_COLUMNS = {
    "ledger_meta": [("key", "TEXT"), ("value", "TEXT")],
    "configurations": [("id", "TEXT")],
    "tasks": [("id", "TEXT"), ("configuration_id", "TEXT"), ("spec_json", "TEXT")],
    "correlations": [("correlation_id", "TEXT"), ("task_id", "TEXT")],
    "attempts": [("id", "TEXT"), ("task_id", "TEXT"), ("attempt_number", "INTEGER"),
                 ("operation", "TEXT"), ("attempt_json", "TEXT")],
    "events": [("attempt_id", "TEXT"), ("event_id", "TEXT"), ("payload_sha256", "TEXT"),
               ("payload_kind", "TEXT"), ("raw_json", "TEXT")],
}
_PRIMARY_KEYS = {"ledger_meta": ["key"], "configurations": ["id"], "tasks": ["id"],
                 "correlations": ["correlation_id"], "attempts": ["id"],
                 "events": ["attempt_id", "event_id"]}


class ReadinessError(ValueError):
    """An offline chain cannot be verified; no budget or history is modified."""

    def __init__(self, message: str, *, unknown_attempts: int | None = None,
                 active_attempts: int | None = None, unknown_billing_attempts: int | None = None):
        super().__init__(message)
        self.unknown_attempts = unknown_attempts
        self.active_attempts = active_attempts
        self.unknown_billing_attempts = unknown_billing_attempts


class ExpectedLedger(StrictContract):
    database: Text
    metadata_directory: Text
    expected_sha256: Sha256


class CampaignCaps(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    expected_ledgers: list[ExpectedLedger] = Field(min_length=1, max_length=32)
    compile_attempt_limit: Annotated[int, Field(strict=True, ge=0, le=2)]
    research_attempt_limit: Annotated[int, Field(strict=True, ge=0, le=3)]
    max_new_cost_microusd: Literal[0]
    project_id: PositiveInt
    response_class: Literal["CONTROL_ONLY", "SYNTHETIC_TEST"]
    start: date
    end: date
    assets: list[Literal["SPY", "AAPL"]] = Field(min_length=1, max_length=2)
    timezone: Literal["America/New_York"] = "America/New_York"
    approval_reference: Text

    @field_validator("max_new_cost_microusd", mode="before")
    @classmethod
    def explicit_integer_zero(cls, value: object) -> object:
        if type(value) is not int or value != 0:
            raise ValueError("new-cost cap must be an explicit integer zero")
        return value

    @model_validator(mode="after")
    def bounded_scope(self) -> Self:
        if not date(2020, 1, 2) <= self.start <= self.end <= date(2020, 1, 10):
            raise ValueError("offline scope must stay within the existing owner date whitelist")
        if len(set(self.assets)) != len(self.assets):
            raise ValueError("offline scope assets must be unique")
        pins = [item.expected_sha256 for item in self.expected_ledgers]
        if len(pins) != len(set(pins)):
            raise ValueError("duplicate/copy ledger digest is not a separate campaign history")
        return self


class PlatformGap(StrictContract):
    condition: Literal[
        "RAW_TO_STANDARD_TRACE", "HISTORICAL_UNIVERSE", "IDENTITY_EVENTS",
        "NONEMPTY_CORPORATE_ACTIONS", "CALENDAR_QUALITY_COVERAGE",
        "DISCLOSURE_REVISION_PIT", "PROVIDER_RECOVERY", "RESOURCE_CAPABILITY",
        "DEPLOYED_SOURCE_IDENTITY", "ALIAS_SID_BINDING",
    ]
    execution: Literal["NOT_RUN"] = "NOT_RUN"
    description: Text
    current_scope_coverage: Literal["PARTIAL", "NOT_COVERED"]


class AttemptProvenance(StrictContract):
    database_sha256: Sha256
    task_id: Sha256
    configuration_id: Sha256
    local_attempt_id: Text
    source_commit: GitCommit
    config_sha256: Sha256
    request_sha256: Sha256
    project_id: PositiveInt
    operation: ComputeOperation
    status: Literal["completed", "error", "incomplete"]
    actual_cost_microusd: NonNegativeInt | None
    reserved_cost_microusd: NonNegativeInt
    check_passed: bool | None


class LedgerInspection(StrictContract):
    database: Text
    metadata_directory: Text
    database_sha256: Sha256
    immutable_metadata_sha256: dict[Text, Sha256]
    task_count: PositiveInt
    attempt_count: PositiveInt
    persisted_budget: TaskBudget


class CampaignReadiness(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["OFFLINE_PREVIEW_ONLY"] = "OFFLINE_PREVIEW_ONLY"
    phase: Literal["IN_PROGRESS"] = "IN_PROGRESS"
    independent_review: Literal["PENDING"] = "PENDING"
    platform_integration: Literal["NOT_RUN"] = "NOT_RUN"
    economic_evidence: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    caps: CampaignCaps
    ledgers: list[LedgerInspection] = Field(min_length=1)
    attempts: list[AttemptProvenance] = Field(min_length=1)
    configuration_count: PositiveInt
    source_count: PositiveInt
    config_sha256_count: PositiveInt
    request_sha256_count: PositiveInt
    request_count: PositiveInt
    used_attempts: dict[ComputeOperation, NonNegativeInt]
    remaining_attempts: dict[ComputeOperation, NonNegativeInt]
    known_cost_microusd: NonNegativeInt
    reserved_unknown_cost_microusd: NonNegativeInt
    billing_unknown_attempt_count: NonNegativeInt
    actual_billing_total_microusd: NonNegativeInt | None
    actual_billing_total_known: bool
    platform_gaps: list[PlatformGap] = Field(min_length=1)
    scope_not_runtime_grant: Literal[True] = True
    scope_data_boundaries_not_verified_by_task_hashes: Literal[True] = True
    input_limits_not_authenticated: Literal[True] = True
    dispatch_allowed: Literal[False] = False
    runtime_grant_enrolled: Literal[False] = False
    new_runtime_ledger_created: Literal[False] = False
    new_task_reserved: Literal[False] = False
    zero_reservation_proves_zero_bill: Literal[False] = False
    merged_into_main: Literal[False] = False
    next_stage_authorized: Literal[False] = False

    @field_validator("scope_not_runtime_grant", "scope_data_boundaries_not_verified_by_task_hashes",
                     "input_limits_not_authenticated", "dispatch_allowed", "runtime_grant_enrolled",
                     "new_runtime_ledger_created", "new_task_reserved", "zero_reservation_proves_zero_bill",
                     "merged_into_main", "next_stage_authorized", mode="before")
    @classmethod
    def explicit_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("control flags require explicit booleans")
        return value

    @model_validator(mode="after")
    def accounting(self) -> Self:
        operations = {name: sum(a.operation == name for a in self.attempts)
                      for name in ("compile", "research_check")}
        limits = {"compile": self.caps.compile_attempt_limit, "research_check": self.caps.research_attempt_limit}
        if self.used_attempts != operations or self.remaining_attempts != {
            name: limits[name] - operations[name] for name in limits
        }:
            raise ValueError("remaining preview must deduct all actual unique attempts")
        if len({a.local_attempt_id for a in self.attempts}) != len(self.attempts):
            raise ValueError("attempt identities cannot be copied or counted twice")
        if self.configuration_count != len({a.configuration_id for a in self.attempts}) or self.request_count != len({a.task_id for a in self.attempts}):
            raise ValueError("configuration/request counts must reflect their canonical identities")
        if (self.source_count != len({a.source_commit for a in self.attempts})
                or self.config_sha256_count != len({a.config_sha256 for a in self.attempts})
                or self.request_sha256_count != len({a.request_sha256 for a in self.attempts})):
            raise ValueError("source/config/request digest counts must remain distinct")
        if (len(self.ledgers) != len({item.database_sha256 for item in self.ledgers})
                or {item.database_sha256 for item in self.ledgers} != {item.expected_sha256 for item in self.caps.expected_ledgers}
                or sum(item.attempt_count for item in self.ledgers) != len(self.attempts)
                or any(item.attempt_count != sum(a.database_sha256 == item.database_sha256 for a in self.attempts)
                       or item.task_count != len({a.task_id for a in self.attempts if a.database_sha256 == item.database_sha256})
                       for item in self.ledgers)):
            raise ValueError("ledger/source accounting must cover the whole declared chain")
        conditions = {gap.condition for gap in self.platform_gaps}
        if len(conditions) != len(self.platform_gaps) or conditions != {gap.condition for gap in platform_gaps()}:
            raise ValueError("offline readiness must retain every required unverified platform condition")
        unknown = [a for a in self.attempts if a.actual_cost_microusd is None]
        known = sum(a.actual_cost_microusd or 0 for a in self.attempts)
        if (self.billing_unknown_attempt_count != len(unknown) or self.known_cost_microusd != known
                or self.reserved_unknown_cost_microusd != sum(a.reserved_cost_microusd for a in unknown)
                or self.actual_billing_total_known != (not unknown)
                or self.actual_billing_total_microusd != (None if unknown else known)):
            raise ValueError("unknown actual bills must remain unknown")
        return self


S2_READINESS_CONTRACTS = {model.__name__: model for model in (
    ExpectedLedger, CampaignCaps, PlatformGap, AttemptProvenance, LedgerInspection, CampaignReadiness,
)}


def platform_gaps() -> list[PlatformGap]:
    """Required platform evidence is not supplied by this offline inspection."""
    descriptions = (
        ("RAW_TO_STANDARD_TRACE", "PARTIAL", "Real raw-to-standard value trace must be checked inside Cloud without data export."),
        ("HISTORICAL_UNIVERSE", "NOT_COVERED", "A fixed SPY/AAPL request does not prove historical constituent completeness or lagged liquidity coverage."),
        ("IDENTITY_EVENTS", "NOT_COVERED", "Nonempty real rename, listing and delisting event chains remain unverified."),
        ("NONEMPTY_CORPORATE_ACTIONS", "NOT_COVERED", "Nonempty real split/dividend conventions need authorized cases and independent in-Cloud comparison."),
        ("CALENDAR_QUALITY_COVERAGE", "PARTIAL", "At-least-one-bar flags do not prove all expected sessions, missing/stale cases or full coverage."),
        ("DISCLOSURE_REVISION_PIT", "NOT_COVERED", "Bar ordering does not establish supplier disclosure, revision or survivorship history."),
        ("PROVIDER_RECOVERY", "PARTIAL", "Local injected recovery is not actual provider timeout/restart/reconciliation evidence."),
        ("RESOURCE_CAPABILITY", "NOT_COVERED", "Actual supported tools, preregistered resource thresholds and independent platform review of the exact new SHA remain required; no self-approval."),
        ("DEPLOYED_SOURCE_IDENTITY", "PARTIAL", "The historical eight-file deployment proves only its old smoke scope. The local shared JSON reader has changed and new modules are not deployed. Recheck exact deployed identity; changed Cloud dependencies/uploads need explicit authorization and available compile budget."),
        ("ALIAS_SID_BINDING", "NOT_COVERED", "SPY/AAPL request aliases must not impersonate provider SIDs. Real raw-to-standard mapping needs a Cloud-internal identity binding without exporting SID records."),
    )
    return [PlatformGap(condition=name, current_scope_coverage=coverage, description=text)
            for name, coverage, text in descriptions]


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ReadinessError(message)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _path(value: str, root: Path) -> Path:
    path = Path(value)
    path = path if path.is_absolute() else root/path
    for current in (path, *path.parents):
        if os.path.lexists(current):
            meta = current.lstat()
            _require(not stat.S_ISLNK(meta.st_mode) and not getattr(meta, "st_file_attributes", 0) & 0x400,
                     "ledger inputs must not redirect through links or reparse points")
    resolved = path.resolve()
    _require(resolved.is_relative_to(root), "ledger inputs must remain within their explicit root")
    return resolved


def _file(path: Path, limit: int = 64_000_000) -> bytes:
    _require(path.is_file() and stat.S_ISREG(path.lstat().st_mode), "missing ordinary ledger/metadata file")
    _require(path.stat().st_size <= limit, "ledger/metadata file exceeds the offline size bound")
    return path.read_bytes()


def _model(model: type[StrictContract], raw: str | bytes, *, canonical: bool = True):
    # Canonical equality rejects duplicate JSON keys as well as loose numbers,
    # extra data and altered stored encodings, after strict model validation.
    value = model.model_validate_json(raw)
    body = raw.encode("utf-8") if isinstance(raw, str) else raw
    if canonical:
        _require(canonical_task_bytes(value) == body, "stored ledger JSON must be exact canonical control metadata")
    return value


def _metadata(directory: Path) -> dict[str, bytes]:
    _require(directory.is_dir(), "missing original metadata directory")
    result = {}
    total = 0
    for path in directory.iterdir():
        _require(len(result) < 4096, "control metadata inventory exceeds the offline bound")
        _require(not path.is_symlink() and not getattr(path.lstat(), "st_file_attributes", 0) & 0x400,
                 "metadata links/reparse points are forbidden")
        _require(path.is_file() and stat.S_ISREG(path.lstat().st_mode), "metadata must contain ordinary control files only")
        raw = _file(path, 65_536)
        _require(0 < len(raw) <= 65_536, "metadata bytes exceed the control contract bound")
        result[path.name] = raw
        total += len(raw)
        _require(total <= 64_000_000, "control metadata total exceeds the offline bound")
    return result


def _binding(spec: TaskSpec, payload) -> None:
    _require(all(getattr(spec, key) == getattr(payload, key) for key in (
        "operation", "source_commit", "config_sha256", "request_sha256", "project_id", "response_class"
    )), "immutable event source/config/request binding differs from its task")


def _identified(state: dict) -> bool:
    return bool(state["platform_task_id"] or (state["invocation_id"] is not None and state["cell_index"] is not None))


def _event_state(spec: TaskSpec, attempt: TaskAttempt, events: dict[str, tuple[RawMetadataRef, object]]) -> None:
    state = dict(status="UNKNOWN", reported_status=None, platform_task_id=None,
                 invocation_id=None, cell_index=None, actual_cost_microusd=None,
                 success=None, check_passed=None, error_category="NONE", reconciliation=None, sync_confirmation=None)
    frozen_verdict = None
    for ref in attempt.raw_metadata:
        stored_ref, event = events[ref.relative_path]
        _require(stored_ref == ref, "attempt event references differ from stored event metadata")
        if isinstance(event, TaskReconciliation):
            _binding(spec, event)
            _require(state["status"] == "UNKNOWN" and not _identified(state)
                     and attempt.created_at <= event.checked_at <= datetime.now(timezone.utc),
                     "not-submitted proof contradicts the observed attempt")
            state.update(status="incomplete", reconciliation=event)
            continue
        sync = isinstance(event, SynchronousResultMetadata)
        response = event.response if sync else event
        _binding(spec, response)
        if sync:
            proof = event.confirmation
            _require(state["status"] == "UNKNOWN" and not any(state[k] is not None for k in ("platform_task_id", "invocation_id", "cell_index"))
                     and proof.local_attempt_id == attempt.local_attempt_id
                     and attempt.created_at <= proof.observed_at <= datetime.now(timezone.utc),
                     "synchronous observation does not bind the current reservation")
            state["sync_confirmation"] = proof
        previous_status = state["status"]
        for key in ("platform_task_id", "invocation_id", "cell_index", "actual_cost_microusd"):
            incoming, previous = getattr(response, key), state[key]
            _require(previous is None or incoming is None or previous == incoming,
                     "confirmed execution identity or actual bill changed")
            if incoming is not None:
                state[key] = incoming
        status = response.status
        if not sync and not _identified(state) and state["sync_confirmation"] is None and status != "UNKNOWN":
            if status == "incomplete":
                _require(state["reconciliation"] is not None, "ordinary incomplete import cannot replace not-submitted reconciliation")
            else:
                status = "UNKNOWN"
        _require(previous_status not in _TERMINAL or status == previous_status, "terminal event state changed")
        _require(not (previous_status in _ORDER and status in _ORDER and _ORDER[status] < _ORDER[previous_status]),
                 "event progress regressed")
        lost_poll = status == "UNKNOWN" and previous_status != "UNKNOWN"
        if lost_poll:
            status = previous_status
        else:
            state["success"] = response.success
            if frozen_verdict is not None:
                _require(response.check_passed is None or response.check_passed is frozen_verdict,
                         "an immutable known check verdict changed")
                state["check_passed"] = frozen_verdict
            else:
                state["check_passed"] = response.check_passed
        state.update(status=status, reported_status=response.status, error_category=response.error_category)
        if state["check_passed"] is not None and status in {"result_available", "completed"} and (_identified(state) or state["sync_confirmation"] is not None):
            frozen_verdict = state["check_passed"]
    _require(all(getattr(attempt, key) == value for key, value in state.items()),
             "persisted attempt snapshot differs from its immutable event history")


def _snapshot_authorizer(action: int, first: str | None, second: str | None,
                         database: str | None, origin: str | None) -> int:
    """Allow only the fixed inspector's reads, including its schema PRAGMAs."""
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and database == "main" and first in _TABLES | {"sqlite_master", "sqlite_schema"}:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA:
        if first == "table_info" and second in _TABLES:
            return sqlite3.SQLITE_OK
        if second is None and first in {"integrity_check", "foreign_key_check", "query_only", "trusted_schema", "temp_store", "database_list"}:
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _ledger_snapshot(raw_db: bytes) -> sqlite3.Connection:
    """Query an isolated image derived solely from the already anchored bytes.

    SQLite's deserialize API cannot read a WAL-format image. For a closed,
    sidecar-free ledger only, adapt the two journal-mode header bytes in this
    private memory image as prescribed by sqlite.org/c3ref/deserialize.html.
    All page content comes from raw_db; its digest and the original disk bytes
    stay unchanged. Never reopen the input path or fall back to a fresh ledger.
    """
    _require(100 <= len(raw_db) <= 64_000_000 and raw_db[:16] == b"SQLite format 3\x00"
             and raw_db[18:20] in {b"\x01\x01", b"\x02\x02"}, "unsupported SQLite snapshot header")
    image = raw_db[:18] + b"\x01\x01" + raw_db[20:]
    connection = sqlite3.connect(":memory:")
    try:
        _require(callable(getattr(connection, "deserialize", None)),
                 "SQLite deserialize support is required for anchored offline inspection")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.deserialize(image)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        _require(connection.execute("PRAGMA query_only").fetchone() == (1,)
                 and connection.execute("PRAGMA trusted_schema").fetchone() == (0,)
                 and connection.execute("PRAGMA temp_store").fetchone() == (2,)
                 and connection.execute("PRAGMA database_list").fetchall() == [(0, "main", "")],
                 "SQLite snapshot must remain isolated and query-only")
        connection.set_authorizer(_snapshot_authorizer)
        return connection
    except BaseException:
        connection.close()
        raise


def _inspect_ledger(expected: ExpectedLedger, root: Path, caps: CampaignCaps):
    database, directory = _path(expected.database, root), _path(expected.metadata_directory, root)
    for suffix in ("-wal", "-shm", "-journal"):
        _require(not os.path.lexists(str(database)+suffix), "ledger must be closed/checkpointed; journal sidecars are forbidden")
    raw_db = _file(database)
    _require(len(raw_db) <= 64_000_000 and _sha(raw_db) == expected.expected_sha256, "ledger bytes differ from the externally declared anchor")
    raw_metadata = _metadata(directory)
    with closing(_ledger_snapshot(raw_db)) as connection:
        _require(connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                 and not connection.execute("PRAGMA foreign_key_check").fetchall(), "ledger database integrity failed")
        objects = connection.execute("SELECT type,name FROM sqlite_master WHERE type IN ('table','view','trigger')").fetchall()
        _require(set(objects) == {("table", name) for name in _TABLES}, "unexpected ledger tables/views/triggers")
        for table, expected_columns in _COLUMNS.items():
            info = connection.execute("PRAGMA table_info("+table+")").fetchall()
            keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            _require([(row[1], row[2]) for row in info] == expected_columns and keys == _PRIMARY_KEYS[table],
                     "ledger column/primary-key schema differs from its declared version")
        meta = dict(connection.execute("SELECT key,value FROM ledger_meta"))
        _require(set(meta) == {"budget", "schema_version", "raw_dir"} and meta["schema_version"] == "1.0"
                 and Path(meta["raw_dir"]).is_absolute() and _path(meta["raw_dir"], root) == directory,
                 "original absolute metadata binding differs")
        budget = _model(TaskBudget, meta["budget"])
        _require(budget.response_class == caps.response_class and budget.max_cost_microusd == 0
                 and set(budget.operation_limits) <= {"compile", "research_check"}, "persisted budget is outside this engineering campaign")
        specs = {}
        for task_id, configuration, raw in connection.execute("SELECT id,configuration_id,spec_json FROM tasks"):
            spec = _model(TaskSpec, raw)
            _require(task_id == task_request_id(spec) and configuration == task_configuration_id(spec)
                     and spec.operation in {"compile", "research_check"} and spec.project_id == caps.project_id
                     and spec.response_class == caps.response_class, "task identity/project/operation differs from campaign scope")
            specs[task_id] = (spec, configuration)
        configurations = [row[0] for row in connection.execute("SELECT id FROM configurations")]
        _require(specs and set(configurations) == {value[1] for value in specs.values()}
                 and len(configurations) <= budget.max_configurations, "empty or unbound configuration/task inventory")
        aliases = list(connection.execute("SELECT correlation_id,task_id FROM correlations"))
        _require(all(task in specs for _, task in aliases), "correlation points outside the task inventory")
        attempts = {}
        for identity, task_id, number, operation, raw in connection.execute("SELECT id,task_id,attempt_number,operation,attempt_json FROM attempts"):
            attempt = _model(TaskAttempt, raw)
            _require(identity == attempt.local_attempt_id and task_id == attempt.task_id and number == attempt.attempt_number
                     and task_id in specs and operation == specs[task_id][0].operation
                     and attempt.created_at <= datetime.now(timezone.utc), "attempt row columns/time differ from canonical task identity")
            attempts[identity] = attempt
        _require(attempts and len(attempts) <= budget.max_attempts, "empty campaign or persisted attempt limit exceeded")
        operation_counts = Counter(specs[a.task_id][0].operation for a in attempts.values())
        _require(all(count <= budget.operation_limits.get(name, 0) for name, count in operation_counts.items()), "persisted operation budget exceeded")
        events = {identity: {} for identity in attempts}
        referenced = set()
        rows = connection.execute("SELECT attempt_id,event_id,payload_sha256,payload_kind,raw_json FROM events").fetchall()
        _require(len(rows) <= 4096, "control event inventory exceeds the offline bound")
        for identity, event_id, digest, kind, raw in rows:
            reference = _model(RawMetadataRef, raw)
            _require(identity in attempts and kind == reference.payload_kind and reference.sha256 == digest
                     and reference.relative_path not in referenced and reference.relative_path in raw_metadata,
                     "unbound, duplicated or missing immutable metadata event")
            payload = raw_metadata[reference.relative_path]
            _require(len(payload) == reference.size_bytes and _sha(payload) == digest, "immutable metadata bytes/hash differ")
            model = {"TaskResponse": TaskResponse, "TaskReconciliation": TaskReconciliation,
                     "SynchronousResultMetadata": SynchronousResultMetadata}[kind]
            # Provider bytes need not be canonical, but duplicate keys/nonfinite
            # fields must be rejected before JSON-mode typed validation.
            checked = parse_json_object(payload)
            event = model.model_validate_json(json.dumps(checked, allow_nan=False))
            if reference.capture_kind == "LOCAL_NORMALIZED_METADATA":
                _require(payload == canonical_task_bytes(event), "normalized local metadata bytes are not canonical")
            _require(type(event_id) is str and 1 <= len(event_id) <= 128
                     and all(c.isascii() and (c.isalnum() or c in "_.:-") for c in event_id), "unsafe event identifier")
            events[identity][reference.relative_path] = (reference, event)
            referenced.add(reference.relative_path)
        _require(referenced == set(raw_metadata), "unreferenced or missing immutable control metadata")
        for task_id, (spec, _) in specs.items():
            ordered = sorted((a for a in attempts.values() if a.task_id == task_id), key=lambda a: a.attempt_number)
            _require(ordered, "registered campaign task has no actual attempt")
            TaskRecord(task_id=task_id, configuration_id=task_configuration_id(spec), spec=spec,
                       correlation_ids=[alias for alias, task in aliases if task == task_id],
                       status=ordered[-1].status, attempts=ordered)
        for identity, attempt in attempts.items():
            _require(len(attempt.raw_metadata) == len(events[identity]), "attempt event inventory differs")
            _event_state(specs[attempt.task_id][0], attempt, events[identity])
    _require(_file(database) == raw_db and _metadata(directory) == raw_metadata
             and not any(os.path.lexists(str(database)+suffix) for suffix in ("-wal", "-shm", "-journal")),
             "original ledger/metadata changed during read-only inspection")
    inspection = LedgerInspection(database=str(database), metadata_directory=str(directory), database_sha256=expected.expected_sha256,
        immutable_metadata_sha256={name: _sha(raw) for name, raw in raw_metadata.items()}, task_count=len(specs),
        attempt_count=len(attempts), persisted_budget=budget)
    return inspection, [(specs[a.task_id][0], specs[a.task_id][1], a) for a in attempts.values()]


def inspect_campaign(caps: CampaignCaps, *, root: Path | None = None,
                     ledgers: Sequence[ExpectedLedger] | None = None) -> CampaignReadiness:
    """Verify the complete declared anchored chain and derive a non-runtime preview.

    ``caps`` is not authentication of the owner. Its anchor list must come from
    the caller's retained campaign context; changing a JSON configuration cannot
    grant permissions. An explicit ledger override must equal that full list.
    """
    try:
        caps = CampaignCaps.model_validate(caps)
        base = (root or Path.cwd()).resolve()
        def identity(item):
            item = ExpectedLedger.model_validate(item)
            return (_path(item.database, base), _path(item.metadata_directory, base), item.expected_sha256)
        declared = [identity(item) for item in caps.expected_ledgers]
        selected = caps.expected_ledgers if ledgers is None else [ExpectedLedger.model_validate(item) for item in ledgers]
        actual = [identity(item) for item in selected]
        _require(len(actual) == len(set(actual)) == len(declared) and set(actual) == set(declared),
                 "supply the complete declared ledger chain exactly once")
        _require(len({item[0] for item in actual}) == len(actual) and len({item[2] for item in actual}) == len(actual),
                 "duplicate/copied database cannot supply additional history")
        file_ids = [(item[0].stat().st_dev, item[0].stat().st_ino) for item in actual]
        _require(len(set(file_ids)) == len(file_ids), "the same database cannot be selected through multiple file identities")
        inspections, entries = [], []
        for item in selected:
            inspection, rows = _inspect_ledger(item, base, caps)
            inspections.append(inspection)
            entries.extend((inspection.database_sha256, *row) for row in rows)
        # Recheck the whole chain together. A writer may otherwise alter an
        # already-read predecessor while a later ledger is being inspected.
        for inspection in inspections:
            database = _path(inspection.database, base)
            directory = _path(inspection.metadata_directory, base)
            _require(_sha(_file(database)) == inspection.database_sha256
                     and {name: _sha(raw) for name, raw in _metadata(directory).items()} == inspection.immutable_metadata_sha256
                     and not any(os.path.lexists(str(database)+suffix) for suffix in ("-wal", "-shm", "-journal")),
                     "declared chain changed during read-only inspection")
        ids, provider_ids = set(), set()
        unknown = sum(attempt.status == "UNKNOWN" for _, _, _, attempt in entries)
        active = sum(attempt.status not in _TERMINAL for _, _, _, attempt in entries)
        unknown_billing = sum(attempt.actual_cost_microusd is None for _, _, _, attempt in entries)
        if unknown or active:
            raise ReadinessError("unresolved/active campaign attempts must be reconciled; their occupancy and unknown bills are retained",
                                 unknown_attempts=unknown, active_attempts=active, unknown_billing_attempts=unknown_billing)
        result_attempts = []
        for database_hash, spec, configuration, attempt in entries:
            _require(attempt.local_attempt_id not in ids, "copied attempt identity across ledgers")
            ids.add(attempt.local_attempt_id)
            execution = ((spec.project_id, spec.operation, "provider", attempt.platform_task_id)
                         if attempt.platform_task_id is not None else
                         (spec.project_id, spec.operation, "invocation", attempt.invocation_id, attempt.cell_index)
                         if attempt.invocation_id is not None and attempt.cell_index is not None else None)
            if execution is not None:
                _require(execution not in provider_ids, "same observed provider execution appears in multiple attempts")
                provider_ids.add(execution)
            result_attempts.append(AttemptProvenance(database_sha256=database_hash, task_id=attempt.task_id,
                configuration_id=configuration, local_attempt_id=attempt.local_attempt_id, source_commit=spec.source_commit,
                config_sha256=spec.config_sha256, request_sha256=spec.request_sha256, project_id=spec.project_id,
                operation=spec.operation, status=attempt.status, actual_cost_microusd=attempt.actual_cost_microusd,
                reserved_cost_microusd=attempt.reserved_cost_microusd, check_passed=attempt.check_passed))
        used = {name: sum(a.operation == name for a in result_attempts) for name in ("compile", "research_check")}
        limits = {"compile": caps.compile_attempt_limit, "research_check": caps.research_attempt_limit}
        _require(all(used[name] <= limits[name] for name in limits), "actual campaign attempts exceed the explicit cumulative owner caps")
        known = sum(a.actual_cost_microusd or 0 for a in result_attempts)
        reserved = sum(a.reserved_cost_microusd for a in result_attempts if a.actual_cost_microusd is None)
        _require(known + reserved <= caps.max_new_cost_microusd, "recorded/reserved campaign cost exceeds the explicit cap; unknown bills are not zero bills")
        return CampaignReadiness(caps=caps, ledgers=inspections, attempts=result_attempts,
            configuration_count=len({a.configuration_id for a in result_attempts}), request_count=len({a.task_id for a in result_attempts}),
            source_count=len({a.source_commit for a in result_attempts}), config_sha256_count=len({a.config_sha256 for a in result_attempts}),
            request_sha256_count=len({a.request_sha256 for a in result_attempts}),
            used_attempts=used, remaining_attempts={name: limits[name]-used[name] for name in limits},
            known_cost_microusd=known, reserved_unknown_cost_microusd=reserved, billing_unknown_attempt_count=unknown_billing,
            actual_billing_total_microusd=None if unknown_billing else known, actual_billing_total_known=not unknown_billing,
            platform_gaps=platform_gaps())
    except ReadinessError:
        raise
    except (ValidationError, ValueError, TypeError, KeyError, OSError, sqlite3.Error):
        raise ReadinessError("campaign inputs are not a consistent closed anchored control history") from None
