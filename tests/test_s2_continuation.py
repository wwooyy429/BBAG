"""Synthetic closed ledgers only; no grants, provider calls or real history."""
from contextlib import closing
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

import bbag.s2_continuation as continuation
from bbag.s2_continuation import (
    ContinuationPlan, ContinuationPreview, OwnerBudgetIncrement,
    campaign_baseline_sha256, check_continuation_budget,
    inspect_continuation, owner_increment_sha256,
)
from bbag.s2_readiness import CampaignCaps, ExpectedLedger, ReadinessError, inspect_campaign
from bbag.s2_tasks import TaskBudget, TaskLedger, TaskResponse, TaskSpec


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def make_ledger(root, name, *, source="a", operations=("compile", "research_check"),
                state="completed", response_class="SYNTHETIC_TEST", empty=False):
    directory = root/name
    db = TaskLedger(directory/"tasks.sqlite", directory/"metadata", TaskBudget(
        response_class=response_class, max_configurations=1, max_attempts=5,
        max_concurrent_attempts=1, max_cost_microusd=0,
        operation_limits={"compile": 2, "research_check": 3}))
    for operation in (() if empty else operations):
        spec = TaskSpec(correlation_id=name+"_"+operation, operation=operation,
            source_commit=source*40, config_sha256="c"*64,
            request_sha256=hashlib.sha256((name+operation).encode()).hexdigest(),
            project_id=101, response_class=response_class)
        changes = {"status": "completed", "success": True, "check_passed": True,
                   "platform_task_id": "SYNTHETIC_"+name+"_"+operation}
        if state == "UNKNOWN":
            changes.update(status="UNKNOWN", success=None, check_passed=None,
                           platform_task_id=None, error_category="TIMEOUT")
        elif state == "accepted":
            changes.update(status="accepted", success=None, check_passed=None)
        if state == "reserved":
            db.register(spec)
            db.begin_attempt(spec.correlation_id)
        else:
            db.submit(spec, lambda task, attempt: TaskResponse(**{
                **{key: getattr(task, key) for key in ("operation", "source_commit", "config_sha256",
                    "request_sha256", "project_id", "response_class")}, **changes}))
    return ExpectedLedger(database=str(directory.relative_to(root)/"tasks.sqlite"),
        metadata_directory=str(directory.relative_to(root)/"metadata"), expected_sha256=digest(directory/"tasks.sqlite"))


@pytest.fixture
def context(tmp_path):
    first, second = make_ledger(tmp_path, "first"), make_ledger(tmp_path, "second", source="b")
    caps = CampaignCaps(expected_ledgers=[first, second], compile_attempt_limit=2,
        research_attempt_limit=3, max_new_cost_microusd=0, project_id=101, response_class="SYNTHETIC_TEST",
        start=date(2020, 1, 2), end=date(2020, 1, 10), assets=["SPY", "AAPL"],
        approval_reference="SYNTHETIC_UNIT_CONTEXT_NOT_OWNER_AUTHENTICATION")
    history = inspect_campaign(caps, root=tmp_path)
    plan = ContinuationPlan(source_commit="d"*40, config_sha256="c"*64, historical_campaign=history)
    return plan, {"root": tmp_path, "expected_baseline_sha256": campaign_baseline_sha256(history),
                  "required_synthetic_ledgers": ()}


def approved(plan, context, **changes):
    increment = OwnerBudgetIncrement(status="OWNER_APPROVED", baseline_sha256=context["expected_baseline_sha256"],
        source_commit=plan.source_commit, config_sha256=plan.config_sha256,
        approval_reference="SYNTHETIC_TEST_NO_RUNTIME_GRANT", **changes)
    plan = ContinuationPlan.model_validate(plan.model_copy(update={"increment": increment}))
    return plan, {**context, "trusted_owner_authorization_sha256s": frozenset({owner_increment_sha256(increment)})}


def test_zero_increment_keeps_original_four_attempts_and_reserved_research(tmp_path, context, monkeypatch):
    plan, arguments = context
    before = inventory(tmp_path)
    monkeypatch.setattr(TaskLedger, "__init__", lambda *_a, **_k: pytest.fail("preview cannot construct a ledger"))
    result = inspect_continuation(plan, **arguments)
    assert result.used_attempts.model_dump() == {"compile": 2, "research_check": 2}
    assert result.cumulative_caps.model_dump() == {"compile": 2, "research_check": 3}
    assert result.remaining_attempts.model_dump() == {"compile": 0, "research_check": 1}
    assert result.available_attempts.model_dump() == {"compile": 0, "research_check": 0}
    assert result.billing_unknown_attempt_count == 4
    assert result.actual_billing_total_microusd is None
    assert result.known_cost_microusd == result.reserved_unknown_cost_microusd == 0
    assert all(a.actual_cost_microusd is None for a in result.historical_campaign.attempts)
    assert result.historical_campaign == plan.historical_campaign
    assert result.research_reserve_intact and not result.authorization_recognized
    assert result.independent_review == "PENDING" and result.platform_integration == "NOT_RUN"
    assert not any((result.dispatch_allowed, result.runtime_grant_enrolled,
                    result.new_runtime_ledger_created, result.new_task_reserved, result.real_additional_ledger_support))
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("operation", ["compile", "research_check"])
def test_no_default_budget_even_with_real_unused_research(context, operation):
    plan, arguments = context
    with pytest.raises(ValueError, match="remains reserved"):
        check_continuation_budget(plan, operation, **arguments)


@pytest.mark.parametrize("status,trusted,expected", [("DRAFT", False, False), ("DRAFT", True, False),
    ("OWNER_APPROVED", False, False), ("OWNER_APPROVED", True, True)])
def test_json_claim_needs_external_allowlist_and_approved_status(context, status, trusted, expected):
    plan, arguments = context
    plan, allowed = approved(plan, arguments, compile_increment=3, research_increment=2)
    proposal = plan.increment.model_copy(update={"status": status})
    plan = plan.model_copy(update={"increment": proposal})
    arguments = {**arguments, "trusted_owner_authorization_sha256s":
        frozenset({owner_increment_sha256(proposal)}) if trusted else frozenset()}
    result = inspect_continuation(plan, **arguments)
    assert result.authorization_recognized is expected
    assert result.available_attempts.model_dump() == ({"compile": 3, "research_check": 2} if expected else {"compile": 0, "research_check": 0})
    assert not result.dispatch_allowed


def test_allowlisted_zero_delta_cannot_release_the_original_last_research(context):
    plan, arguments = approved(*context)
    result = inspect_continuation(plan, **arguments)
    assert result.authorization_recognized
    assert result.available_attempts.model_dump() == {"compile": 0, "research_check": 0}


def test_increment_is_cumulative_once_and_new_synthetic_attempts_are_deducted(tmp_path, context):
    plan, arguments = approved(*context, compile_increment=2, research_increment=2)
    anchor = make_ledger(tmp_path, "later", source="d")
    plan = plan.model_copy(update={"additional_synthetic_ledgers": [anchor]})
    arguments = {**arguments, "required_synthetic_ledgers": [anchor]}
    before = inventory(tmp_path)
    result = check_continuation_budget(plan, "research_check", **arguments)
    assert result.cumulative_caps.model_dump() == {"compile": 4, "research_check": 5}
    assert result.used_attempts.model_dump() == {"compile": 3, "research_check": 3}
    assert result.remaining_attempts.model_dump() == {"compile": 1, "research_check": 2}
    assert result.available_attempts.model_dump() == {"compile": 1, "research_check": 1}
    assert result.billing_unknown_attempt_count == 6 and result.actual_billing_total_microusd is None
    assert result.additional_synthetic_campaigns[0].attempts[0].source_commit == "d"*40
    assert inspect_continuation(plan, **arguments) == result
    assert inventory(tmp_path) == before


def test_added_attempts_without_increment_do_not_reset_budget_or_consume_reserved_slot_silently(tmp_path, context):
    plan, arguments = context
    anchor = make_ledger(tmp_path, "later", source="d")
    result = inspect_continuation(plan.model_copy(update={"additional_synthetic_ledgers": [anchor]}),
        **{**arguments, "required_synthetic_ledgers": [anchor]})
    assert result.used_attempts.model_dump() == {"compile": 3, "research_check": 3}
    assert not result.within_cumulative_caps and not result.research_reserve_intact
    assert result.available_attempts.model_dump() == {"compile": 0, "research_check": 0}


@pytest.mark.parametrize("location", ["plan", "context", "both_duplicate"])
def test_later_history_cannot_be_omitted_or_duplicated(tmp_path, context, location):
    plan, arguments = context
    anchor = make_ledger(tmp_path, "later", source="d")
    declared = [] if location == "plan" else [anchor]
    required = [] if location == "context" else [anchor]
    if location == "both_duplicate": declared = required = [anchor, anchor]
    with pytest.raises(ValueError, match="exactly once"):
        inspect_continuation(plan.model_copy(update={"additional_synthetic_ledgers": declared}),
            **{**arguments, "required_synthetic_ledgers": required})


def test_original_anchor_cannot_be_added_as_new_history(context):
    plan, arguments = context
    anchor = plan.historical_campaign.caps.expected_ledgers[0]
    with pytest.raises(ValueError, match="duplicated"):
        inspect_continuation(plan.model_copy(update={"additional_synthetic_ledgers": [anchor]}),
            **{**arguments, "required_synthetic_ledgers": [anchor]})


@pytest.mark.parametrize("state,unknown,active", [("UNKNOWN", 1, 1), ("reserved", 1, 1), ("accepted", 0, 1)])
def test_pending_added_attempt_blocks_every_budget_check_and_keeps_unknown_bills(tmp_path, context, state, unknown, active):
    plan, arguments = approved(*context, compile_increment=1, research_increment=1)
    anchor = make_ledger(tmp_path, "pending", source="d", operations=("research_check",), state=state)
    plan = plan.model_copy(update={"additional_synthetic_ledgers": [anchor]})
    before = inventory(tmp_path)
    with pytest.raises(ReadinessError, match="unresolved/active") as caught:
        check_continuation_budget(plan, "compile", **{**arguments, "required_synthetic_ledgers": [anchor]})
    assert caught.value.unknown_attempts == unknown and caught.value.active_attempts == active
    assert caught.value.unknown_billing_attempts == 5
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("response_class,source", [("CONTROL_ONLY", "d"), ("SYNTHETIC_TEST", "e")])
def test_additional_real_or_wrong_source_ledgers_are_not_supported(tmp_path, context, response_class, source):
    plan, arguments = context
    anchor = make_ledger(tmp_path, "wrong", source=source, response_class=response_class)
    with pytest.raises(ValueError):
        inspect_continuation(plan.model_copy(update={"additional_synthetic_ledgers": [anchor]}),
            **{**arguments, "required_synthetic_ledgers": [anchor]})


def test_empty_ledger_cannot_become_new_campaign(tmp_path, context):
    plan, arguments = context
    anchor = make_ledger(tmp_path, "empty", source="d", empty=True)
    with pytest.raises(ReadinessError, match="empty"):
        inspect_continuation(plan.model_copy(update={"additional_synthetic_ledgers": [anchor]}),
            **{**arguments, "required_synthetic_ledgers": [anchor]})


@pytest.mark.parametrize("mutation", ["database", "metadata", "unreferenced_metadata"])
def test_old_summary_cannot_authorize_changed_historical_inputs(tmp_path, context, mutation):
    plan, arguments = context
    anchor = plan.historical_campaign.caps.expected_ledgers[0]
    if mutation == "database":
        with closing(sqlite3.connect(tmp_path/anchor.database)) as connection:
            connection.execute("UPDATE ledger_meta SET value='2.0' WHERE key='schema_version'")
            connection.commit()
    else:
        directory = tmp_path/anchor.metadata_directory
        path = next(directory.iterdir()) if mutation == "metadata" else directory/"extra.json"
        path.write_bytes(path.read_bytes()+b" " if path.exists() else b"{}")
    with pytest.raises(ReadinessError):
        inspect_continuation(plan, **arguments)


def test_metadata_aba_during_real_parser_cannot_replace_captured_payload(tmp_path, context, monkeypatch):
    import bbag.s2_readiness as readiness
    plan, arguments = context
    directory = tmp_path/plan.historical_campaign.caps.expected_ledgers[0].metadata_directory
    path = next(directory.iterdir())
    original_bytes = path.read_bytes()
    original_parse = readiness.parse_json_object
    triggered = []
    def schedule(payload):
        if payload == original_bytes and not triggered:
            changed = json.loads(payload)
            changed["check_passed"] = False
            path.write_text(json.dumps(changed), encoding="utf-8")
            try:
                result = original_parse(payload)
                triggered.append(True)
            finally:
                path.write_bytes(original_bytes)
            return result
        return original_parse(payload)
    monkeypatch.setattr(readiness, "parse_json_object", schedule)
    result = inspect_continuation(plan, **arguments)
    assert triggered == [True]
    assert all(attempt.check_passed is True for attempt in result.historical_campaign.attempts)
    assert path.read_bytes() == original_bytes


def test_fresh_inspections_are_mandatory_not_cached_summary(context, monkeypatch):
    plan, arguments = context
    real_inspect = continuation.inspect_campaign
    calls = []
    def spy(caps, **kwargs):
        calls.append(caps)
        return real_inspect(caps, **kwargs)
    monkeypatch.setattr(continuation, "inspect_campaign", spy)
    for _ in range(2): inspect_continuation(plan, **arguments)
    assert len(calls) == 4


@pytest.mark.parametrize("field,value", [("source_commit", "e"*40), ("config_sha256", "e"*64),
    ("baseline_sha256", "e"*64)])
def test_allowlisted_increment_must_bind_exact_baseline_source_and_config(context, field, value):
    plan, arguments = approved(*context, compile_increment=1)
    proposal = plan.increment.model_copy(update={field: value})
    plan = plan.model_copy(update={"increment": proposal})
    with pytest.raises(ValueError, match="bind"):
        inspect_continuation(plan, **{**arguments, "trusted_owner_authorization_sha256s": frozenset({owner_increment_sha256(proposal)})})


@pytest.mark.parametrize("field,value", [("compile_increment", True), ("compile_increment", -1),
    ("research_increment", 33), ("research_increment", "1"), ("max_new_cost_microusd", False),
    ("max_new_cost_microusd", 1), ("scope", "NEW_DATA"), ("status", "AUTHORIZED")])
def test_copy_construct_cannot_bypass_increment_contracts_before_io(context, monkeypatch, field, value):
    plan, arguments = approved(*context)
    proposal = plan.increment.model_copy(update={field: value})
    plan = plan.model_copy(update={"increment": proposal})
    monkeypatch.setattr(continuation, "inspect_campaign", lambda *_a, **_k: pytest.fail("invalid input reached I/O"))
    with pytest.raises(ValidationError):
        inspect_continuation(plan, **arguments)


@pytest.mark.parametrize("field,value", [("preserve_research_attempts", 0), ("preserve_research_attempts", True),
    ("source_commit", "bad"), ("additional_synthetic_ledgers", [] )])
def test_plan_mutations_and_forged_history_fail_revalidation(context, monkeypatch, field, value):
    plan, arguments = context
    if field == "additional_synthetic_ledgers":
        history = plan.historical_campaign.model_copy(update={"attempts": []})
        plan = plan.model_copy(update={"historical_campaign": history})
    else:
        plan = plan.model_copy(update={field: value})
    monkeypatch.setattr(continuation, "inspect_campaign", lambda *_a, **_k: pytest.fail("invalid input reached I/O"))
    with pytest.raises(ValueError): inspect_continuation(plan, **arguments)


@pytest.mark.parametrize("argument,value", [("expected_baseline_sha256", "0"*64),
    ("expected_baseline_sha256", True), ("trusted_owner_authorization_sha256s", set()),
    ("trusted_owner_authorization_sha256s", frozenset({"bad"})), ("required_synthetic_ledgers", ""),
    ("root", "somewhere")])
def test_external_context_requires_explicit_exact_trust_inputs(context, monkeypatch, argument, value):
    plan, arguments = context
    monkeypatch.setattr(continuation, "inspect_campaign", lambda *_a, **_k: pytest.fail("invalid trust reached I/O"))
    with pytest.raises(ValueError): inspect_continuation(plan, **{**arguments, argument: value})


def test_typed_public_boundaries_and_extra_fields_are_rejected(context):
    plan, arguments = context
    with pytest.raises(ValueError): inspect_continuation({}, **arguments)
    with pytest.raises(ValueError): campaign_baseline_sha256({})
    with pytest.raises(ValueError): owner_increment_sha256({})
    with pytest.raises(ValueError): check_continuation_budget(plan, "backtest", **arguments)
    with pytest.raises(ValidationError): ContinuationPlan.model_validate({**plan.model_dump(mode="python"), "grant": True})
    body = inspect_continuation(plan, **arguments).model_dump(mode="python")
    body["dispatch_allowed"] = 0
    with pytest.raises(ValidationError): ContinuationPreview.model_validate(body)


@pytest.mark.parametrize("field,value", [("preserved_research_attempts", True),
    ("max_new_cost_microusd", False), ("available_attempts", {"compile": 1, "research_check": 0}),
    ("baseline_sha256", "0"*64)])
def test_serialized_preview_cannot_launder_budget_or_control_types(context, field, value):
    plan, arguments = context
    body = inspect_continuation(plan, **arguments).model_dump(mode="python")
    body[field] = value
    with pytest.raises(ValidationError): ContinuationPreview.model_validate(body)


def test_model_construct_is_revalidated_not_only_model_copy(context, monkeypatch):
    plan, arguments = context
    forged = OwnerBudgetIncrement.model_construct(status="OWNER_APPROVED", baseline_sha256=arguments["expected_baseline_sha256"],
        source_commit=plan.source_commit, config_sha256=plan.config_sha256, approval_reference="SYNTHETIC_FORGERY",
        compile_increment=-1)
    plan = plan.model_copy(update={"increment": forged})
    monkeypatch.setattr(continuation, "inspect_campaign", lambda *_a, **_k: pytest.fail("forged input reached I/O"))
    with pytest.raises(ValidationError): inspect_continuation(plan, **arguments)


@pytest.mark.parametrize("nested", [False, True])
def test_copy_injected_extra_fields_are_rejected_not_silently_dumped(context, monkeypatch, nested):
    plan, arguments = approved(*context)
    if nested:
        plan = plan.model_copy(update={"increment": plan.increment.model_copy(update={"grant": True})})
    else:
        plan = plan.model_copy(update={"grant": True})
    monkeypatch.setattr(continuation, "inspect_campaign", lambda *_a, **_k: pytest.fail("extra field reached I/O"))
    with pytest.raises(ValidationError): inspect_continuation(plan, **arguments)


def test_all_later_occupancy_and_original_unknown_bills_are_aggregated(tmp_path, context):
    plan, arguments = approved(*context, compile_increment=2, research_increment=2)
    anchors = [make_ledger(tmp_path, "unknown", source="d", operations=("research_check",), state="UNKNOWN"),
               make_ledger(tmp_path, "active", source="d", operations=("compile",), state="accepted")]
    with pytest.raises(ReadinessError) as caught:
        inspect_continuation(plan.model_copy(update={"additional_synthetic_ledgers": anchors}),
            **{**arguments, "required_synthetic_ledgers": anchors})
    assert caught.value.unknown_attempts == 1 and caught.value.active_attempts == 2
    assert caught.value.unknown_billing_attempts == 6
