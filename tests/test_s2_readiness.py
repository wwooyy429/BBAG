"""SYNTHETIC_TEST ledger histories only; no provider, grant or dispatch."""
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Thread

import pytest
from pydantic import ValidationError

from bbag.s2_readiness import (
    CampaignCaps, CampaignReadiness, ExpectedLedger, ReadinessError,
    inspect_campaign, platform_gaps,
)
from bbag.s2_tasks import (
    RawMetadataRef, SynchronousCompletionRecord, TaskAttempt, TaskBudget, TaskLedger,
    TaskResponse, TaskSpec, canonical_task_bytes,
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def limits(anchors, **changes):
    return CampaignCaps(**{
        "expected_ledgers": anchors, "compile_attempt_limit": 2, "research_attempt_limit": 3,
        "max_new_cost_microusd": 0, "project_id": 101, "response_class": "SYNTHETIC_TEST",
        "start": date(2020, 1, 2), "end": date(2020, 1, 10), "assets": ["SPY", "AAPL"],
        "approval_reference": "SYNTHETIC_UNIT_CONTEXT_NOT_OWNER_AUTHENTICATION", **changes,
    })


def response(spec, **changes):
    return TaskResponse(**{**{name: getattr(spec, name) for name in (
        "operation", "source_commit", "config_sha256", "request_sha256", "project_id", "response_class")},
        "status": "completed", "success": True, "check_passed": True,
        "platform_task_id": "SYNTHETIC_" + spec.correlation_id, **changes})


def ledger(root, name, source="a", *, operations=("compile", "research_check"),
           budget_limits=None, state="completed", shared_provider=False):
    directory = root/name
    local_budget = budget_limits or {"max_attempts": 5, "operation_limits": {"compile": 2, "research_check": 3}}
    db = TaskLedger(directory/"tasks.sqlite", directory/"metadata", TaskBudget(
        response_class="SYNTHETIC_TEST", max_configurations=1, max_concurrent_attempts=1,
        max_cost_microusd=0, **local_budget))
    specs = []
    for operation in operations:
        spec = TaskSpec(correlation_id=name+"_"+operation, operation=operation, source_commit=source*40,
                        config_sha256="c"*64, request_sha256=hashlib.sha256((name+operation).encode()).hexdigest(),
                        project_id=101, response_class="SYNTHETIC_TEST")
        specs.append(spec)
        if state == "reserved":
            db.register(spec)
            db.begin_attempt(spec.correlation_id)
        else:
            changes = {"platform_task_id": "SYNTHETIC_SHARED_"+operation} if shared_provider else {}
            if state == "UNKNOWN":
                changes.update(status="UNKNOWN", success=None, check_passed=None, platform_task_id=None,
                               error_category="TIMEOUT")
            elif state == "accepted":
                changes.update(status="accepted", success=None, check_passed=None)
            db.submit(spec, lambda task, attempt: response(task, **changes))
    return db, specs, ExpectedLedger(database=str(directory.relative_to(root)/"tasks.sqlite"),
        metadata_directory=str(directory.relative_to(root)/"metadata"), expected_sha256=digest(directory/"tasks.sqlite"))


@pytest.fixture
def campaign(tmp_path):
    first = ledger(tmp_path, "first", "a")
    second = ledger(tmp_path, "second", "b", budget_limits={
        "max_attempts": 3, "operation_limits": {"compile": 1, "research_check": 2}})
    return first, second, limits([first[2], second[2]])


def repin(caps, root, index):
    data = caps.model_dump(mode="python")
    data["expected_ledgers"][index]["expected_sha256"] = digest(root/data["expected_ledgers"][index]["database"])
    return CampaignCaps.model_validate(data)


def edit_database(path, callback):
    with closing(sqlite3.connect(path)) as connection:
        callback(connection)
        connection.commit()


def test_actual_attempts_not_sum_of_local_budgets_and_unknown_bills_preserved(tmp_path, campaign, monkeypatch):
    first, second, caps = campaign
    before = inventory(tmp_path)
    monkeypatch.setattr(TaskLedger, "__init__", lambda *_a, **_k: pytest.fail("reader must never construct a ledger"))
    result = inspect_campaign(caps, root=tmp_path)
    assert result.used_attempts == {"compile": 2, "research_check": 2}
    assert result.remaining_attempts == {"compile": 0, "research_check": 1}
    assert [row.persisted_budget.max_attempts for row in result.ledgers] == [5, 3]
    assert result.configuration_count == result.source_count == 2
    assert result.config_sha256_count == 1 and result.request_count == result.request_sha256_count == 4
    assert result.billing_unknown_attempt_count == 4
    assert result.actual_billing_total_microusd is None and result.actual_billing_total_known is False
    assert result.known_cost_microusd == result.reserved_unknown_cost_microusd == 0
    assert [a.actual_cost_microusd for a in result.attempts] == [None]*4
    assert result.zero_reservation_proves_zero_bill is False
    assert result.independent_review == "PENDING" and result.platform_integration == "NOT_RUN"
    assert result.economic_evidence == "NOT_APPLICABLE" and result.scope_not_runtime_grant is True
    assert result.scope_data_boundaries_not_verified_by_task_hashes is True
    assert not any((result.dispatch_allowed, result.runtime_grant_enrolled, result.new_task_reserved,
                    result.new_runtime_ledger_created, result.next_stage_authorized))
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("name,value", [
    ("compile_attempt_limit", True), ("compile_attempt_limit", 3), ("research_attempt_limit", 4),
    ("max_new_cost_microusd", False), ("max_new_cost_microusd", 1),
    ("start", date(2020, 1, 1)), ("end", date(2020, 1, 11)),
    ("assets", ["OTHER"]), ("assets", ["SPY", "SPY"]), ("assets", []),
    ("expected_ledgers", []), ("project_id", True),
])
def test_caps_model_construct_copy_cannot_bypass_existing_owner_limits(tmp_path, campaign, monkeypatch, name, value):
    caps = campaign[2].model_copy(update={name: value})
    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: pytest.fail("invalid plan must precede database I/O"))
    with pytest.raises(ReadinessError):
        inspect_campaign(caps, root=tmp_path)


def test_drop_or_duplicate_declared_history_cannot_recreate_full_allowance(tmp_path, campaign):
    caps = campaign[2]
    for selected in ([caps.expected_ledgers[0]], [caps.expected_ledgers[0], caps.expected_ledgers[0]], []):
        with pytest.raises(ReadinessError, match="complete declared"):
            inspect_campaign(caps, root=tmp_path, ledgers=selected)
    with pytest.raises(ValidationError, match="duplicate/copy"):
        limits([caps.expected_ledgers[0], caps.expected_ledgers[0]])


def test_reordered_complete_inputs_have_same_usage_not_fresh_budget(tmp_path, campaign):
    caps = campaign[2]
    result = inspect_campaign(caps, root=tmp_path, ledgers=list(reversed(caps.expected_ledgers)))
    assert result.used_attempts == {"compile": 2, "research_check": 2}
    assert result.remaining_attempts == {"compile": 0, "research_check": 1}


@pytest.mark.parametrize("state,unknown,active", [("reserved", 1, 1), ("UNKNOWN", 1, 1), ("accepted", 0, 1)])
def test_unresolved_occupancy_and_unknown_cost_are_not_erased_or_skipped(tmp_path, state, unknown, active):
    _, _, anchor = ledger(tmp_path, "unfinished", operations=("research_check",), state=state)
    caps = limits([anchor])
    before = inventory(tmp_path)
    with pytest.raises(ReadinessError, match="unresolved/active") as caught:
        inspect_campaign(caps, root=tmp_path)
    assert caught.value.unknown_attempts == unknown and caught.value.active_attempts == active
    assert caught.value.unknown_billing_attempts == 1
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_even_empty_sidecars_prevent_an_immutable_checkpoint_claim(tmp_path, campaign, suffix):
    caps = campaign[2]
    path = tmp_path/caps.expected_ledgers[0].database
    Path(str(path)+suffix).write_bytes(b"")
    with pytest.raises(ReadinessError, match="sidecars"):
        inspect_campaign(caps, root=tmp_path)


def test_database_byte_anchor_is_external_to_self_consistent_json(tmp_path, campaign):
    caps = campaign[2]
    path = tmp_path/caps.expected_ledgers[0].database
    edit_database(path, lambda db: db.execute("UPDATE ledger_meta SET value='2.0' WHERE key='schema_version'"))
    with pytest.raises(ReadinessError, match="anchor"):
        inspect_campaign(caps, root=tmp_path)


@pytest.mark.parametrize("mutation", ["check", "cost", "event_hash", "source", "schema"])
def test_rehashing_database_does_not_hide_record_event_or_source_contradictions(tmp_path, campaign, mutation):
    caps = campaign[2]
    path = tmp_path/caps.expected_ledgers[0].database
    def change(db):
        if mutation in {"check", "cost"}:
            identity, raw = db.execute("SELECT id,attempt_json FROM attempts ORDER BY id LIMIT 1").fetchone()
            attempt = TaskAttempt.model_validate_json(raw)
            update = {"check_passed": False} if mutation == "check" else {"actual_cost_microusd": 0}
            altered = TaskAttempt.model_validate(attempt.model_copy(update=update))
            db.execute("UPDATE attempts SET attempt_json=? WHERE id=?", (canonical_task_bytes(altered).decode(), identity))
        elif mutation == "event_hash":
            db.execute("UPDATE events SET payload_sha256=?", ("0"*64,))
        elif mutation == "source":
            identity, raw = db.execute("SELECT id,spec_json FROM tasks LIMIT 1").fetchone()
            spec = TaskSpec.model_validate_json(raw).model_copy(update={"source_commit": "d"*40})
            db.execute("UPDATE tasks SET spec_json=? WHERE id=?", (canonical_task_bytes(spec).decode(), identity))
        else:
            db.execute("ALTER TABLE events ADD COLUMN unreviewed TEXT")
    edit_database(path, change)
    caps = repin(caps, tmp_path, 0)
    with pytest.raises(ReadinessError):
        inspect_campaign(caps, root=tmp_path)


@pytest.mark.parametrize("mutation", ["missing", "tamper", "extra"])
def test_metadata_must_exactly_match_immutable_event_references(tmp_path, campaign, mutation):
    caps = campaign[2]
    directory = tmp_path/caps.expected_ledgers[0].metadata_directory
    path = next(directory.iterdir())
    if mutation == "missing": path.unlink()
    elif mutation == "tamper": path.write_bytes(path.read_bytes()+b" ")
    else: (directory/"unreferenced.json").write_bytes(b"{}")
    with pytest.raises(ReadinessError):
        inspect_campaign(caps, root=tmp_path)


def test_no_database_or_metadata_paths_are_rebound_when_a_copy_is_offered(tmp_path, campaign):
    caps = campaign[2]
    original = tmp_path/caps.expected_ledgers[0].database
    copied = tmp_path/"copy.sqlite"
    copied.write_bytes(original.read_bytes())
    changed = caps.expected_ledgers[0].model_copy(update={"database": "copy.sqlite"})
    with pytest.raises(ReadinessError, match="complete declared"):
        inspect_campaign(caps, root=tmp_path, ledgers=[changed, caps.expected_ledgers[1]])
    assert copied.read_bytes() == original.read_bytes()


def test_distinct_database_digests_cannot_copy_an_existing_local_attempt(tmp_path, campaign):
    first, second, caps = campaign
    original_id = first[0].get(first[1][0].correlation_id).attempts[0].local_attempt_id
    path = tmp_path/caps.expected_ledgers[1].database
    def copy_attempt(db):
        previous, raw = db.execute("SELECT id,attempt_json FROM attempts ORDER BY id LIMIT 1").fetchone()
        altered = TaskAttempt.model_validate_json(raw).model_copy(update={"local_attempt_id": original_id})
        db.execute("UPDATE attempts SET id=?,attempt_json=? WHERE id=?", (original_id, canonical_task_bytes(altered).decode(), previous))
        db.execute("UPDATE events SET attempt_id=? WHERE attempt_id=?", (original_id, previous))
    edit_database(path, copy_attempt)
    with pytest.raises(ReadinessError, match="copied attempt"):
        inspect_campaign(repin(caps, tmp_path, 1), root=tmp_path)


def test_same_observed_provider_execution_is_not_two_actual_attempts(tmp_path):
    a = ledger(tmp_path, "one", "a", shared_provider=True)[2]
    b = ledger(tmp_path, "two", "b", shared_provider=True)[2]
    with pytest.raises(ReadinessError, match="same observed provider"):
        inspect_campaign(limits([a, b]), root=tmp_path)


def test_known_false_survives_multi_event_terminal_omission_and_first_zero_bill(tmp_path):
    db, specs, anchor = ledger(tmp_path, "terminal", operations=("research_check",), state="reserved")
    task = specs[0]
    attempt = db.get(task.correlation_id).attempts[0]
    db.record_event(attempt.local_attempt_id, "available", response(task, status="result_available", success=None, check_passed=False))
    for i in range(3):
        db.record_event(attempt.local_attempt_id, f"error_{i}", response(task, status="error", success=False,
            check_passed=None, error_category="PROVIDER_ERROR", actual_cost_microusd=0 if i == 2 else None))
    anchor = anchor.model_copy(update={"expected_sha256": digest(tmp_path/anchor.database)})
    result = inspect_campaign(limits([anchor]), root=tmp_path)
    assert result.attempts[0].check_passed is False and result.attempts[0].status == "error"
    assert result.used_attempts == {"compile": 0, "research_check": 1}
    assert result.actual_billing_total_microusd == 0 and result.actual_billing_total_known is True
    assert result.dispatch_allowed is False and result.platform_integration == "NOT_RUN"


def test_no_id_sync_confirmation_closes_without_inventing_provider_ids(tmp_path):
    db, specs, anchor = ledger(tmp_path, "sync", operations=("compile",), state="reserved")
    task = specs[0]; attempt = db.get(task.correlation_id).attempts[0]
    result = response(task, platform_task_id=None, check_passed=False)
    proof = SynchronousCompletionRecord(local_attempt_id=attempt.local_attempt_id,
        observed_at=datetime.now(timezone.utc), operation=task.operation, source_commit=task.source_commit,
        config_sha256=task.config_sha256, request_sha256=task.request_sha256, project_id=task.project_id,
        response_class="SYNTHETIC_TEST", response_metadata_sha256=hashlib.sha256(canonical_task_bytes(result)).hexdigest())
    db.record_synchronous_result(attempt.local_attempt_id, "fresh_tool_assertion", result, proof)
    anchor = anchor.model_copy(update={"expected_sha256": digest(tmp_path/anchor.database)})
    before = inventory(tmp_path)
    plan = inspect_campaign(limits([anchor]), root=tmp_path)
    assert plan.attempts[0].check_passed is False and plan.actual_billing_total_microusd is None
    assert inventory(tmp_path) == before


def test_new_scope_never_turns_the_preview_into_an_approval_or_grant(tmp_path, campaign):
    plan = inspect_campaign(campaign[2], root=tmp_path)
    assert len(platform_gaps()) == len(plan.platform_gaps) == 10
    assert {gap.execution for gap in plan.platform_gaps} == {"NOT_RUN"}
    for key, value in (("dispatch_allowed", True), ("dispatch_allowed", 0),
                       ("scope_not_runtime_grant", 1), ("independent_review", "APPROVED"),
                       ("platform_integration", "PASS"), ("billing_unknown_attempt_count", 0),
                       ("remaining_attempts", {"compile": 2, "research_check": 3}),
                       ("platform_gaps", plan.platform_gaps[:1])):
        with pytest.raises(ValidationError):
            CampaignReadiness.model_validate(plan.model_copy(update={key: value}))


def test_an_empty_fresh_database_cannot_replenish_campaign_allowance(tmp_path):
    path = tmp_path/"empty"
    TaskLedger(path/"tasks.sqlite", path/"metadata", TaskBudget(response_class="SYNTHETIC_TEST",
        max_attempts=5, max_configurations=1, max_concurrent_attempts=1, max_cost_microusd=0,
        operation_limits={"compile": 2, "research_check": 3}))
    anchor = ExpectedLedger(database="empty/tasks.sqlite", metadata_directory="empty/metadata",
                            expected_sha256=digest(path/"tasks.sqlite"))
    before = inventory(tmp_path)
    with pytest.raises(ReadinessError, match="empty"):
        inspect_campaign(limits([anchor]), root=tmp_path)
    assert inventory(tmp_path) == before


def test_missing_database_is_not_implicitly_created(tmp_path, monkeypatch):
    anchor = ExpectedLedger(database="not-created/tasks.sqlite", metadata_directory="not-created/metadata",
                            expected_sha256="0"*64)
    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: pytest.fail("missing path must precede SQLite"))
    with pytest.raises(ReadinessError):
        inspect_campaign(limits([anchor]), root=tmp_path)
    assert not (tmp_path/"not-created").exists()


def test_paths_outside_the_explicit_root_are_rejected_before_sqlite(tmp_path, campaign, monkeypatch):
    caps = campaign[2]
    data = caps.model_dump(mode="python")
    data["expected_ledgers"][0]["metadata_directory"] = "../outside-metadata"
    caps = CampaignCaps.model_validate(data)
    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: pytest.fail("root boundary must precede SQLite"))
    with pytest.raises(ReadinessError, match="explicit root"):
        inspect_campaign(caps, root=tmp_path)


def test_concurrent_change_to_already_read_predecessor_invalidates_the_whole_chain(tmp_path, campaign, monkeypatch):
    import bbag.s2_readiness as readiness
    caps = campaign[2]
    first = tmp_path/caps.expected_ledgers[0].database
    second_bytes = (tmp_path/caps.expected_ledgers[1].database).read_bytes()
    original_connect = sqlite3.connect
    original_snapshot = readiness._ledger_snapshot
    errors = []
    triggered = []
    def mutate_predecessor():
        try:
            with closing(original_connect(first)) as db:
                raw = db.execute("SELECT value FROM ledger_meta WHERE key='budget'").fetchone()[0]
                changed = TaskBudget.model_validate_json(raw).model_copy(update={"max_concurrent_attempts": 2})
                db.execute("UPDATE ledger_meta SET value=? WHERE key='budget'", (canonical_task_bytes(changed).decode(),))
                db.commit()
        except Exception as error:
            errors.append(error)
    def observe_snapshot(raw_db):
        if raw_db == second_bytes and not triggered:
            triggered.append(True)
            worker = Thread(target=mutate_predecessor)
            worker.start(); worker.join(timeout=10)
            assert not worker.is_alive() and not errors
        return original_snapshot(raw_db)
    monkeypatch.setattr(readiness, "_ledger_snapshot", observe_snapshot)
    with pytest.raises(ReadinessError, match="declared chain changed"):
        inspect_campaign(caps, root=tmp_path)
    assert triggered == [True] and digest(first) != caps.expected_ledgers[0].expected_sha256


def test_caller_metadata_keeps_its_exact_noncanonical_bytes(tmp_path):
    db, specs, anchor = ledger(tmp_path, "caller", operations=("compile",), state="reserved")
    task = specs[0]; attempt = db.get(task.correlation_id).attempts[0]
    result = response(task)
    raw = json.dumps(result.model_dump(mode="json"), indent=3).encode()+b"\n"
    record = db.record_event(attempt.local_attempt_id, "caller_bytes", result, raw_metadata=raw)
    reference = record.attempts[0].raw_metadata[0]
    assert reference.capture_kind == "CALLER_METADATA_BYTES"
    anchor = anchor.model_copy(update={"expected_sha256": digest(tmp_path/anchor.database)})
    before = inventory(tmp_path)
    plan = inspect_campaign(limits([anchor]), root=tmp_path)
    assert plan.ledgers[0].immutable_metadata_sha256[reference.relative_path] == hashlib.sha256(raw).hexdigest()
    assert (tmp_path/anchor.metadata_directory/reference.relative_path).read_bytes() == raw
    assert inventory(tmp_path) == before


def test_rehashed_pretty_bytes_cannot_claim_local_normalized_capture(tmp_path):
    db, specs, anchor = ledger(tmp_path, "false_local", operations=("compile",))
    directory = tmp_path/anchor.metadata_directory
    path = tmp_path/anchor.database
    def change(connection):
        identity, event_id, _, _, raw_ref = connection.execute("SELECT * FROM events").fetchone()
        reference = RawMetadataRef.model_validate_json(raw_ref)
        previous = directory/reference.relative_path
        raw = json.dumps(json.loads(previous.read_bytes()), indent=3).encode()
        digest_value = hashlib.sha256(raw).hexdigest()
        new_ref = reference.model_copy(update={"sha256": digest_value, "size_bytes": len(raw),
            "relative_path": reference.relative_path.replace(reference.sha256, digest_value)})
        (directory/new_ref.relative_path).write_bytes(raw)
        previous.unlink()
        row = connection.execute("SELECT attempt_json FROM attempts WHERE id=?", (identity,)).fetchone()[0]
        attempt = TaskAttempt.model_validate_json(row).model_copy(update={"raw_metadata": [new_ref]})
        connection.execute("UPDATE attempts SET attempt_json=? WHERE id=?", (canonical_task_bytes(attempt).decode(), identity))
        connection.execute("UPDATE events SET payload_sha256=?,raw_json=? WHERE attempt_id=? AND event_id=?",
                           (digest_value, canonical_task_bytes(new_ref).decode(), identity, event_id))
    edit_database(path, change)
    anchor = anchor.model_copy(update={"expected_sha256": digest(path)})
    with pytest.raises(ReadinessError, match="normalized local metadata"):
        inspect_campaign(limits([anchor]), root=tmp_path)
