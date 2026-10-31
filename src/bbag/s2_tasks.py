"""Durable, bounded S2 control-plane tasks; this module makes no platform calls.

Identifiers beginning ``local_`` are explicitly local bookkeeping identifiers.
Saved response bytes contain allowlisted control metadata, never market data.
Callback responses and reconciliation assertions remain unauthenticated evidence.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Annotated, Any, Iterator, Literal, Protocol, Self
import uuid

from pydantic import AwareDatetime, Field, StringConstraints, ValidationError, field_validator, model_validator

from .contracts import GitCommit, NonNegativeInt, PositiveInt, Sha256, StrictContract


Operation = Literal[
    "compile", "research_check", "data_query", "project_read", "file_read", "research_read"
]
TaskStatus = Literal[
    "accepted", "compiling", "compiled", "running", "run_finished", "result_available",
    "completed", "error", "incomplete", "UNKNOWN",
]
ResponseClass = Literal["CONTROL_ONLY", "SYNTHETIC_TEST"]
ErrorCategory = Literal[
    "NONE", "INVALID_REQUEST", "ACCESS_DENIED", "CAPABILITY_UNAVAILABLE", "PROVIDER_ERROR",
    "TIMEOUT", "MALFORMED_RESPONSE",
]
Identifier = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
]
BudgetCount = Annotated[int, Field(strict=True, ge=0, le=100_000)]
Money = Annotated[int, Field(strict=True, ge=0, le=10**15)]
_ASYNC_OPERATIONS = {"compile", "research_check", "data_query"}
_TERMINAL = {"completed", "error", "incomplete"}
_ORDER = {
    "accepted": 1, "compiling": 2, "compiled": 3, "running": 4,
    "run_finished": 5, "result_available": 6, "completed": 7,
}
_REDIRECT_TAGS = {0xA0000003, 0xA000000C}


class TaskLedgerError(ValueError):
    """Safe control-plane error; never includes rejected response contents."""


class TaskBudget(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    response_class: ResponseClass = "CONTROL_ONLY"
    max_configurations: BudgetCount = 1
    max_attempts: BudgetCount = 0
    max_concurrent_attempts: Annotated[int, Field(strict=True, ge=1, le=1000)] = 1
    max_cost_microusd: Money = 0
    operation_limits: dict[Operation, BudgetCount] = Field(default_factory=dict)


class TaskSpec(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    correlation_id: Identifier
    operation: Operation
    source_commit: GitCommit
    config_sha256: Sha256
    request_sha256: Sha256
    project_id: PositiveInt | None = None
    response_class: ResponseClass = "CONTROL_ONLY"


class TaskResponse(StrictContract):
    """Whitelisted metadata, not arbitrary provider JSON or a market-data digest."""

    schema_version: Literal["1.0"] = "1.0"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    operation: Operation
    source_commit: GitCommit
    config_sha256: Sha256
    request_sha256: Sha256
    project_id: PositiveInt | None = None
    response_class: ResponseClass = "CONTROL_ONLY"
    status: TaskStatus
    platform_task_id: Identifier | None = None
    invocation_id: Identifier | None = None
    cell_index: NonNegativeInt | None = None
    success: bool | None = None
    check_passed: bool | None = None
    error_category: ErrorCategory = "NONE"
    actual_cost_microusd: Money | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.status == "completed" and self.success is not True:
            raise ValueError("completed requires execution success, separate from check verdict")
        if self.status == "error" and (
            self.success is not False or self.error_category == "NONE"
        ):
            raise ValueError("error requires an error category and success=false")
        if self.status not in {"error", "UNKNOWN"} and self.error_category != "NONE":
            raise ValueError("error category is inconsistent with status")
        if self.check_passed is not None and self.status not in {"result_available", "completed"}:
            raise ValueError("a check verdict requires an available result")
        if self.cell_index is not None and self.invocation_id is None:
            raise ValueError("cell index requires an observed invocation identifier")
        if self.invocation_id is not None and self.project_id is None:
            raise ValueError("invocation identifier requires an observed project")
        return self


class SynchronousCompletionRecord(StrictContract):
    """Trusted application's assertion about a fresh synchronous tool return.

    This is deliberately an explicit API separate from importing a provider
    status claim. It authenticates neither the caller nor the platform. The
    application must actually observe the reserved invocation returning before
    constructing this record; arbitrary JSON is not proof of that observation.
    Its digest covers only allowlisted TaskResponse metadata, never market data.
    """

    schema_version: Literal["1.0"] = "1.0"
    response_kind: Literal["SYNCHRONOUS_TOOL_RETURN"] = "SYNCHRONOUS_TOOL_RETURN"
    local_attempt_id: Annotated[
        str, StringConstraints(strict=True, pattern=r"^local_[0-9a-f]{32}$")
    ]
    observed_at: AwareDatetime
    operation: Literal["compile", "research_check"]
    source_commit: GitCommit
    config_sha256: Sha256
    request_sha256: Sha256
    project_id: PositiveInt
    response_class: ResponseClass = "CONTROL_ONLY"
    completion_observed: Literal[True] = True
    provider_task_id_availability: Literal["NOT_RETURNED"] = "NOT_RETURNED"
    response_metadata_sha256: Sha256
    evidence_authentication: Literal["NOT_AUTHENTICATED"] = "NOT_AUTHENTICATED"

    @field_validator("completion_observed", mode="before")
    @classmethod
    def strict_completion(cls, value: object) -> object:
        if type(value) is not bool or value is not True:
            raise ValueError("completion must be an explicit boolean observation")
        return value


class SynchronousResultMetadata(StrictContract):
    """Complete normalized safe response plus its explicit application assertion."""

    response: TaskResponse
    confirmation: SynchronousCompletionRecord

    @model_validator(mode="after")
    def consistent(self) -> Self:
        response, proof = self.response, self.confirmation
        if response.status not in {"completed", "error"}:
            raise ValueError("synchronous confirmation requires a finished tool return")
        if any(getattr(response, field) is not None for field in (
            "platform_task_id", "invocation_id", "cell_index"
        )):
            raise ValueError("identified executions use normal task event reconciliation")
        for key in (
            "operation", "source_commit", "config_sha256", "request_sha256", "project_id", "response_class"
        ):
            if getattr(response, key) != getattr(proof, key):
                raise ValueError("synchronous response and confirmation identities differ")
        if hashlib.sha256(canonical_task_bytes(response)).hexdigest() != proof.response_metadata_sha256:
            raise ValueError("synchronous confirmation must bind the exact safe metadata")
        return self


class TaskReconciliation(StrictContract):
    """Explicit, unauthenticated assertion that the prior submission did not occur.

    The caller must actually perform this lookup/manual reconciliation. Supplying
    the model itself is not authentication or authorization for another attempt.
    """

    kind: Literal["NOT_SUBMITTED"] = "NOT_SUBMITTED"
    evidence_authentication: Literal["NOT_AUTHENTICATED"] = "NOT_AUTHENTICATED"
    lookup_method: Literal[
        "PROVIDER_IDEMPOTENCY_LOOKUP", "PROVIDER_REQUEST_LOOKUP", "OWNER_MANUAL_RECONCILIATION"
    ]
    checked_at: AwareDatetime
    operation: Operation
    source_commit: GitCommit
    config_sha256: Sha256
    request_sha256: Sha256
    project_id: PositiveInt | None = None
    response_class: ResponseClass = "CONTROL_ONLY"


class RawMetadataRef(StrictContract):
    relative_path: Annotated[
        str, StringConstraints(strict=True, pattern=r"^metadata_[0-9a-f]{32}_[0-9a-f]{64}\.json$")
    ]
    sha256: Sha256
    size_bytes: Annotated[int, Field(strict=True, ge=1, le=65_536)]
    capture_kind: Literal["LOCAL_NORMALIZED_METADATA", "CALLER_METADATA_BYTES"]
    payload_kind: Literal["TaskResponse", "TaskReconciliation", "SynchronousResultMetadata"]
    evidence_authentication: Literal["NOT_AUTHENTICATED"] = "NOT_AUTHENTICATED"
    market_data_export: Literal["NOT_SUPPORTED"] = "NOT_SUPPORTED"

    @model_validator(mode="after")
    def path_matches_hash(self) -> Self:
        if not self.relative_path.endswith("_" + self.sha256 + ".json"):
            raise ValueError("metadata filename must include its exact hash")
        return self


class TaskAttempt(StrictContract):
    local_attempt_id: Annotated[
        str, StringConstraints(strict=True, pattern=r"^local_[0-9a-f]{32}$")
    ]
    task_id: Sha256
    attempt_number: PositiveInt
    created_at: AwareDatetime
    status: TaskStatus
    reported_status: TaskStatus | None = None
    platform_task_id: Identifier | None = None
    invocation_id: Identifier | None = None
    cell_index: NonNegativeInt | None = None
    reserved_cost_microusd: Money
    actual_cost_microusd: Money | None = None
    success: bool | None = None
    check_passed: bool | None = None
    error_category: ErrorCategory = "NONE"
    reconciliation: TaskReconciliation | None = None
    sync_confirmation: SynchronousCompletionRecord | None = None
    raw_metadata: list[RawMetadataRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.status == "completed" and self.success is not True:
            raise ValueError("completed attempt requires execution success")
        if self.cell_index is not None and self.invocation_id is None:
            raise ValueError("cell index requires invocation identity")
        if self.reconciliation is not None and (
            self.status != "incomplete" or self.platform_task_id is not None
            or self.invocation_id is not None
        ):
            raise ValueError("not-submitted reconciliation contradicts observed execution")
        if self.sync_confirmation is not None and (
            self.status not in {"completed", "error"}
            or self.platform_task_id is not None or self.invocation_id is not None
            or self.cell_index is not None or self.reconciliation is not None
            or self.sync_confirmation.local_attempt_id != self.local_attempt_id
            or self.sync_confirmation.observed_at < self.created_at
        ):
            raise ValueError("synchronous confirmation contradicts the reserved attempt")
        names = [reference.relative_path for reference in self.raw_metadata]
        if len(names) != len(set(names)):
            raise ValueError("metadata references must be unique")
        return self


class TaskRecord(StrictContract):
    task_id: Sha256
    configuration_id: Sha256
    spec: TaskSpec
    correlation_ids: list[Identifier]
    status: TaskStatus
    attempts: list[TaskAttempt]
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    economic_evaluation: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    evidence_authentication: Literal["NOT_AUTHENTICATED"] = "NOT_AUTHENTICATED"

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.task_id != task_request_id(self.spec) or self.configuration_id != task_configuration_id(self.spec):
            raise ValueError("task/configuration identities must match the canonical specification")
        if (self.spec.correlation_id not in self.correlation_ids
                or len(set(self.correlation_ids)) != len(self.correlation_ids)):
            raise ValueError("correlation aliases must include the original and be unique")
        if self.status != (self.attempts[-1].status if self.attempts else "incomplete"):
            raise ValueError("task status must match the latest attempt")
        ids = [attempt.local_attempt_id for attempt in self.attempts]
        if len(ids) != len(set(ids)):
            raise ValueError("attempt identities must be unique")
        for index, attempt in enumerate(self.attempts, 1):
            if attempt.task_id != self.task_id or attempt.attempt_number != index:
                raise ValueError("attempt identity or sequence differs from the task")
            if index < len(self.attempts) and attempt.status not in _TERMINAL:
                raise ValueError("earlier attempts must be resolved before another attempt")
            if attempt.sync_confirmation is not None:
                for key in (
                    "operation", "source_commit", "config_sha256", "request_sha256", "project_id", "response_class"
                ):
                    if getattr(self.spec, key) != getattr(attempt.sync_confirmation, key):
                        raise ValueError("synchronous confirmation must bind its task specification")
        return self


class TaskUsage(StrictContract):
    configuration_count: NonNegativeInt
    task_count: NonNegativeInt
    attempt_count: NonNegativeInt
    confirmed_execution_count: NonNegativeInt
    synchronous_result_count: NonNegativeInt = 0
    unknown_attempt_count: NonNegativeInt
    active_attempt_count: NonNegativeInt
    operation_attempts: dict[Operation, NonNegativeInt]
    known_cost_microusd: NonNegativeInt
    reserved_unknown_cost_microusd: NonNegativeInt
    unknown_cost_attempt_count: NonNegativeInt
    budget_cost_microusd: NonNegativeInt

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if sum(self.operation_attempts.values()) != self.attempt_count:
            raise ValueError("operation attempts must sum to all attempts")
        if any(value > self.attempt_count for value in (
            self.confirmed_execution_count, self.unknown_attempt_count,
            self.active_attempt_count, self.unknown_cost_attempt_count, self.synchronous_result_count,
        )):
            raise ValueError("attempt subcounts cannot exceed all attempts")
        if self.known_cost_microusd + self.reserved_unknown_cost_microusd != self.budget_cost_microusd:
            raise ValueError("cost budget usage must include unknown-cost reservations")
        return self


S2_TASK_CONTRACTS: dict[str, type[StrictContract]] = {
    model.__name__: model for model in (
        TaskBudget, TaskSpec, TaskResponse, TaskReconciliation, SynchronousCompletionRecord,
        SynchronousResultMetadata, RawMetadataRef,
        TaskAttempt, TaskRecord, TaskUsage,
    )
}


def canonical_task_bytes(value: StrictContract) -> bytes:
    """Canonical bytes for typed control contracts, never arbitrary dataset data."""
    if not isinstance(value, StrictContract):
        raise TaskLedgerError("canonical task bytes require a typed control contract")
    checked = type(value).model_validate(value)
    return json.dumps(
        checked.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    ).encode("utf-8")


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")).hexdigest()


def task_configuration_id(spec: TaskSpec) -> str:
    checked = TaskSpec.model_validate(spec)
    return _digest({key: checked.model_dump(mode="json")[key] for key in (
        "source_commit", "config_sha256", "response_class", "purpose", "stage"
    )})


def task_request_id(spec: TaskSpec) -> str:
    checked = TaskSpec.model_validate(spec)
    payload = checked.model_dump(mode="json")
    del payload["correlation_id"]
    return _digest(payload)


def _json_model(model: type[StrictContract], payload: str | bytes) -> Any:
    return model.model_validate_json(payload)


def _read_control_model(payload: bytes) -> TaskResponse | TaskReconciliation | SynchronousResultMetadata:
    for model in (TaskResponse, TaskReconciliation, SynchronousResultMetadata):
        try:
            return _json_model(model, payload)
        except (ValueError, ValidationError, TypeError):
            continue
    raise TaskLedgerError("stored metadata is not an allowlisted control payload")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TaskLedgerError("metadata JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_nonfinite(_: str) -> Any:
    raise TaskLedgerError("metadata JSON contains a non-finite number")


def _metadata_bytes(
    response: TaskResponse | TaskReconciliation | SynchronousResultMetadata, raw_metadata: bytes | None
) -> tuple[bytes, str]:
    """Reject unsafe bytes before retaining either their contents or their hash."""
    if raw_metadata is None:
        return canonical_task_bytes(response), "LOCAL_NORMALIZED_METADATA"
    try:
        if type(raw_metadata) is not bytes or not 0 < len(raw_metadata) <= 65_536:
            raise ValueError
        decoded = json.loads(
            raw_metadata.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
        if not isinstance(decoded, dict):
            raise ValueError
        # JSON validation is required for aware datetime strings in reconciliation.
        parsed = type(response).model_validate_json(json.dumps(decoded, allow_nan=False))
        if parsed != response:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, ValidationError, OverflowError, RecursionError):
        raise TaskLedgerError("metadata bytes are not an exact allowlisted response") from None
    return raw_metadata, "CALLER_METADATA_BYTES"


def _safe_path(path: Path, *, directory: bool) -> Path:
    absolute = Path(os.path.abspath(path))
    for component in reversed((absolute, *absolute.parents)):
        if os.path.lexists(component):
            metadata = component.lstat()
            if (stat.S_ISLNK(metadata.st_mode)
                    or getattr(metadata, "st_reparse_tag", 0) in _REDIRECT_TAGS):
                raise TaskLedgerError("ledger paths must not contain links or junctions")
            if component != absolute and not stat.S_ISDIR(metadata.st_mode):
                raise TaskLedgerError("ledger path parent is not a directory")
    if directory:
        absolute.mkdir(parents=True, exist_ok=True)
        if not absolute.is_dir():
            raise TaskLedgerError("metadata root must be a directory")
    else:
        absolute.parent.mkdir(parents=True, exist_ok=True)
        if absolute.exists() and not absolute.is_file():
            raise TaskLedgerError("ledger database must be a regular file")
    return absolute.resolve(strict=directory)


class TaskTransport(Protocol):
    def __call__(self, spec: TaskSpec, attempt: TaskAttempt) -> TaskResponse:
        """Inject a separately authorized transport; no retry is implicit."""
        ...


class TaskLedger:
    """SQLite transactions serialize duplicates, budgets and immutable metadata.

    A committed UNKNOWN record precedes every transport invocation. Crashes can
    leave unreferenced *safe metadata* files; referenced files are immutable and
    checked on reopen. Attempt counts measure dispatch intents (including lost
    responses); confirmed execution counts separately require observed IDs.
    """

    def __init__(self, db_path: Path, raw_dir: Path, budget: TaskBudget):
        self._budget_bytes = canonical_task_bytes(TaskBudget.model_validate(budget))
        self.db_path = _safe_path(Path(db_path), directory=False)
        self.raw_dir = _safe_path(Path(raw_dir), directory=True)
        if self.db_path.is_relative_to(self.raw_dir):
            raise TaskLedgerError("database must not be inside immutable metadata root")
        self._validate_raw_tree()
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS configurations (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, configuration_id TEXT NOT NULL, spec_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS correlations (
                    correlation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    attempt_number INTEGER NOT NULL, operation TEXT NOT NULL,
                    attempt_json TEXT NOT NULL, UNIQUE(task_id, attempt_number)
                );
                CREATE TABLE IF NOT EXISTS events (
                    attempt_id TEXT NOT NULL REFERENCES attempts(id), event_id TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL, payload_kind TEXT NOT NULL,
                    raw_json TEXT NOT NULL, PRIMARY KEY(attempt_id, event_id)
                );
            """)
        with self._transaction() as connection:
            encoded = canonical_task_bytes(self.budget).decode("utf-8")
            stored = connection.execute("SELECT value FROM ledger_meta WHERE key='budget'").fetchone()
            if stored is None:
                connection.execute("INSERT INTO ledger_meta VALUES ('budget', ?)", (encoded,))
                connection.execute("INSERT INTO ledger_meta VALUES ('schema_version', '1.0')")
                connection.execute(
                    "INSERT INTO ledger_meta VALUES ('raw_dir', ?)", (str(self.raw_dir),)
                )
            elif stored[0] != encoded:
                raise TaskLedgerError("reopening a ledger cannot replace its persisted budget")
            raw_root = connection.execute(
                "SELECT value FROM ledger_meta WHERE key='raw_dir'"
            ).fetchone()
            version = connection.execute(
                "SELECT value FROM ledger_meta WHERE key='schema_version'"
            ).fetchone()
            if raw_root != (str(self.raw_dir),) or version != ("1.0",):
                raise TaskLedgerError("ledger metadata root or schema version does not match")
        self.verify_integrity()

    @property
    def budget(self) -> TaskBudget:
        """A detached copy; mutation cannot expand the durable budget snapshot."""
        return TaskBudget.model_validate_json(self._budget_bytes)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        _safe_path(self.db_path, directory=False)
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def _validate_raw_tree(self) -> None:
        _safe_path(self.raw_dir, directory=True)
        for child in self.raw_dir.iterdir():
            metadata = child.lstat()
            if (not stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or getattr(metadata, "st_reparse_tag", 0) in _REDIRECT_TAGS):
                raise TaskLedgerError("immutable metadata root must contain only regular files")
            match = re.fullmatch(r"metadata_[0-9a-f]{32}_([0-9a-f]{64})\.json", child.name)
            if match is None or not 1 <= metadata.st_size <= 65_536:
                raise TaskLedgerError("metadata root contains an unexpected file name or size")
            payload = child.read_bytes()
            if hashlib.sha256(payload).hexdigest() != match.group(1):
                raise TaskLedgerError("immutable metadata bytes do not match their filename hash")
            try:
                checked = _read_control_model(payload)
                _metadata_bytes(checked, payload)
            except (ValueError, ValidationError, TypeError):
                raise TaskLedgerError("metadata root contains non-allowlisted control metadata") from None

    @staticmethod
    def _attempts(connection: sqlite3.Connection, task_id: str | None = None) -> list[TaskAttempt]:
        if task_id is None:
            rows = connection.execute("SELECT attempt_json FROM attempts ORDER BY rowid")
        else:
            rows = connection.execute(
                "SELECT attempt_json FROM attempts WHERE task_id=? ORDER BY attempt_number", (task_id,)
            )
        return [_json_model(TaskAttempt, row[0]) for row in rows]

    def _record(self, connection: sqlite3.Connection, task_id: str) -> TaskRecord:
        row = connection.execute(
            "SELECT configuration_id,spec_json FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise TaskLedgerError("unknown task or correlation identifier")
        attempts = self._attempts(connection, task_id)
        return TaskRecord(
            task_id=task_id, configuration_id=row[0], spec=_json_model(TaskSpec, row[1]),
            correlation_ids=[item[0] for item in connection.execute(
                "SELECT correlation_id FROM correlations WHERE task_id=? ORDER BY correlation_id", (task_id,)
            )],
            status=attempts[-1].status if attempts else "incomplete", attempts=attempts,
        )

    @staticmethod
    def _find_task(connection: sqlite3.Connection, correlation_id: str) -> str:
        row = connection.execute(
            "SELECT task_id FROM correlations WHERE correlation_id=?", (correlation_id,)
        ).fetchone()
        if row is None:
            raise TaskLedgerError("unknown correlation identifier")
        return row[0]

    def register(self, spec: TaskSpec) -> TaskRecord:
        spec = TaskSpec.model_validate(spec)
        if spec.response_class != self.budget.response_class:
            raise TaskLedgerError("response class differs from the persisted budget")
        task_id = task_request_id(spec)
        configuration_id = task_configuration_id(spec)
        with self._transaction() as connection:
            alias = connection.execute(
                "SELECT task_id FROM correlations WHERE correlation_id=?", (spec.correlation_id,)
            ).fetchone()
            if alias is not None and alias[0] != task_id:
                raise TaskLedgerError("correlation identifier already binds a different request")
            if connection.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
                if connection.execute(
                    "SELECT 1 FROM configurations WHERE id=?", (configuration_id,)
                ).fetchone() is None:
                    count = connection.execute("SELECT COUNT(*) FROM configurations").fetchone()[0]
                    if count >= self.budget.max_configurations:
                        raise TaskLedgerError("configuration budget exhausted")
                    connection.execute("INSERT INTO configurations VALUES (?)", (configuration_id,))
                connection.execute(
                    "INSERT INTO tasks VALUES (?, ?, ?)",
                    (task_id, configuration_id, canonical_task_bytes(spec).decode("utf-8")),
                )
            connection.execute(
                "INSERT OR IGNORE INTO correlations VALUES (?, ?)", (spec.correlation_id, task_id)
            )
            return self._record(connection, task_id)

    def get(self, correlation_id: str) -> TaskRecord:
        with self._connection() as connection:
            return self._record(connection, self._find_task(connection, correlation_id))

    def _usage(self, connection: sqlite3.Connection) -> TaskUsage:
        attempts = self._attempts(connection)
        known = sum(item.actual_cost_microusd or 0 for item in attempts)
        unknown = [item for item in attempts if item.actual_cost_microusd is None]
        reserved = sum(item.reserved_cost_microusd for item in unknown)
        operations: dict[str, int] = {}
        for row in connection.execute("SELECT operation,COUNT(*) FROM attempts GROUP BY operation"):
            operations[row[0]] = row[1]
        return TaskUsage(
            configuration_count=connection.execute("SELECT COUNT(*) FROM configurations").fetchone()[0],
            task_count=connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            attempt_count=len(attempts),
            confirmed_execution_count=sum(self._has_execution_id(item) for item in attempts),
            synchronous_result_count=sum(item.sync_confirmation is not None for item in attempts),
            unknown_attempt_count=sum(item.status == "UNKNOWN" for item in attempts),
            active_attempt_count=sum(item.status not in _TERMINAL for item in attempts),
            operation_attempts=operations, known_cost_microusd=known,
            reserved_unknown_cost_microusd=reserved, unknown_cost_attempt_count=len(unknown),
            budget_cost_microusd=known + reserved,
        )

    def usage(self) -> TaskUsage:
        with self._connection() as connection:
            return self._usage(connection)

    def begin_attempt(
        self, correlation_id: str, *, retry: bool = False, cost_limit_microusd: int = 0
    ) -> TaskAttempt:
        if type(retry) is not bool:
            raise TaskLedgerError("retry flag must be a boolean")
        if type(cost_limit_microusd) is not int or not 0 <= cost_limit_microusd <= 10**15:
            raise TaskLedgerError("cost reservation must be a nonnegative integer micro-USD amount")
        with self._transaction() as connection:
            task_id = self._find_task(connection, correlation_id)
            record = self._record(connection, task_id)
            if record.attempts:
                latest = record.attempts[-1]
                if latest.status not in _TERMINAL:
                    raise TaskLedgerError("unresolved attempt must be reconciled before retry")
                if not retry:
                    raise TaskLedgerError("another attempt requires explicit retry=true")
            elif retry:
                raise TaskLedgerError("initial attempt is not a retry")
            usage = self._usage(connection)
            if usage.attempt_count >= self.budget.max_attempts:
                raise TaskLedgerError("attempt budget exhausted")
            if usage.operation_attempts.get(record.spec.operation, 0) >= self.budget.operation_limits.get(
                record.spec.operation, 0
            ):
                raise TaskLedgerError("operation attempt budget exhausted")
            if usage.active_attempt_count >= self.budget.max_concurrent_attempts:
                raise TaskLedgerError("concurrent attempt budget exhausted")
            if usage.budget_cost_microusd + cost_limit_microusd > self.budget.max_cost_microusd:
                raise TaskLedgerError("cost reservation budget exhausted")
            attempt = TaskAttempt(
                local_attempt_id="local_" + uuid.uuid4().hex, task_id=task_id,
                attempt_number=len(record.attempts) + 1, created_at=datetime.now(timezone.utc),
                status="UNKNOWN", reserved_cost_microusd=cost_limit_microusd,
            )
            connection.execute(
                "INSERT INTO attempts VALUES (?, ?, ?, ?, ?)",
                (attempt.local_attempt_id, task_id, attempt.attempt_number, record.spec.operation,
                 canonical_task_bytes(attempt).decode("utf-8")),
            )
            return attempt

    @staticmethod
    def _has_execution_id(item: TaskAttempt | TaskResponse) -> bool:
        return bool(item.platform_task_id or (
            item.invocation_id is not None and item.cell_index is not None
        ))

    @staticmethod
    def _check_binding(spec: TaskSpec, response: TaskResponse | TaskReconciliation) -> None:
        for key in (
            "operation", "source_commit", "config_sha256", "request_sha256", "project_id", "response_class"
        ):
            if getattr(spec, key) != getattr(response, key):
                raise TaskLedgerError("response identity does not match the registered request")

    def _write_metadata(
        self, payload: bytes, capture_kind: str, payload_kind: str
    ) -> RawMetadataRef:
        self._validate_raw_tree()
        digest = hashlib.sha256(payload).hexdigest()
        name = "metadata_" + uuid.uuid4().hex + "_" + digest + ".json"
        # Publish only a fully flushed file. Hard-link creation is atomic and
        # refuses an existing destination on both Windows and POSIX; a rename
        # could overwrite on POSIX. A crash before publication leaves a safe
        # staging file outside the immutable metadata directory.
        staging = self.raw_dir.parent / (".metadata_pending_" + uuid.uuid4().hex + ".tmp")
        created = False
        try:
            with staging.open("xb") as stream:
                created = True
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(staging, self.raw_dir / name)
        finally:
            if created:
                staging.unlink()
        return RawMetadataRef(
            relative_path=name, sha256=digest, size_bytes=len(payload),
            capture_kind=capture_kind, payload_kind=payload_kind,
        )

    @staticmethod
    def _load_attempt(
        connection: sqlite3.Connection, attempt_id: str
    ) -> tuple[TaskAttempt, TaskSpec]:
        row = connection.execute(
            "SELECT a.attempt_json,t.spec_json FROM attempts a JOIN tasks t ON a.task_id=t.id WHERE a.id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise TaskLedgerError("unknown local attempt identifier")
        return _json_model(TaskAttempt, row[0]), _json_model(TaskSpec, row[1])

    @staticmethod
    def _duplicate(
        connection: sqlite3.Connection, attempt_id: str, event_id: str, digest: str, kind: str
    ) -> bool:
        if type(event_id) is not str or not 1 <= len(event_id) <= 128 or not all(
            character.isascii() and (character.isalnum() or character in "_.:-") for character in event_id
        ):
            raise TaskLedgerError("event identifier must be a safe control identifier")
        row = connection.execute(
            "SELECT payload_sha256,payload_kind FROM events WHERE attempt_id=? AND event_id=?",
            (attempt_id, event_id),
        ).fetchone()
        if row is not None and row != (digest, kind):
            raise TaskLedgerError("event identifier already binds different metadata")
        return row is not None

    def _save_event(
        self, connection: sqlite3.Connection, attempt: TaskAttempt, event_id: str,
        payload: bytes, capture_kind: str, payload_kind: str,
    ) -> None:
        reference = self._write_metadata(payload, capture_kind, payload_kind)
        updated = attempt.model_copy(update={"raw_metadata": [*attempt.raw_metadata, reference]})
        checked = TaskAttempt.model_validate(updated)
        connection.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
            (attempt.local_attempt_id, event_id, reference.sha256, payload_kind,
             canonical_task_bytes(reference).decode("utf-8")),
        )
        connection.execute(
            "UPDATE attempts SET attempt_json=? WHERE id=?",
            (canonical_task_bytes(checked).decode("utf-8"), checked.local_attempt_id),
        )

    def record_event(
        self, attempt_id: str, event_id: str, response: TaskResponse, *, raw_metadata: bytes | None = None
    ) -> TaskRecord:
        """Fold ordinary metadata without treating an unidentified error as resolution."""
        try:
            response = TaskResponse.model_validate(response)
        except (ValidationError, ValueError, TypeError):
            raise TaskLedgerError("response is not allowlisted control metadata") from None
        payload, capture_kind = _metadata_bytes(response, raw_metadata)
        with self._transaction() as connection:
            previous, spec = self._load_attempt(connection, attempt_id)
            self._check_binding(spec, response)
            if self._duplicate(connection, attempt_id, event_id, hashlib.sha256(payload).hexdigest(), "TaskResponse"):
                return self._record(connection, previous.task_id)
            updates = response.model_dump(exclude={
                "schema_version", "purpose", "operation", "source_commit", "config_sha256",
                "request_sha256", "project_id", "response_class",
            })
            for key in ("platform_task_id", "invocation_id", "cell_index"):
                existing = getattr(previous, key)
                incoming = getattr(response, key)
                if existing is not None and incoming is not None and existing != incoming:
                    raise TaskLedgerError("observed execution identity cannot change within an attempt")
                if incoming is None:
                    updates[key] = existing
            if previous.actual_cost_microusd is not None:
                if response.actual_cost_microusd not in {None, previous.actual_cost_microusd}:
                    raise TaskLedgerError("confirmed cost cannot change within an attempt")
                updates["actual_cost_microusd"] = previous.actual_cost_microusd
            # An established verdict survives later error/incomplete metadata.
            # An unconfirmed no-ID claim remains UNKNOWN and is not execution proof.
            if previous.check_passed is not None and previous.status != "UNKNOWN":
                if response.check_passed is not None and response.check_passed != previous.check_passed:
                    raise TaskLedgerError("known check verdict cannot change within an attempt")
                updates["check_passed"] = previous.check_passed
            # Inspect the merged typed IDs before folding status. The complete
            # effective attempt is validated below, before any event is written.
            candidate = previous.model_copy(update=updates)
            status = response.status
            if (spec.operation in _ASYNC_OPERATIONS and not self._has_execution_id(candidate)
                    and previous.sync_confirmation is None
                    and status not in {"UNKNOWN", "incomplete"}):
                status = "UNKNOWN"
            if (previous.status == "UNKNOWN" and status == "incomplete"
                    and not self._has_execution_id(candidate)):
                raise TaskLedgerError("unknown submission requires an explicit not-submitted reconciliation")
            if previous.status in _TERMINAL and status != previous.status:
                raise TaskLedgerError("terminal task state cannot change")
            if previous.status in _ORDER and status in _ORDER and _ORDER[status] < _ORDER[previous.status]:
                raise TaskLedgerError("task status cannot regress")
            # A lost poll response cannot erase previously observed progress.
            if status == "UNKNOWN" and previous.status != "UNKNOWN":
                status = previous.status
                for key in ("success", "check_passed"):
                    updates[key] = getattr(previous, key)
            updated = TaskAttempt.model_validate(previous.model_copy(update={
                **updates, "status": status, "reported_status": response.status,
            }))
            self._save_event(connection, updated, event_id, payload, capture_kind, "TaskResponse")
            return self._record(connection, previous.task_id)

    def reconcile_not_submitted(
        self, attempt_id: str, event_id: str, proof: TaskReconciliation,
        *, raw_metadata: bytes | None = None,
    ) -> TaskRecord:
        try:
            proof = TaskReconciliation.model_validate(proof)
        except (ValidationError, ValueError, TypeError):
            raise TaskLedgerError("reconciliation is not allowlisted metadata") from None
        payload, capture_kind = _metadata_bytes(proof, raw_metadata)
        with self._transaction() as connection:
            previous, spec = self._load_attempt(connection, attempt_id)
            self._check_binding(spec, proof)
            if self._duplicate(
                connection, attempt_id, event_id, hashlib.sha256(payload).hexdigest(), "TaskReconciliation"
            ):
                return self._record(connection, previous.task_id)
            if (previous.status != "UNKNOWN" or self._has_execution_id(previous)
                    or proof.checked_at < previous.created_at
                    or proof.checked_at > datetime.now(timezone.utc)):
                raise TaskLedgerError("not-submitted reconciliation contradicts the observed attempt")
            updated = TaskAttempt.model_validate(previous.model_copy(update={
                "status": "incomplete", "reconciliation": proof,
            }))
            self._save_event(connection, updated, event_id, payload, capture_kind, "TaskReconciliation")
            return self._record(connection, previous.task_id)

    def record_synchronous_result(
        self, attempt_id: str, event_id: str, response: TaskResponse,
        confirmation: SynchronousCompletionRecord,
    ) -> TaskRecord:
        """Close a reserved attempt after a separately observed synchronous call.

        Only trusted application code with an actual fresh tool return should
        invoke this method. A receipt/imported response alone must use ordinary
        record_event and remains UNKNOWN when no execution ID was returned.
        This assertion grants no access, authenticates no platform evidence and
        does not infer a zero bill. It does not submit or retry anything.
        """
        try:
            response = TaskResponse.model_validate(response)
            confirmation = SynchronousCompletionRecord.model_validate(confirmation)
            envelope = SynchronousResultMetadata(response=response, confirmation=confirmation)
        except (ValueError, ValidationError, TypeError):
            raise TaskLedgerError("synchronous result is not exact allowlisted control metadata") from None
        payload, capture_kind = _metadata_bytes(envelope, None)
        with self._transaction() as connection:
            previous, spec = self._load_attempt(connection, attempt_id)
            self._check_binding(spec, response)
            if self._duplicate(connection, attempt_id, event_id, hashlib.sha256(payload).hexdigest(),
                               "SynchronousResultMetadata"):
                return self._record(connection, previous.task_id)
            if (previous.status != "UNKNOWN" or previous.platform_task_id is not None
                    or previous.invocation_id is not None or previous.cell_index is not None
                    or confirmation.local_attempt_id != attempt_id
                    or not previous.created_at <= confirmation.observed_at <= datetime.now(timezone.utc)):
                raise TaskLedgerError("synchronous confirmation does not match the current unknown reservation")
            updates = response.model_dump(exclude={
                "schema_version", "purpose", "operation", "source_commit", "config_sha256",
                "request_sha256", "project_id", "response_class",
            })
            if previous.actual_cost_microusd is not None:
                if response.actual_cost_microusd not in {None, previous.actual_cost_microusd}:
                    raise TaskLedgerError("confirmed cost cannot change within an attempt")
                updates["actual_cost_microusd"] = previous.actual_cost_microusd
            updated = TaskAttempt.model_validate(previous.model_copy(update={
                **updates, "reported_status": response.status, "sync_confirmation": confirmation,
            }))
            self._save_event(connection, updated, event_id, payload, capture_kind, "SynchronousResultMetadata")
            return self._record(connection, previous.task_id)

    def submit(
        self, spec: TaskSpec, transport: TaskTransport, *, retry: bool = False,
        cost_limit_microusd: int = 0,
    ) -> TaskRecord:
        """At most one callback per reserved attempt; repeated calls reuse evidence.

        The budget is a local bound, not permission for a Cloud operation. The
        injected transport remains responsible for runtime authorization and
        source/request boundary checks before it invokes the provider.
        """
        spec = TaskSpec.model_validate(spec)
        if not callable(transport):
            raise TaskLedgerError("transport must be an injected callable")
        record = self.register(spec)
        if record.attempts and not retry:
            return record
        try:
            attempt = self.begin_attempt(
                spec.correlation_id, retry=retry, cost_limit_microusd=cost_limit_microusd
            )
        except TaskLedgerError:
            # Concurrent identical initial submits share one persisted intent.
            refreshed = self.get(spec.correlation_id)
            if not retry and refreshed.attempts:
                return refreshed
            raise
        try:
            response = TaskResponse.model_validate(transport(spec, attempt))
            self._check_binding(spec, response)
        except Exception as error:
            reported_category = getattr(error, "error_category", None)
            allowed_categories = {
                "INVALID_REQUEST", "ACCESS_DENIED", "CAPABILITY_UNAVAILABLE", "PROVIDER_ERROR",
                "TIMEOUT", "MALFORMED_RESPONSE",
            }
            category = (reported_category if type(reported_category) is str
                        and reported_category in allowed_categories else
                        "TIMEOUT" if isinstance(error, TimeoutError) else
                        "MALFORMED_RESPONSE" if isinstance(error, (ValidationError, TaskLedgerError)) else
                        "PROVIDER_ERROR")
            response = TaskResponse(
                operation=spec.operation, source_commit=spec.source_commit,
                config_sha256=spec.config_sha256, request_sha256=spec.request_sha256,
                project_id=spec.project_id, response_class=spec.response_class,
                status="UNKNOWN", error_category=category,
            )
        return self.record_event(attempt.local_attempt_id, "dispatch_response", response)

    def verify_integrity(self) -> None:
        """Verify every referenced immutable byte sequence, without executing jobs."""
        self._validate_raw_tree()
        with self._connection() as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise TaskLedgerError("SQLite integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise TaskLedgerError("SQLite foreign-key check failed")
            for row in connection.execute("SELECT raw_json,payload_sha256,payload_kind FROM events"):
                reference = _json_model(RawMetadataRef, row[0])
                path = self.raw_dir / reference.relative_path
                if not path.is_file() or path.is_symlink():
                    raise TaskLedgerError("referenced metadata is missing or redirected")
                payload = path.read_bytes()
                if (len(payload) != reference.size_bytes
                        or hashlib.sha256(payload).hexdigest() != reference.sha256
                        or reference.sha256 != row[1] or reference.payload_kind != row[2]):
                    raise TaskLedgerError("immutable metadata bytes do not match their recorded hash")
                try:
                    checked = _read_control_model(payload)
                    if type(checked).__name__ != reference.payload_kind:
                        raise ValueError("stored payload kind differs from its reference")
                    _metadata_bytes(checked, payload)
                except (ValueError, ValidationError, TypeError):
                    raise TaskLedgerError("stored metadata is not allowlisted") from None
