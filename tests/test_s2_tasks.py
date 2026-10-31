"""SYNTHETIC_TEST control metadata only: no platform calls or market values."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from threading import Barrier, Event

from pydantic import ValidationError
import pytest

from bbag.s2_tasks import (
    S2_TASK_CONTRACTS, RawMetadataRef, TaskAttempt, TaskBudget, TaskLedger,
    TaskLedgerError, TaskReconciliation, TaskRecord, TaskResponse, TaskSpec,
    SynchronousCompletionRecord,
    canonical_task_bytes, task_configuration_id, task_request_id,
)


def budget(**updates):
    return TaskBudget(**{
        "response_class": "SYNTHETIC_TEST", "max_configurations": 5,
        "max_attempts": 20, "max_concurrent_attempts": 10, "max_cost_microusd": 1000,
        "operation_limits": {"compile": 10, "research_check": 10, "project_read": 10},
        **updates,
    })


def spec(**updates):
    return TaskSpec(**{
        "correlation_id": "synthetic-request", "operation": "compile",
        "source_commit": "a" * 40, "config_sha256": "c" * 64,
        "request_sha256": "d" * 64, "project_id": 123,
        "response_class": "SYNTHETIC_TEST", **updates,
    })


def response(task=None, **updates):
    task = task or spec()
    return TaskResponse(**{
        **{name: getattr(task, name) for name in (
            "operation", "source_commit", "config_sha256", "request_sha256",
            "project_id", "response_class",
        )},
        "status": "accepted", "platform_task_id": "synthetic-provider-1", **updates,
    })


def make_ledger(tmp_path, **updates):
    return TaskLedger(tmp_path / "tasks.sqlite", tmp_path / "metadata", budget(**updates))


def reconcile_proof(task=None, **updates):
    task = task or spec()
    return TaskReconciliation(**{
        "lookup_method": "PROVIDER_REQUEST_LOOKUP", "checked_at": datetime.now(timezone.utc),
        **{name: getattr(task, name) for name in (
            "operation", "source_commit", "config_sha256", "request_sha256", "project_id", "response_class",
        )}, **updates,
    })


@pytest.mark.parametrize("changes", [
    {"max_attempts": True}, {"max_attempts": -1}, {"max_attempts": 100001},
    {"max_concurrent_attempts": 0}, {"max_concurrent_attempts": 1001},
    {"max_cost_microusd": 0.0}, {"max_cost_microusd": -1},
    {"operation_limits": {"backtest": 1}}, {"operation_limits": {"compile": True}},
    {"purpose": "RESEARCH"}, {"status": "APPROVED"}, {"extra": "forbidden"},
])
def test_budget_strict_rejects_invalid_values(changes):
    with pytest.raises(ValidationError):
        TaskBudget(**changes)


def test_default_budget_is_draft_and_denies_dispatch(tmp_path):
    ledger = TaskLedger(tmp_path / "tasks.sqlite", tmp_path / "metadata", TaskBudget())
    actual = spec(response_class="CONTROL_ONLY")
    ledger.register(actual)
    with pytest.raises(TaskLedgerError, match="attempt budget"):
        ledger.begin_attempt(actual.correlation_id)
    assert ledger.usage().attempt_count == 0


@pytest.mark.parametrize("changes", [
    {"project_id": True}, {"project_id": 0}, {"source_commit": "x" * 40},
    {"request_sha256": "f" * 63}, {"correlation_id": "../escape"},
    {"operation": "backtest"}, {"request_payload": {"price": 1}},
])
def test_spec_strict_rejects_unsafe_or_unsupported_inputs(changes):
    with pytest.raises(ValidationError):
        spec(**changes)


@pytest.mark.parametrize("changes", [
    {"success": 1}, {"check_passed": "true"}, {"status": "COMPLETED"},
    {"status": "completed", "success": False},
    {"status": "error", "success": False},
    {"status": "accepted", "error_category": "TIMEOUT"},
    {"status": "accepted", "check_passed": True},
    {"cell_index": 1, "invocation_id": None},
    {"platform_task_id": "\nsecret"}, {"actual_cost_microusd": 0.01},
    {"bars": [{"close": 100}]}, {"price_sha256": "e" * 64},
])
def test_response_control_only_closed_shape(changes):
    with pytest.raises(ValidationError):
        response(**changes)


def test_contracts_export_schemas_and_revalidate_bypassed_instances():
    assert set(S2_TASK_CONTRACTS) == {
        "TaskBudget", "TaskSpec", "TaskResponse", "TaskReconciliation", "RawMetadataRef",
        "TaskAttempt", "TaskRecord", "TaskUsage",
        "SynchronousCompletionRecord", "SynchronousResultMetadata",
    }
    for model in S2_TASK_CONTRACTS.values():
        assert model.model_json_schema()["additionalProperties"] is False
    corrupt = spec().model_copy(update={"project_id": True})
    with pytest.raises(ValidationError):
        task_request_id(corrupt)
    with pytest.raises(TaskLedgerError):
        canonical_task_bytes({"close": 1})


def test_canonical_identity_aliases_and_config_count(tmp_path):
    ledger = make_ledger(tmp_path)
    first = ledger.register(spec())
    duplicate = ledger.register(spec(correlation_id="second-correlation"))
    assert duplicate.task_id == first.task_id == task_request_id(spec())
    assert duplicate.correlation_ids == ["second-correlation", "synthetic-request"]
    assert ledger.usage().configuration_count == ledger.usage().task_count == 1
    second_operation = spec(correlation_id="other-op", operation="research_check")
    ledger.register(second_operation)
    assert ledger.usage().task_count == 2
    assert ledger.usage().configuration_count == 1
    assert task_configuration_id(second_operation) == first.configuration_id
    assert task_request_id(second_operation) != first.task_id
    assert b'"purpose":"ENGINEERING_TEST"' in canonical_task_bytes(spec())


@pytest.mark.parametrize("changes", [
    {"source_commit": "b" * 40}, {"config_sha256": "e" * 64},
    {"request_sha256": "e" * 64}, {"project_id": 124}, {"operation": "research_check"},
])
def test_correlation_cannot_change_identity(tmp_path, changes):
    ledger = make_ledger(tmp_path)
    ledger.register(spec())
    with pytest.raises(TaskLedgerError, match="different request"):
        ledger.register(spec(**changes))
    assert ledger.usage().task_count == 1


def test_simulation_class_cannot_reuse_control_ledger(tmp_path):
    ledger = make_ledger(tmp_path)
    with pytest.raises(TaskLedgerError, match="response class"):
        ledger.register(spec(response_class="CONTROL_ONLY"))
    assert ledger.usage().configuration_count == 0


def test_distinct_configuration_budget_rollback(tmp_path):
    ledger = make_ledger(tmp_path, max_configurations=1)
    ledger.register(spec())
    with pytest.raises(TaskLedgerError, match="configuration budget"):
        ledger.register(spec(correlation_id="new-config", config_sha256="e" * 64))
    assert ledger.usage().configuration_count == ledger.usage().task_count == 1


def test_unknown_precedes_callback_and_crash_reopen(tmp_path):
    ledger = make_ledger(tmp_path)
    checked = []
    def crash(task, attempt):
        reopened = make_ledger(tmp_path)
        assert reopened.get(task.correlation_id).attempts[-1].status == "UNKNOWN"
        assert reopened.usage().attempt_count == 1
        checked.append(attempt.local_attempt_id)
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        ledger.submit(spec(), crash)
    reopened = make_ledger(tmp_path)
    assert reopened.get(spec().correlation_id).status == "UNKNOWN"
    assert reopened.usage().unknown_cost_attempt_count == 1
    assert len(checked) == 1
    with pytest.raises(TaskLedgerError, match="reconciled"):
        reopened.begin_attempt(spec().correlation_id, retry=True)


def test_timeout_no_resubmit_no_exception_body_saved(tmp_path):
    ledger = make_ledger(tmp_path)
    calls = []
    def fail(task, attempt):
        calls.append(attempt.local_attempt_id)
        raise TimeoutError("SECRET licensed close=12345")
    result = ledger.submit(spec(), fail, cost_limit_microusd=20)
    assert result.status == "UNKNOWN"
    attempt = result.attempts[-1]
    assert attempt.error_category == "TIMEOUT"
    assert attempt.actual_cost_microusd is None
    assert ledger.usage().reserved_unknown_cost_microusd == 20
    assert ledger.submit(spec(), fail).attempts[-1].local_attempt_id == attempt.local_attempt_id
    assert calls == [attempt.local_attempt_id]
    stored = b"".join(path.read_bytes() for path in ledger.raw_dir.iterdir())
    assert b"SECRET" not in stored and b"12345" not in stored
    assert len(list(ledger.raw_dir.iterdir())) == 1


def test_closed_gateway_error_category_is_preserved_without_message(tmp_path):
    class GatewayError(RuntimeError):
        error_category = "MALFORMED_RESPONSE"
    def fail(task, attempt):
        raise GatewayError("secret")
    result = make_ledger(tmp_path).submit(spec(), fail)
    assert result.status == "UNKNOWN"
    assert result.attempts[-1].error_category == "MALFORMED_RESPONSE"


@pytest.mark.parametrize("status", [
    "accepted", "compiling", "compiled", "running", "run_finished", "result_available", "completed",
])
def test_async_without_real_identifier_remains_unknown(tmp_path, status):
    ledger = make_ledger(tmp_path)
    result = ledger.submit(spec(), lambda *_: response(
        status=status, platform_task_id=None, success=True if status == "completed" else None,
    ))
    assert result.status == "UNKNOWN"
    attempt = result.attempts[-1]
    assert attempt.reported_status == status
    assert attempt.platform_task_id is None and attempt.actual_cost_microusd is None
    assert ledger.usage().confirmed_execution_count == 0
    with pytest.raises(TaskLedgerError, match="reconciled"):
        ledger.begin_attempt(spec().correlation_id, retry=True)


def test_actual_notebook_invocation_and_cell_identify_execution(tmp_path):
    ledger = make_ledger(tmp_path)
    task = spec(operation="research_check")
    result = ledger.submit(task, lambda *_: response(
        task, status="completed", success=True, check_passed=False,
        platform_task_id=None, invocation_id="synthetic-invocation", cell_index=0,
    ))
    assert result.status == "completed"
    assert result.attempts[-1].success is True
    assert result.attempts[-1].check_passed is False
    assert result.economic_evaluation == "NOT_EVALUATED"
    assert ledger.usage().confirmed_execution_count == 1


def test_metadata_read_can_finish_synchronously_without_fabricated_id(tmp_path):
    ledger = make_ledger(tmp_path)
    task = spec(operation="project_read")
    result = ledger.submit(task, lambda *_: response(
        task, status="completed", success=True, platform_task_id=None,
    ))
    assert result.status == "completed"
    assert ledger.usage().confirmed_execution_count == 0


def test_full_lifecycle_duplicate_completion_and_true_cost_once(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.register(spec())
    attempt = ledger.begin_attempt(spec().correlation_id, cost_limit_microusd=100)
    for status in ("accepted", "compiling", "compiled", "running", "run_finished", "result_available"):
        ledger.record_event(attempt.local_attempt_id, status, response(status=status))
    done = response(status="completed", success=True, check_passed=True, actual_cost_microusd=37)
    completed = ledger.record_event(attempt.local_attempt_id, "done", done)
    original_paths = {path.name for path in ledger.raw_dir.iterdir()}
    assert ledger.record_event(attempt.local_attempt_id, "done", done) == completed
    assert {path.name for path in ledger.raw_dir.iterdir()} == original_paths
    ledger.record_event(attempt.local_attempt_id, "duplicate-provider-message", done)
    usage = ledger.usage()
    assert usage.configuration_count == usage.attempt_count == usage.confirmed_execution_count == 1
    assert usage.known_cost_microusd == usage.budget_cost_microusd == 37
    assert usage.unknown_cost_attempt_count == usage.reserved_unknown_cost_microusd == 0
    assert usage.active_attempt_count == 0
    assert make_ledger(tmp_path).get(spec().correlation_id).status == "completed"


def test_same_event_id_cannot_bind_changed_bytes_or_cost(tmp_path):
    ledger = make_ledger(tmp_path)
    record = ledger.submit(spec(), lambda *_: response())
    attempt_id = record.attempts[-1].local_attempt_id
    with pytest.raises(TaskLedgerError, match="different metadata"):
        ledger.record_event(attempt_id, "dispatch_response", response(status="running"))
    assert len(list(ledger.raw_dir.iterdir())) == 1


@pytest.mark.parametrize("changes", [
    {"source_commit": "b" * 40}, {"config_sha256": "e" * 64},
    {"request_sha256": "e" * 64}, {"project_id": 124},
    {"response_class": "CONTROL_ONLY"}, {"operation": "research_check"},
])
def test_result_binding_rejects_wrong_provenance_before_write(tmp_path, changes):
    ledger = make_ledger(tmp_path)
    ledger.register(spec())
    attempt = ledger.begin_attempt(spec().correlation_id)
    with pytest.raises(TaskLedgerError, match="identity"):
        ledger.record_event(attempt.local_attempt_id, "wrong-binding", response(**changes))
    assert list(ledger.raw_dir.iterdir()) == []
    assert ledger.get(spec().correlation_id).status == "UNKNOWN"


def test_ids_cannot_change_and_later_poll_may_omit_known_id(tmp_path):
    ledger = make_ledger(tmp_path)
    result = ledger.submit(spec(), lambda *_: response())
    attempt_id = result.attempts[-1].local_attempt_id
    with pytest.raises(TaskLedgerError, match="identity cannot change"):
        ledger.record_event(attempt_id, "wrong-id", response(platform_task_id="different"))
    finished = ledger.record_event(attempt_id, "done", response(
        status="completed", success=True, platform_task_id=None,
    ))
    assert finished.status == "completed"
    assert finished.attempts[-1].platform_task_id == "synthetic-provider-1"


def test_state_regression_and_contradictory_terminal_rejected(tmp_path):
    ledger = make_ledger(tmp_path)
    result = ledger.submit(spec(), lambda *_: response(status="running"))
    attempt_id = result.attempts[-1].local_attempt_id
    with pytest.raises(TaskLedgerError, match="regress"):
        ledger.record_event(attempt_id, "stale", response(status="accepted"))
    ledger.record_event(attempt_id, "done", response(status="completed", success=True))
    with pytest.raises(TaskLedgerError, match="terminal"):
        ledger.record_event(attempt_id, "contradiction", response(
            status="error", success=False, error_category="PROVIDER_ERROR",
        ))


def test_unknown_can_be_reconciled_by_real_observed_task_then_finish(tmp_path):
    ledger = make_ledger(tmp_path)
    unknown = ledger.submit(spec(), lambda *_: response(platform_task_id=None))
    attempt_id = unknown.attempts[-1].local_attempt_id
    running = ledger.record_event(attempt_id, "lookup-found", response(status="running"))
    assert running.status == "running"
    assert ledger.usage().attempt_count == ledger.usage().confirmed_execution_count == 1
    final = ledger.record_event(attempt_id, "done", response(status="completed", success=True))
    assert final.status == "completed"


def test_not_submitted_reconciliation_then_explicit_retry_counts_attempt_not_config(tmp_path):
    ledger = make_ledger(tmp_path)
    initial = ledger.submit(spec(), lambda *_: response(platform_task_id=None))
    attempt_id = initial.attempts[-1].local_attempt_id
    with pytest.raises(TaskLedgerError, match="explicit not-submitted"):
        ledger.record_event(attempt_id, "unsafe-shortcut", response(status="incomplete", platform_task_id=None))
    proof = reconcile_proof()
    recovered = ledger.reconcile_not_submitted(attempt_id, "lookup-empty", proof)
    assert recovered.status == "incomplete"
    assert recovered.attempts[-1].actual_cost_microusd is None
    assert recovered.attempts[-1].reconciliation == proof
    assert ledger.reconcile_not_submitted(attempt_id, "lookup-empty", proof) == recovered
    with pytest.raises(TaskLedgerError, match="explicit retry"):
        ledger.begin_attempt(spec().correlation_id)
    retry = ledger.submit(spec(), lambda *_: response(status="completed", success=True), retry=True)
    assert retry.status == "completed"
    usage = ledger.usage()
    assert usage.configuration_count == 1 and usage.attempt_count == 2
    assert usage.confirmed_execution_count == 1
    assert usage.operation_attempts == {"compile": 2}


@pytest.mark.parametrize("proof_updates", [
    {"checked_at": datetime(2000, 1, 1, tzinfo=timezone.utc)},
    {"checked_at": datetime.now(timezone.utc) + timedelta(days=1)},
    {"request_sha256": "e" * 64},
])
def test_reconciliation_boundaries_do_not_unblock_unknown(tmp_path, proof_updates):
    ledger = make_ledger(tmp_path)
    result = ledger.submit(spec(), lambda *_: response(platform_task_id=None))
    with pytest.raises(TaskLedgerError):
        ledger.reconcile_not_submitted(result.attempts[-1].local_attempt_id, "bad-proof", reconcile_proof(**proof_updates))
    assert ledger.get(spec().correlation_id).status == "UNKNOWN"
    assert len(list(ledger.raw_dir.iterdir())) == 1


def test_not_submitted_proof_cannot_erase_observed_execution(tmp_path):
    ledger = make_ledger(tmp_path)
    result = ledger.submit(spec(), lambda *_: response())
    with pytest.raises(TaskLedgerError, match="contradicts"):
        ledger.reconcile_not_submitted(result.attempts[-1].local_attempt_id, "bad-proof", reconcile_proof())


def test_retry_spends_new_attempt_and_operation_budget(tmp_path):
    ledger = make_ledger(tmp_path, operation_limits={"compile": 2}, max_attempts=3)
    def fail(*_):
        return response(status="error", success=False, error_category="PROVIDER_ERROR")
    ledger.submit(spec(), fail)
    ledger.submit(spec(), fail, retry=True)
    with pytest.raises(TaskLedgerError, match="operation attempt budget"):
        ledger.submit(spec(), fail, retry=True)
    assert ledger.usage().attempt_count == 2


def test_operation_limits_separate_compile_and_research(tmp_path):
    ledger = make_ledger(tmp_path, operation_limits={"compile": 1, "research_check": 1})
    ledger.submit(spec(), lambda *_: response(status="completed", success=True))
    research = spec(correlation_id="research", operation="research_check")
    ledger.submit(research, lambda *_: response(research, status="completed", success=True))
    assert ledger.usage().operation_attempts == {"compile": 1, "research_check": 1}
    denied = spec(correlation_id="denied-read", operation="project_read")
    ledger.register(denied)
    with pytest.raises(TaskLedgerError, match="operation attempt budget"):
        ledger.begin_attempt(denied.correlation_id)


@pytest.mark.parametrize("updates,match", [
    ({"max_attempts": 0}, "attempt budget"),
    ({"operation_limits": {}}, "operation attempt budget"),
    ({"max_cost_microusd": 19}, "cost reservation"),
])
def test_budget_failure_prevents_transport_and_spends_nothing(tmp_path, updates, match):
    ledger = make_ledger(tmp_path, **updates)
    called = []
    with pytest.raises(TaskLedgerError, match=match):
        ledger.submit(spec(), lambda *_: called.append(True), cost_limit_microusd=20)
    assert called == []
    assert ledger.usage().attempt_count == 0


@pytest.mark.parametrize("cost", [True, -1, 0.0, 10**15 + 1])
def test_attempt_reservations_are_strict_integer_money(tmp_path, cost):
    ledger = make_ledger(tmp_path)
    ledger.register(spec())
    with pytest.raises(TaskLedgerError, match="integer"):
        ledger.begin_attempt(spec().correlation_id, cost_limit_microusd=cost)


def test_known_overspend_is_preserved_and_stops_future_attempts(tmp_path):
    ledger = make_ledger(tmp_path, max_cost_microusd=50)
    ledger.submit(spec(), lambda *_: response(
        status="completed", success=True, actual_cost_microusd=60,
    ), cost_limit_microusd=40)
    assert ledger.usage().known_cost_microusd == 60
    second = spec(correlation_id="other-request", request_sha256="e" * 64)
    ledger.register(second)
    with pytest.raises(TaskLedgerError, match="cost reservation"):
        ledger.begin_attempt(second.correlation_id)


def test_cost_integer_sum_unknown_reservation_and_immutability(tmp_path):
    ledger = make_ledger(tmp_path, max_cost_microusd=100)
    result = ledger.submit(spec(), lambda *_: response(
        status="completed", success=True, actual_cost_microusd=31,
    ), cost_limit_microusd=40)
    attempt_id = result.attempts[-1].local_attempt_id
    with pytest.raises(TaskLedgerError, match="confirmed cost"):
        ledger.record_event(attempt_id, "revised-bill", response(
            status="completed", success=True, actual_cost_microusd=32,
        ))
    second = spec(correlation_id="other-request", request_sha256="e" * 64)
    ledger.submit(second, lambda *_: response(second), cost_limit_microusd=69)
    assert ledger.usage().budget_cost_microusd == 100
    third = spec(correlation_id="third-request", request_sha256="f" * 64)
    ledger.register(third)
    with pytest.raises(TaskLedgerError, match="cost reservation"):
        ledger.begin_attempt(third.correlation_id, cost_limit_microusd=1)


def test_exact_safe_caller_bytes_hash_and_fresh_immutable_file(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.register(spec())
    attempt = ledger.begin_attempt(spec().correlation_id)
    payload = response()
    original_bytes = (json.dumps(payload.model_dump(mode="json"), indent=3) + "\n").encode()
    result = ledger.record_event(attempt.local_attempt_id, "message-1", payload, raw_metadata=original_bytes)
    reference = result.attempts[-1].raw_metadata[-1]
    assert reference.capture_kind == "CALLER_METADATA_BYTES"
    assert reference.evidence_authentication == "NOT_AUTHENTICATED"
    assert reference.market_data_export == "NOT_SUPPORTED"
    assert reference.sha256 == hashlib.sha256(original_bytes).hexdigest()
    assert reference.size_bytes == len(original_bytes)
    path = ledger.raw_dir / reference.relative_path
    assert path.read_bytes() == original_bytes
    ledger.record_event(attempt.local_attempt_id, "message-2", response(status="running"))
    assert path.read_bytes() == original_bytes
    assert len(list(ledger.raw_dir.iterdir())) == 2
    ledger.verify_integrity()


@pytest.mark.parametrize("payload", [
    b'{"bars":[{"close":123}]}',
    b'{"status":"accepted","status":"completed"}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}',
    b'[]', b'null', b'not json', b'\xff', b'{}',
])
def test_unsafe_bytes_neither_saved_nor_hashed_in_ledger(tmp_path, payload):
    ledger = make_ledger(tmp_path)
    ledger.register(spec())
    attempt = ledger.begin_attempt(spec().correlation_id)
    with pytest.raises(TaskLedgerError, match="exact allowlisted") as caught:
        ledger.record_event(attempt.local_attempt_id, "unsafe", response(), raw_metadata=payload)
    assert "123" not in str(caught.value)
    assert list(ledger.raw_dir.iterdir()) == []
    with sqlite3.connect(ledger.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert hashlib.sha256(payload).hexdigest() not in " ".join(connection.iterdump())


def test_bypassed_response_extra_data_is_rejected_with_safe_error(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.register(spec())
    attempt = ledger.begin_attempt(spec().correlation_id)
    corrupt = TaskResponse.model_construct(**response().model_dump(), bars=[{"close": 123}])
    # model_construct silently drops extra keys for extra=forbid; mutate the
    # instance dictionary to exercise a genuine bypass rather than that no-op.
    corrupt.__dict__["bars"] = [{"close": 123}]
    with pytest.raises(TaskLedgerError, match="allowlisted") as caught:
        ledger.record_event(attempt.local_attempt_id, "unsafe", corrupt)
    assert "123" not in str(caught.value)
    assert list(ledger.raw_dir.iterdir()) == []


def test_reopen_binds_budget_and_raw_root_and_detects_tampering(tmp_path):
    ledger = make_ledger(tmp_path)
    result = ledger.submit(spec(), lambda *_: response())
    assert make_ledger(tmp_path).get(spec().correlation_id) == result
    with pytest.raises(TaskLedgerError, match="persisted budget"):
        make_ledger(tmp_path, max_attempts=21)
    with pytest.raises(TaskLedgerError, match="metadata root"):
        TaskLedger(ledger.db_path, tmp_path / "different-raw", budget())
    reference = result.attempts[-1].raw_metadata[0]
    path = ledger.raw_dir / reference.relative_path
    path.write_bytes(b"changed")
    with pytest.raises(TaskLedgerError, match="hash"):
        make_ledger(tmp_path)


def test_missing_raw_file_detected_on_reopen(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.submit(spec(), lambda *_: response())
    next(ledger.raw_dir.iterdir()).unlink()
    with pytest.raises(TaskLedgerError, match="missing"):
        make_ledger(tmp_path)


def test_no_database_inside_raw_metadata_root(tmp_path):
    with pytest.raises(TaskLedgerError, match="must not be inside"):
        TaskLedger(tmp_path / "raw" / "tasks.sqlite", tmp_path / "raw", budget())


def test_nested_metadata_directory_is_not_accepted(tmp_path):
    root = tmp_path / "raw"
    (root / "nested").mkdir(parents=True)
    with pytest.raises(TaskLedgerError, match="only regular files"):
        TaskLedger(tmp_path / "tasks.sqlite", root, budget())


def test_redirected_root_or_child_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    def redirect(path):
        try:
            path.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                pytest.skip("host does not grant link creation")
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(path), str(target)], capture_output=True,
            )
            if result.returncode:
                pytest.skip("host does not grant link or directory junction creation")
    redirect(link)
    try:
        with pytest.raises(TaskLedgerError, match="links or junctions"):
            TaskLedger(tmp_path / "tasks.sqlite", link, budget())
    finally:
        if link.is_symlink():
            link.unlink()
        else:
            os.rmdir(link)  # Remove only the verified junction, never its target.
    ledger = make_ledger(tmp_path)
    child = ledger.raw_dir / "redirect"
    redirect(child)
    try:
        with pytest.raises(TaskLedgerError, match="only regular files"):
            ledger.submit(spec(), lambda *_: response())
        assert ledger.usage().attempt_count == 1  # dispatch intent remains durable
    finally:
        if child.is_symlink():
            child.unlink()
        else:
            os.rmdir(child)


def test_concurrent_alias_registration_and_initial_submit_once(tmp_path):
    ledger = make_ledger(tmp_path)
    ready = Barrier(8)
    calls = []
    def work(index):
        other = make_ledger(tmp_path)
        ready.wait(timeout=10)
        return other.submit(spec(correlation_id=f"alias-{index}"), lambda task, attempt: (
            calls.append(attempt.local_attempt_id) or response(task)
        ))
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(work, range(8)))
    assert len(calls) == 1
    assert len({item.task_id for item in records}) == 1
    assert ledger.usage().configuration_count == ledger.usage().attempt_count == 1
    assert len(ledger.get("alias-0").correlation_ids) == 8


def test_concurrent_duplicate_event_does_not_double_count(tmp_path):
    ledger = make_ledger(tmp_path)
    result = ledger.submit(spec(), lambda *_: response())
    attempt_id = result.attempts[-1].local_attempt_id
    ready = Barrier(6)
    def finish(_):
        other = make_ledger(tmp_path)
        ready.wait(timeout=10)
        return other.record_event(attempt_id, "same-completion", response(
            status="completed", success=True, actual_cost_microusd=7,
        ))
    with ThreadPoolExecutor(max_workers=6) as pool:
        records = list(pool.map(finish, range(6)))
    assert all(item.status == "completed" for item in records)
    assert ledger.usage().known_cost_microusd == 7
    assert ledger.usage().attempt_count == 1
    assert len(list(ledger.raw_dir.iterdir())) == 2


def test_concurrent_global_attempt_cap_is_atomic(tmp_path):
    ledger = make_ledger(tmp_path, max_attempts=1)
    for index in range(6):
        ledger.register(spec(correlation_id=f"request-{index}", request_sha256=f"{index:064x}"))
    ready = Barrier(6)
    def begin(index):
        ready.wait(timeout=10)
        try:
            return ledger.begin_attempt(f"request-{index}")
        except TaskLedgerError:
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        attempts = list(pool.map(begin, range(6)))
    assert sum(item is not None for item in attempts) == 1
    assert ledger.usage().attempt_count == 1


def test_unresolved_attempt_blocks_other_task_under_concurrent_limit(tmp_path):
    ledger = make_ledger(tmp_path, max_concurrent_attempts=1)
    ledger.submit(spec(), lambda *_: response(platform_task_id=None))
    other = spec(correlation_id="other", request_sha256="e" * 64)
    ledger.register(other)
    with pytest.raises(TaskLedgerError, match="concurrent attempt"):
        ledger.begin_attempt(other.correlation_id)


def test_failed_metadata_write_retains_unknown_reservation(tmp_path, monkeypatch):
    ledger = make_ledger(tmp_path)
    def disk_failure(*args, **kwargs):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(ledger, "_write_metadata", disk_failure)
    with pytest.raises(OSError):
        ledger.submit(spec(), lambda *_: response(status="completed", success=True))
    persisted = make_ledger(tmp_path).get(spec().correlation_id)
    assert persisted.status == "UNKNOWN"
    assert len(persisted.attempts) == 1
    assert persisted.attempts[-1].raw_metadata == []


def test_record_model_detects_inconsistent_summary(tmp_path):
    ledger = make_ledger(tmp_path)
    record = ledger.submit(spec(), lambda *_: response())
    with pytest.raises(ValidationError):
        TaskRecord.model_validate(record.model_copy(update={"status": "completed"}))
    with pytest.raises(ValidationError):
        RawMetadataRef(relative_path="metadata_" + "a" * 32 + "_" + "b" * 64 + ".json",
                       sha256="c" * 64, size_bytes=1, capture_kind="LOCAL_NORMALIZED_METADATA",
                       payload_kind="TaskResponse")


def test_in_memory_budget_mutation_cannot_expand_persisted_caps(tmp_path):
    original = budget(max_attempts=0, operation_limits={})
    ledger = TaskLedger(tmp_path / "tasks.sqlite", tmp_path / "metadata", original)
    original.max_attempts = 100
    original.operation_limits["compile"] = 100
    snapshot = ledger.budget
    snapshot.max_attempts = 100
    snapshot.operation_limits["compile"] = 100
    ledger.register(spec())
    with pytest.raises(TaskLedgerError, match="attempt budget"):
        ledger.begin_attempt(spec().correlation_id)
    assert ledger.budget.max_attempts == 0
    assert ledger.budget.operation_limits == {}
    assert ledger.usage().attempt_count == 0


def test_non_callable_transport_is_not_a_dispatch_intent(tmp_path):
    ledger = make_ledger(tmp_path)
    with pytest.raises(TaskLedgerError, match="injected callable"):
        ledger.submit(spec(), None)
    assert ledger.usage().attempt_count == ledger.usage().task_count == 0


@pytest.mark.parametrize("unrelated", [True, False])
def test_unreferenced_unsafe_files_are_not_silently_accepted(tmp_path, unrelated):
    raw = tmp_path / "metadata"
    raw.mkdir()
    payload = b'{"secret":"SYNTHETIC_UNSAFE_SENTINEL","bars":[123]}'
    name = ("secrets.json" if unrelated else "metadata_" + "a" * 32 + "_"
            + hashlib.sha256(payload).hexdigest() + ".json")
    (raw / name).write_bytes(payload)
    with pytest.raises(TaskLedgerError):
        TaskLedger(tmp_path / "tasks.sqlite", raw, budget())


def test_safe_orphan_from_interrupted_transaction_can_reopen(tmp_path):
    ledger = make_ledger(tmp_path)
    payload = canonical_task_bytes(response())
    orphan = ledger.raw_dir / ("metadata_" + "a" * 32 + "_"
                              + hashlib.sha256(payload).hexdigest() + ".json")
    orphan.write_bytes(payload)
    reopened = make_ledger(tmp_path)
    assert reopened.usage().attempt_count == 0
    assert orphan.read_bytes() == payload


def test_failed_atomic_publication_leaves_unknown_and_no_partial_raw(tmp_path, monkeypatch):
    ledger = make_ledger(tmp_path)
    def unavailable(*args, **kwargs):
        raise OSError("synthetic publication failure")
    monkeypatch.setattr(os, "link", unavailable)
    with pytest.raises(OSError):
        ledger.submit(spec(), lambda *_: response())
    assert list(ledger.raw_dir.iterdir()) == []
    assert list(tmp_path.glob(".metadata_pending_*")) == []
    assert make_ledger(tmp_path).get(spec().correlation_id).status == "UNKNOWN"


def synchronous_proof(attempt, payload, **updates):
    return SynchronousCompletionRecord(**{
        "local_attempt_id": attempt.local_attempt_id,
        "observed_at": datetime.now(timezone.utc),
        **{name: getattr(payload, name) for name in (
            "operation", "source_commit", "config_sha256", "request_sha256", "project_id", "response_class",
        )},
        "response_metadata_sha256": hashlib.sha256(canonical_task_bytes(payload)).hexdigest(),
        **updates,
    })


def test_explicit_fresh_sync_result_closes_without_id_and_releases_concurrency(tmp_path):
    ledger = make_ledger(tmp_path, max_concurrent_attempts=1)
    ledger.register(spec())
    attempt = ledger.begin_attempt(spec().correlation_id)
    payload = response(status="completed", success=True, platform_task_id=None)
    proof = synchronous_proof(attempt, payload)
    result = ledger.record_synchronous_result(attempt.local_attempt_id, "observed-tool-return", payload, proof)
    assert result.status == "completed"
    saved = result.attempts[-1]
    assert saved.platform_task_id is None and saved.invocation_id is None
    assert saved.sync_confirmation == proof
    assert saved.actual_cost_microusd is None
    assert saved.sync_confirmation.provider_task_id_availability == "NOT_RETURNED"
    assert saved.sync_confirmation.evidence_authentication == "NOT_AUTHENTICATED"
    assert saved.raw_metadata[0].payload_kind == "SynchronousResultMetadata"
    captured = json.loads((ledger.raw_dir / saved.raw_metadata[0].relative_path).read_bytes())
    assert captured["response"]["status"] == "completed"
    assert captured["confirmation"]["response_metadata_sha256"] == hashlib.sha256(canonical_task_bytes(payload)).hexdigest()
    usage = ledger.usage()
    assert usage.synchronous_result_count == 1 and usage.confirmed_execution_count == 0
    assert usage.unknown_cost_attempt_count == 1 and usage.active_attempt_count == 0
    research = spec(correlation_id="followup-research", operation="research_check")
    ledger.register(research)
    assert ledger.begin_attempt(research.correlation_id).status == "UNKNOWN"
    reopened = make_ledger(tmp_path, max_concurrent_attempts=1)
    assert reopened.get(spec().correlation_id).attempts[-1].sync_confirmation == proof
    assert reopened.usage().synchronous_result_count == 1
    assert reopened.usage().attempt_count == 2


@pytest.mark.parametrize("changes", [
    {"observed_at": datetime(2000, 1, 1, tzinfo=timezone.utc)},
    {"observed_at": datetime.now(timezone.utc) + timedelta(days=1)},
    {"local_attempt_id": "local_" + "f" * 32},
    {"source_commit": "b" * 40}, {"project_id": 124},
    {"response_metadata_sha256": "f" * 64},
])
def test_sync_confirmation_wrong_time_nonce_identity_or_hash_does_not_close(tmp_path, changes):
    ledger = make_ledger(tmp_path)
    ledger.register(spec())
    attempt = ledger.begin_attempt(spec().correlation_id)
    payload = response(status="completed", success=True, platform_task_id=None)
    proof = synchronous_proof(attempt, payload, **changes)
    with pytest.raises(TaskLedgerError):
        ledger.record_synchronous_result(attempt.local_attempt_id, "invalid-sync", payload, proof)
    assert ledger.get(spec().correlation_id).status == "UNKNOWN"
    assert ledger.usage().synchronous_result_count == 0
    assert list(ledger.raw_dir.iterdir()) == []


@pytest.mark.parametrize("value", [1, 0, False, "true"])
def test_sync_completion_assertion_is_strict_boolean(value):
    payload = response(status="completed", success=True, platform_task_id=None)
    local = TaskAttempt(local_attempt_id="local_" + "a" * 32, task_id="b" * 64,
                        attempt_number=1, created_at=datetime.now(timezone.utc),
                        status="UNKNOWN", reserved_cost_microusd=0)
    with pytest.raises(ValidationError):
        synchronous_proof(local, payload, completion_observed=value)


@pytest.mark.parametrize("status", ["accepted", "running", "UNKNOWN"])
def test_unfinished_tool_claim_is_not_synchronous_completion(tmp_path, status):
    ledger = make_ledger(tmp_path)
    ledger.register(spec())
    attempt = ledger.begin_attempt(spec().correlation_id)
    payload = response(status=status, platform_task_id=None)
    proof = synchronous_proof(attempt, payload)
    with pytest.raises(TaskLedgerError, match="allowlisted"):
        ledger.record_synchronous_result(attempt.local_attempt_id, "unfinished-sync", payload, proof)
    assert ledger.get(spec().correlation_id).status == "UNKNOWN"
    assert list(ledger.raw_dir.iterdir()) == []


def test_normal_import_without_sync_observation_still_cannot_close(tmp_path):
    ledger = make_ledger(tmp_path)
    result = ledger.submit(spec(), lambda *_: response(status="completed", success=True, platform_task_id=None))
    assert result.status == "UNKNOWN" and result.attempts[-1].sync_confirmation is None
    assert ledger.usage().synchronous_result_count == 0
    assert ledger.usage().active_attempt_count == 1


def test_sync_error_is_a_finished_execution_with_unknown_bill_then_retry_is_explicit(tmp_path):
    ledger = make_ledger(tmp_path, max_concurrent_attempts=1)
    ledger.register(spec())
    attempt = ledger.begin_attempt(spec().correlation_id)
    payload = response(status="error", success=False, platform_task_id=None, error_category="PROVIDER_ERROR")
    proof = synchronous_proof(attempt, payload)
    failed = ledger.record_synchronous_result(attempt.local_attempt_id, "observed-error", payload, proof)
    assert failed.status == "error" and failed.attempts[-1].actual_cost_microusd is None
    assert ledger.usage().synchronous_result_count == 1 and ledger.usage().confirmed_execution_count == 0
    with pytest.raises(TaskLedgerError, match="explicit retry"):
        ledger.begin_attempt(spec().correlation_id)
    next_attempt = ledger.begin_attempt(spec().correlation_id, retry=True)
    with pytest.raises(TaskLedgerError, match="current unknown reservation"):
        ledger.record_synchronous_result(next_attempt.local_attempt_id, "replayed-old-observation", payload, proof)
    assert ledger.get(spec().correlation_id).status == "UNKNOWN"
    assert ledger.usage().attempt_count == 2 and ledger.usage().synchronous_result_count == 1


def test_sync_confirmation_does_not_hide_existing_provider_identifier(tmp_path):
    ledger = make_ledger(tmp_path)
    accepted = ledger.submit(spec(), lambda *_: response())
    attempt = accepted.attempts[-1]
    payload = response(status="completed", success=True, platform_task_id=None)
    proof = synchronous_proof(attempt, payload)
    with pytest.raises(TaskLedgerError):
        ledger.record_synchronous_result(attempt.local_attempt_id, "hide-observed-id", payload, proof)
    assert ledger.get(spec().correlation_id).status == "accepted"


def test_identified_response_uses_standard_events_instead_of_sync_exception(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.register(spec())
    attempt = ledger.begin_attempt(spec().correlation_id)
    payload = response(status="completed", success=True)
    proof = synchronous_proof(attempt, payload)
    with pytest.raises(TaskLedgerError):
        ledger.record_synchronous_result(attempt.local_attempt_id, "unneeded-exception", payload, proof)
    assert ledger.record_event(attempt.local_attempt_id, "identified-completion", payload).status == "completed"
    assert ledger.usage().confirmed_execution_count == 1 and ledger.usage().synchronous_result_count == 0


def test_concurrent_duplicate_sync_results_do_not_double_count_or_cost(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.register(spec())
    attempt = ledger.begin_attempt(spec().correlation_id)
    payload = response(status="completed", success=True, platform_task_id=None)
    proof = synchronous_proof(attempt, payload)
    ready = Barrier(6)
    def close(_):
        other = make_ledger(tmp_path)
        ready.wait(timeout=10)
        return other.record_synchronous_result(attempt.local_attempt_id, "same-sync-result", payload, proof)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(close, range(6)))
    assert all(item.status == "completed" for item in results)
    assert ledger.usage().synchronous_result_count == ledger.usage().attempt_count == 1
    assert ledger.usage().confirmed_execution_count == 0
    assert ledger.usage().unknown_cost_attempt_count == 1
    assert len(list(ledger.raw_dir.iterdir())) == 1
