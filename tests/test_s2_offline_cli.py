"""Synthetic CLI integration; no platform callbacks, source identities are fixtures."""
import json
import hashlib
from pathlib import Path

import pytest

from bbag import s2_cli
from bbag.provenance import json_bytes
from bbag.s2_tasks import TaskBudget, TaskLedger, TaskResponse, TaskSpec


ROOT = Path(__file__).resolve().parents[1]
SOURCE = "b" * 40


def inventory(directory):
    return {p.relative_to(directory).as_posix(): p.read_bytes()
            for p in directory.rglob("*") if p.is_file()}


@pytest.fixture
def clean_declared_source(monkeypatch):
    monkeypatch.setattr(s2_cli, "git_provenance", lambda: {"commit": SOURCE, "dirty": False})
    # Unit source tokens are deliberate fixtures, not real Git attestations.
    monkeypatch.setattr(s2_cli, "_check_offline_source", lambda root, commit: 0, raising=False)


def test_fixed_offline_workflow_preserves_literal_values_and_reuses_every_batch(tmp_path, clean_declared_source):
    cache = tmp_path / "cache"
    first = s2_cli.offline_command(ROOT / "configs/s2.example.json", cache)
    assert first["batch_count"] == first["synthetic_fetch_count"] == first["repeat_cache_hits"] == 4
    assert first["first_cache_hits"] == first["repeat_synthetic_fetch_count"] == 0
    assert all(first["checks"].values())
    assert first["code_commit"] == SOURCE and first["working_tree_dirty"] is False
    assert first["platform_execution"] == "NOT_RUN" and first["independent_review"] == "PENDING"
    assert first["new_real_platform_calls"] == 0 and not first["new_real_task_reserved"]
    assert not first["runtime_grant_enrolled"] and first["fixture_class"] == "SYNTHETIC_TEST"
    assert first["quality"]["expected_bars"] == first["quality"]["complete_bars"] == 8
    assert len(first["identity_audit"]["rows"]) == 8
    actions = {item["sid"]: item for item in first["action_chains"]}
    assert actions["SYN_A"]["shares_after"] == 20.0 and actions["SYN_A"]["cash_distribution"] == 0.0
    assert actions["SYN_B"]["shares_after"] == 10.0 and actions["SYN_B"]["cash_distribution"] == 10.0
    before = inventory(cache)
    second = s2_cli.offline_command(ROOT / "configs/s2.example.json", cache)
    assert second["synthetic_fetch_count"] == second["repeat_synthetic_fetch_count"] == 0
    assert second["first_cache_hits"] == second["repeat_cache_hits"] == 4
    assert inventory(cache) == before


def test_changed_code_identity_creates_separate_synthetic_snapshots(tmp_path, clean_declared_source, monkeypatch):
    cache = tmp_path / "cache"
    s2_cli.offline_command(ROOT / "configs/s2.example.json", cache)
    before = inventory(cache)
    monkeypatch.setattr(s2_cli, "git_provenance", lambda: {"commit": "c" * 40, "dirty": False})
    output = s2_cli.offline_command(ROOT / "configs/s2.example.json", cache)
    assert output["synthetic_fetch_count"] == 4 and output["repeat_synthetic_fetch_count"] == 0
    after = inventory(cache)
    assert len(after) == 8 and all(after[name] == raw for name, raw in before.items())


@pytest.mark.parametrize("source", [{"commit": SOURCE, "dirty": True}, {"commit": None, "dirty": False}])
def test_uncommitted_source_rejects_before_cache_creation(tmp_path, monkeypatch, source):
    monkeypatch.setattr(s2_cli, "git_provenance", lambda: source)
    with pytest.raises(ValueError, match="clean committed source"):
        s2_cli.offline_command(ROOT / "configs/s2.example.json", tmp_path / "cache")
    assert not (tmp_path / "cache").exists()


def test_cloud_example_rejects_before_source_or_cache_access(tmp_path, monkeypatch):
    def unexpected():
        raise AssertionError("must reject before source work or any provider/cache access")
    monkeypatch.setattr(s2_cli, "git_provenance", unexpected)
    with pytest.raises(ValueError, match="Cloud data stays in Cloud"):
        s2_cli.offline_command(ROOT / "configs/s2.cloud-scope.json", tmp_path / "cache")
    assert not (tmp_path / "cache").exists()


def test_nonfixed_example_rejects_without_creating_cache(tmp_path, clean_declared_source):
    value = json.loads((ROOT / "configs/s2.example.json").read_bytes())
    value["config_id"] = "ANOTHER_SYNTHETIC_CONFIG_NOT_THE_FIXED_ORACLE"
    config = tmp_path / "changed.json"
    config.write_bytes(json_bytes(value))
    with pytest.raises(ValueError, match="committed fixed"):
        s2_cli.offline_command(config, tmp_path / "cache")
    assert not (tmp_path / "cache").exists()


def test_fixed_bundle_metadata_must_match_raw_before_cache_creation(tmp_path, clean_declared_source, monkeypatch):
    config = s2_cli.load_s2_config(ROOT / "configs/s2.example.json")
    raw = (ROOT / "tests/fixtures/s2_raw.json").read_bytes()
    assert config.bundle.provenance.source_sha256 == hashlib.sha256(raw).hexdigest()
    config.bundle.membership = [event for event in config.bundle.membership if event.sid != "SYN_A"]
    changed = type(config).model_validate(config.model_dump(mode="python"))
    # Both requested and fixed-reference loads use the same internally
    # inconsistent typed fixture. SOURCE is the existing explicit unit stub.
    monkeypatch.setattr(s2_cli, "load_s2_config", lambda _: type(changed).model_validate(changed.model_dump(mode="python")))
    cache = tmp_path / "cache"
    with pytest.raises(ValueError, match="raw|bundle|original"):
        s2_cli.offline_command(ROOT / "configs/s2.example.json", cache)
    assert not cache.exists()


def test_source_change_during_check_cannot_return_clean_success(tmp_path, monkeypatch):
    states = iter([{"commit": SOURCE, "dirty": False}, {"commit": SOURCE, "dirty": True}])
    monkeypatch.setattr(s2_cli, "git_provenance", lambda: next(states))
    monkeypatch.setattr(s2_cli, "_check_offline_source", lambda root, commit: 0, raising=False)
    with pytest.raises(ValueError, match="source changed"):
        s2_cli.offline_command(ROOT / "configs/s2.example.json", tmp_path / "cache")


def campaign_config(tmp_path):
    database, metadata = tmp_path / "history.sqlite", tmp_path / "metadata"
    ledger = TaskLedger(database, metadata, TaskBudget(max_attempts=5,
        operation_limits={"compile": 2, "research_check": 3}, response_class="SYNTHETIC_TEST"))
    for operation in ("compile", "research_check"):
        spec = TaskSpec(correlation_id="SYNTHETIC-" + operation, operation=operation, project_id=77,
            source_commit=SOURCE, config_sha256="a" * 64, request_sha256="d" * 64,
            response_class="SYNTHETIC_TEST")
        ledger.submit(spec, lambda selected, attempt: TaskResponse(operation=selected.operation,
            project_id=77, source_commit=selected.source_commit, config_sha256=selected.config_sha256,
            request_sha256=selected.request_sha256, response_class="SYNTHETIC_TEST",
            status="completed", platform_task_id="SYNTHETIC_" + selected.operation, success=True,
            actual_cost_microusd=None))
    value = dict(expected_ledgers=[dict(database=str(database), metadata_directory=str(metadata),
        expected_sha256=hashlib.sha256(database.read_bytes()).hexdigest())], compile_attempt_limit=2,
        research_attempt_limit=3, max_new_cost_microusd=0, project_id=77, response_class="SYNTHETIC_TEST",
        start="2020-01-02", end="2020-01-10", assets=["SPY", "AAPL"], approval_reference="SYNTHETIC UNIT INPUT, NOT AN OWNER GRANT")
    path = tmp_path / "campaign.json"
    path.write_bytes(json_bytes(value))
    return path, database, metadata


def test_campaign_cli_reads_actual_fixture_history_without_writing_or_granting(tmp_path, run_cli, cli_json):
    config, database, metadata = campaign_config(tmp_path)
    before = database.read_bytes(), inventory(metadata)
    process = run_cli("s2-campaign-plan", "--config", config, "--history-root", tmp_path)
    assert process.returncode == 0, process.stderr
    output = cli_json(process)
    assert output["used_attempts"] == {"compile": 1, "research_check": 1}
    assert output["remaining_attempts"] == {"compile": 1, "research_check": 2}
    assert output["billing_unknown_attempt_count"] == 2 and output["actual_billing_total_microusd"] is None
    assert output["phase"] == "IN_PROGRESS" and output["platform_integration"] == "NOT_RUN"
    assert not output["dispatch_allowed"] and not output["runtime_grant_enrolled"]
    assert not output["new_runtime_ledger_created"] and not output["new_task_reserved"]
    assert output["input_limits_not_authenticated"] is True
    assert before == (database.read_bytes(), inventory(metadata))


def test_missing_campaign_history_is_a_json_rejection_without_creating_a_database(tmp_path, run_cli, cli_json):
    config, database, metadata = campaign_config(tmp_path)
    value = json.loads(config.read_bytes())
    absent = tmp_path / "does-not-exist.sqlite"
    value["expected_ledgers"][0]["database"] = str(absent)
    config.write_bytes(json_bytes(value))
    before = database.read_bytes(), inventory(metadata)
    process = run_cli("s2-campaign-plan", "--config", config, "--history-root", tmp_path)
    assert process.returncode == 2 and cli_json(process)["valid"] is False
    assert not absent.exists() and before == (database.read_bytes(), inventory(metadata))


def test_campaign_duplicate_json_is_rejected_before_history_io(tmp_path, run_cli, cli_json):
    path = tmp_path / "duplicate.json"
    path.write_text('{"compile_attempt_limit":0,"compile_attempt_limit":2}', encoding="utf-8")
    process = run_cli("s2-campaign-plan", "--config", path)
    assert process.returncode == 2 and "duplicate JSON key" in cli_json(process)["error"]


def test_offline_cloud_cli_json_rejection_leaves_no_output(tmp_path, run_cli, cli_json):
    cache = tmp_path / "cache"
    process = run_cli("s2-offline-check", "--config", ROOT / "configs/s2.cloud-scope.json", "--cache-dir", cache)
    assert process.returncode == 2 and cli_json(process)["valid"] is False
    assert not cache.exists()
