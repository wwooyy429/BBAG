"""SYNTHETIC control-only regressions for S2-001/002; no platform acceptance."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from threading import Barrier

import pytest

from bbag.s2_tasks import (
    SynchronousCompletionRecord, TaskBudget, TaskLedger, TaskLedgerError,
    TaskReconciliation, TaskResponse, TaskSpec, canonical_task_bytes,
)


ASYNC = ("compile", "research_check", "data_query")
ERRORS = ("INVALID_REQUEST", "ACCESS_DENIED", "CAPABILITY_UNAVAILABLE", "PROVIDER_ERROR", "TIMEOUT", "MALFORMED_RESPONSE")


def setup(tmp_path, operation="compile", response_class="CONTROL_ONLY", **changes):
    values = dict(response_class=response_class, max_configurations=2, max_attempts=4,
        max_concurrent_attempts=1, max_cost_microusd=100,
        operation_limits={name: 4 for name in (*ASYNC, "project_read")})
    values.update(changes)
    budget = TaskBudget(**values)
    task = TaskSpec(correlation_id="SYNTHETIC_REQUEST", operation=operation, source_commit="a" * 40,
        config_sha256="c" * 64, request_sha256="d" * 64, project_id=123, response_class=response_class)
    ledger = TaskLedger(tmp_path / "tasks.sqlite", tmp_path / "metadata", budget)
    return ledger, task, budget


def message(task, **changes):
    values = {key: getattr(task, key) for key in ("operation", "source_commit", "config_sha256", "request_sha256", "project_id", "response_class")}
    values.update(status="completed", success=True)
    values.update(changes)
    return TaskResponse(**values)


def reopened(ledger, budget):
    return TaskLedger(ledger.db_path, ledger.raw_dir, budget)


def proof(attempt, response):
    values = {key: getattr(response, key) for key in ("operation", "source_commit", "config_sha256", "request_sha256", "project_id", "response_class")}
    return SynchronousCompletionRecord(local_attempt_id=attempt.local_attempt_id,
        observed_at=datetime.now(timezone.utc), response_metadata_sha256=hashlib.sha256(canonical_task_bytes(response)).hexdigest(), **values)


def timeout(ledger, task, calls):
    def callback(selected, attempt):
        assert ledger.get(selected.correlation_id).status == "UNKNOWN"
        calls.append(attempt.local_attempt_id)
        raise TimeoutError("SYNTHETIC_TIMEOUT_ONLY")
    return ledger.submit(task, callback, cost_limit_microusd=20)


def snapshot(ledger, task):
    """Check persisted bytes as well as folded state, budget and immutable files."""
    record = ledger.get(task.correlation_id).model_dump(mode="json")
    usage = ledger.usage().model_dump(mode="json")
    with closing(sqlite3.connect(ledger.db_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        events = connection.execute("SELECT attempt_id,event_id,payload_sha256,payload_kind,raw_json FROM events ORDER BY attempt_id,event_id").fetchall()
    return dict(record=record, usage=usage, events=events,
        db_sha256=hashlib.sha256(ledger.db_path.read_bytes()).hexdigest(),
        metadata={path.name: path.read_bytes() for path in ledger.raw_dir.iterdir()},
        pending={path.name for path in ledger.raw_dir.parent.glob(".metadata_pending_*")})


@pytest.mark.parametrize("operation", ASYNC)
@pytest.mark.parametrize("category", ERRORS)
@pytest.mark.parametrize("response_class", ["CONTROL_ONLY", "SYNTHETIC_TEST"])
def test_unidentified_error_cannot_resolve_unknown_retry_or_concurrency(tmp_path, operation, category, response_class):
    ledger, task, budget = setup(tmp_path, operation, response_class)
    calls = []
    first = timeout(ledger, task, calls)
    result = ledger.record_event(first.attempts[-1].local_attempt_id, "ordinary-error-claim",
        message(task, status="error", success=False, error_category=category))
    assert result.status == "UNKNOWN"
    assert result.attempts[-1].reported_status == "error"
    assert result.attempts[-1].error_category == category
    assert result.attempts[-1].actual_cost_microusd is None
    assert result.attempts[-1].sync_confirmation is None
    usage = ledger.usage()
    assert usage.attempt_count == usage.active_attempt_count == usage.unknown_attempt_count == 1
    assert usage.unknown_cost_attempt_count == 1
    assert usage.reserved_unknown_cost_microusd == usage.budget_cost_microusd == 20
    def should_not_run(selected, attempt):
        calls.append(attempt.local_attempt_id)
        return message(selected, platform_task_id="SYNTHETIC_PROVIDER_ID")
    with pytest.raises(TaskLedgerError, match="unresolved"):
        ledger.submit(task, should_not_run, retry=True)
    other = task.model_copy(update={"correlation_id": "SYNTHETIC_OTHER", "request_sha256": "e" * 64})
    with pytest.raises(TaskLedgerError, match="concurrent"):
        ledger.submit(other, should_not_run)
    assert len(calls) == 1
    assert ledger.usage().attempt_count == 1
    assert reopened(ledger, budget).get(task.correlation_id).status == "UNKNOWN"


@pytest.mark.parametrize("operation", ASYNC)
@pytest.mark.parametrize("identity", ["new-provider-id", "new-invocation-cell", "known-id-omitted"])
def test_identified_error_can_finish_and_explicit_retry_still_spends_budget(tmp_path, operation, identity):
    ledger, task, _ = setup(tmp_path, operation, max_attempts=2, operation_limits={operation: 2})
    if identity == "known-id-omitted":
        first = ledger.submit(task, lambda *_: message(task, status="running", success=None, platform_task_id="SYNTHETIC_JOB"))
        ids = {}
    else:
        first = timeout(ledger, task, [])
        ids = {"platform_task_id": "SYNTHETIC_JOB"} if identity == "new-provider-id" else {"invocation_id": "SYNTHETIC_INVOCATION", "cell_index": 0}
    result = ledger.record_event(first.attempts[-1].local_attempt_id, "observed-identified-error",
        message(task, status="error", success=False, error_category="TIMEOUT", **ids))
    assert result.status == "error" and result.attempts[-1].actual_cost_microusd is None
    assert ledger.usage().active_attempt_count == 0
    if identity == "known-id-omitted":
        assert result.attempts[-1].platform_task_id == "SYNTHETIC_JOB"
    with pytest.raises(TaskLedgerError, match="explicit retry"):
        ledger.begin_attempt(task.correlation_id)
    retried = ledger.submit(task, lambda *_: message(task, platform_task_id="SYNTHETIC_RETRY"), retry=True)
    assert retried.status == "completed" and ledger.usage().attempt_count == 2
    with pytest.raises(TaskLedgerError, match="attempt budget"):
        ledger.begin_attempt(task.correlation_id, retry=True)


@pytest.mark.parametrize("operation", ASYNC)
def test_explicit_not_submitted_reconciliation_remains_the_no_id_retry_path(tmp_path, operation):
    ledger, task, _ = setup(tmp_path, operation)
    first = timeout(ledger, task, [])
    attempt = first.attempts[-1]
    ledger.record_event(attempt.local_attempt_id, "ordinary-timeout", message(task, status="error", success=False, error_category="TIMEOUT"))
    values = {key: getattr(task, key) for key in ("operation", "source_commit", "config_sha256", "request_sha256", "project_id", "response_class")}
    reconciled = ledger.reconcile_not_submitted(attempt.local_attempt_id, "explicit-lookup-empty",
        TaskReconciliation(lookup_method="PROVIDER_REQUEST_LOOKUP", checked_at=datetime.now(timezone.utc), **values))
    assert reconciled.status == "incomplete" and reconciled.attempts[-1].actual_cost_microusd is None
    assert ledger.usage().active_attempt_count == 0
    assert ledger.submit(task, lambda *_: message(task, platform_task_id="SYNTHETIC_RETRY"), retry=True).status == "completed"


@pytest.mark.parametrize("operation", ["compile", "research_check"])
def test_fresh_sync_error_remains_a_valid_resolution_without_clearing_unknown_bill(tmp_path, operation):
    ledger, task, _ = setup(tmp_path, operation)
    attempt = timeout(ledger, task, []).attempts[-1]
    response = message(task, status="error", success=False, error_category="TIMEOUT")
    result = ledger.record_synchronous_result(attempt.local_attempt_id, "fresh-observed-tool-error", response, proof(attempt, response))
    assert result.status == "error" and result.attempts[-1].actual_cost_microusd is None
    assert ledger.usage().synchronous_result_count == 1
    assert ledger.usage().unknown_cost_attempt_count == 1
    assert ledger.begin_attempt(task.correlation_id, retry=True).attempt_number == 2


def available_result(tmp_path, route, verdict):
    ledger, task, budget = setup(tmp_path, "research_check")
    if route == "sync-completed":
        ledger.register(task)
        attempt = ledger.begin_attempt(task.correlation_id, cost_limit_microusd=20)
        response = message(task, check_passed=verdict)
        result = ledger.record_synchronous_result(attempt.local_attempt_id, "fresh-observation", response, proof(attempt, response))
    else:
        status = "result_available" if route == "id-available" else "completed"
        result = ledger.submit(task, lambda *_: message(task, status=status,
            success=None if status == "result_available" else True, check_passed=verdict, platform_task_id="SYNTHETIC_JOB"), cost_limit_microusd=20)
    return ledger, task, budget, result.attempts[-1]


@pytest.mark.parametrize("route", ["id-available", "id-completed", "sync-completed"])
@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("caller_bytes", [False, True])
def test_known_result_conflict_is_rejected_before_any_event_file_or_budget_change(tmp_path, route, known, caller_bytes):
    ledger, task, _, attempt = available_result(tmp_path, route, known)
    before = snapshot(ledger, task)
    response = message(task, check_passed=not known, actual_cost_microusd=7)
    raw = json.dumps(response.model_dump(mode="json"), indent=2).encode("utf-8") if caller_bytes else None
    with pytest.raises(TaskLedgerError, match="verdict"):
        ledger.record_event(attempt.local_attempt_id, "new-id-cannot-revise-verdict", response, raw_metadata=raw)
    assert snapshot(ledger, task) == before


@pytest.mark.parametrize("route", ["id-available", "id-completed", "sync-completed"])
@pytest.mark.parametrize("known", [False, True])
def test_omission_preserves_known_verdict_ids_and_sync_proof_while_first_bill_is_allowed(tmp_path, route, known):
    ledger, task, budget, attempt = available_result(tmp_path, route, known)
    response = message(task, actual_cost_microusd=7)  # known verdict and IDs deliberately omitted
    result = ledger.record_event(attempt.local_attempt_id, "first-bill", response)
    current = result.attempts[-1]
    assert result.status == "completed" and current.check_passed is known
    assert current.platform_task_id == attempt.platform_task_id
    assert current.sync_confirmation == attempt.sync_confirmation
    assert current.actual_cost_microusd == 7
    usage = ledger.usage()
    assert usage.attempt_count == 1 and usage.known_cost_microusd == usage.budget_cost_microusd == 7
    assert usage.unknown_cost_attempt_count == usage.reserved_unknown_cost_microusd == 0
    files = {path.name for path in ledger.raw_dir.iterdir()}
    assert ledger.record_event(attempt.local_attempt_id, "first-bill", response) == result
    assert {path.name for path in ledger.raw_dir.iterdir()} == files
    same = ledger.record_event(attempt.local_attempt_id, "same-verdict-new-event-id", message(task, check_passed=known))
    assert same.attempts[-1].check_passed is known and same.attempts[-1].actual_cost_microusd == 7
    assert reopened(ledger, budget).get(task.correlation_id).attempts[-1].check_passed is known


@pytest.mark.parametrize("route", ["id-available", "id-completed", "sync-completed"])
@pytest.mark.parametrize("first_verdict", [False, True])
def test_unknown_verdict_may_be_filled_once_then_omission_or_same_value_is_safe(tmp_path, route, first_verdict):
    ledger, task, _, attempt = available_result(tmp_path, route, None)
    result = ledger.record_event(attempt.local_attempt_id, "first-known-verdict", message(task, check_passed=first_verdict))
    assert result.attempts[-1].check_passed is first_verdict
    omitted = ledger.record_event(attempt.local_attempt_id, "verdict-not-repeated", message(task))
    assert omitted.attempts[-1].check_passed is first_verdict
    before = snapshot(ledger, task)
    with pytest.raises(TaskLedgerError, match="verdict"):
        ledger.record_event(attempt.local_attempt_id, "later-conflict", message(task, check_passed=not first_verdict))
    assert snapshot(ledger, task) == before


def test_lost_poll_preserves_available_known_result_and_cannot_enable_verdict_washing(tmp_path):
    ledger, task, _, attempt = available_result(tmp_path, "id-available", False)
    polled = ledger.record_event(attempt.local_attempt_id, "lost-poll", message(task, status="UNKNOWN", success=None, error_category="TIMEOUT"))
    assert polled.status == "result_available" and polled.attempts[-1].check_passed is False
    assert polled.attempts[-1].reported_status == "UNKNOWN" and polled.attempts[-1].platform_task_id == "SYNTHETIC_JOB"
    before = snapshot(ledger, task)
    with pytest.raises(TaskLedgerError, match="verdict"):
        ledger.record_event(attempt.local_attempt_id, "changed-final-result", message(task, check_passed=True))
    assert snapshot(ledger, task) == before
    with pytest.raises(TaskLedgerError, match="current unknown"):
        response = message(task, check_passed=True)
        ledger.record_synchronous_result(attempt.local_attempt_id, "cannot-hide-identified-execution", response, proof(attempt, response))
    assert snapshot(ledger, task) == before


@pytest.mark.parametrize("operation", ASYNC)
def test_unconfirmed_no_id_result_claim_cannot_lend_error_import_execution_proof(tmp_path, operation):
    ledger, task, _ = setup(tmp_path, operation)
    claimed = ledger.submit(task, lambda *_: message(task, check_passed=False))
    assert claimed.status == "UNKNOWN" and claimed.attempts[-1].reported_status == "completed"
    result = ledger.record_event(claimed.attempts[-1].local_attempt_id, "still-unidentified-error",
        message(task, status="error", success=False, error_category="PROVIDER_ERROR"))
    assert result.status == "UNKNOWN" and result.attempts[-1].sync_confirmation is None
    with pytest.raises(TaskLedgerError, match="unresolved"):
        ledger.begin_attempt(task.correlation_id, retry=True)


@pytest.mark.parametrize("operation", ["compile", "research_check"])
def test_fresh_sync_can_first_confirm_an_unidentified_claim_then_verdict_is_immutable(tmp_path, operation):
    ledger, task, _ = setup(tmp_path, operation)
    claimed = ledger.submit(task, lambda *_: message(task, check_passed=False))
    attempt = claimed.attempts[-1]
    assert claimed.status == "UNKNOWN"
    actual = message(task, check_passed=True)
    finished = ledger.record_synchronous_result(attempt.local_attempt_id, "first-confirmed-tool-return", actual, proof(attempt, actual))
    assert finished.status == "completed" and finished.attempts[-1].check_passed is True
    before = snapshot(ledger, task)
    with pytest.raises(TaskLedgerError, match="verdict"):
        ledger.record_event(attempt.local_attempt_id, "cannot-revise-sync-verdict", message(task, check_passed=False))
    assert snapshot(ledger, task) == before


def test_concurrent_first_verdict_fill_serializes_one_value_and_rejects_conflict_atomically(tmp_path):
    ledger, task, budget, attempt = available_result(tmp_path, "id-completed", None)
    ready = Barrier(2)
    def fill(value):
        other = reopened(ledger, budget)
        ready.wait(timeout=10)
        try:
            result = other.record_event(attempt.local_attempt_id, "first-" + str(value), message(task, check_passed=value))
            return "committed", result.attempts[-1].check_passed
        except TaskLedgerError:
            return "conflict", value
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(fill, [False, True]))
    assert sorted(kind for kind, _ in results) == ["committed", "conflict"]
    winner = next(value for kind, value in results if kind == "committed")
    assert reopened(ledger, budget).get(task.correlation_id).attempts[-1].check_passed is winner
    assert len(list(ledger.raw_dir.iterdir())) == 2
    assert len(snapshot(ledger, task)["events"]) == 2
    assert ledger.usage().attempt_count == 1
    assert ledger.usage().known_cost_microusd == 0
    assert ledger.usage().unknown_cost_attempt_count == 1


def test_non_async_metadata_error_still_needs_no_fabricated_execution_id(tmp_path):
    ledger, task, _ = setup(tmp_path, "project_read")
    result = ledger.submit(task, lambda *_: message(task, status="error", success=False, error_category="PROVIDER_ERROR"))
    assert result.status == "error" and result.attempts[-1].actual_cost_microusd is None
