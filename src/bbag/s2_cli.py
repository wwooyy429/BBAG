"""S2 local synthetic workflow and explicit cloud control plans, without remote IO."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .provenance import git_provenance, json_bytes, safe_run_directory, sha256_bytes
from .s2_access import AccessGuard, canonical_policy_sha256
from .s2_contracts import S2RunManifest, S2SyntheticResult, canonical_s2_config, load_s2_config
from .s2_data import canonical_request_sha256, data_quality, historical_universe, to_s1_panel
from .s2_quantconnect import QuantConnectCheckRequest, canonical_qc_request
from .s2_tasks import TaskLedger, TaskResponse, TaskSpec


def validate_command(path: Path) -> dict:
    config = load_s2_config(path)
    # This endpoint neither enrolls a runtime grant nor invokes a transport.
    if config.request.provider == "SYNTHETIC":
        AccessGuard(config.policy).authorize(config.request)
    return {"valid": True, "stage": "S2", "purpose": "ENGINEERING_TEST", "status": "DRAFT",
            "config_sha256": sha256_bytes(canonical_s2_config(config)),
            "policy_sha256": canonical_policy_sha256(config.policy), "research_ready": False,
            "runtime_grant_enrolled": False, "platform_execution": "NOT_RUN",
            "pending_decisions": config.pending_decisions}


def plan_command(path: Path, project_id: int) -> dict:
    config = load_s2_config(path)
    source = git_provenance()
    if config.request.provider != "QUANTCONNECT_CLOUD" or source["commit"] is None:
        raise ValueError("a cloud control config and real Git source are required to create a plan")
    plan = QuantConnectCheckRequest(project_id=project_id, data=config.request,
        source_commit=source["commit"], config_sha256=sha256_bytes(canonical_s2_config(config)),
        checks=["data_presence", "requested_bounds", "field_availability", "normalization_raw",
                "point_in_time", "identity_consistency", "no_data_export"])
    return {"plan": plan.model_dump(mode="json"), "request_sha256": sha256_bytes(canonical_qc_request(plan)),
            "runtime_grant_enrolled": False, "platform_execution": "NOT_RUN"}


def synthetic_command(path: Path, output_dir: Path, run_id: str) -> dict:
    config = load_s2_config(path)
    if config.request.provider != "SYNTHETIC" or config.bundle is None:
        raise ValueError("this command accepts synthetic fixtures only; cloud data stays in cloud")
    guard = AccessGuard(config.policy)
    guard.authorize(config.request)
    content = canonical_s2_config(config)
    config_hash = sha256_bytes(content)
    source = git_provenance()
    if source["commit"] is None:
        raise ValueError("the task provenance demonstration requires a real Git checkout")
    universe = historical_universe(config.bundle, config.request, guard, liquidity=config.liquidity)
    quality = data_quality(config.bundle, config.request, guard, rule=config.quality)
    panel = to_s1_panel(config.bundle, config.request, guard)
    # All access and numerical validation precedes output creation.
    target = safe_run_directory(output_dir, run_id)
    spec = TaskSpec(correlation_id="synthetic-project-metadata", operation="project_read",
        source_commit=source["commit"], config_sha256=config_hash,
        request_sha256=canonical_request_sha256(config.request), response_class="SYNTHETIC_TEST")
    target.mkdir(parents=True, exist_ok=False)
    ledger = TaskLedger(target / "task_ledger.sqlite", target / "metadata", config.task_budget)
    def fixture_callback(selected, attempt):
        return TaskResponse(operation=selected.operation, source_commit=selected.source_commit,
            config_sha256=selected.config_sha256, request_sha256=selected.request_sha256,
            response_class="SYNTHETIC_TEST", status="completed", success=True,
            check_passed=True, actual_cost_microusd=0)
    task = ledger.submit(spec, fixture_callback)
    # Reopen the actual SQLite record; no simulated provider task ID is invented.
    reopened = TaskLedger(target / "task_ledger.sqlite", target / "metadata", config.task_budget)
    reopened.verify_integrity()
    result = S2SyntheticResult(config_sha256=config_hash, request_sha256=spec.request_sha256,
        universe=universe, quality=quality, projected_s1_panel_sha256=sha256_bytes(json_bytes(panel.model_dump(mode="json"))),
        task=reopened.get(spec.correlation_id), usage=reopened.usage())
    raw_result = json_bytes(result.model_dump(mode="json"))
    record = S2RunManifest(run_id=run_id, config_sha256=config_hash, result_sha256=sha256_bytes(raw_result),
        code_commit=source["commit"], working_tree_dirty=source["dirty"], created_at=datetime.now(timezone.utc))
    (target / "config.json").write_bytes(content)
    (target / "result.json").write_bytes(raw_result)
    (target / "run.json").write_bytes(json_bytes(record.model_dump(mode="json")))
    return {"stage": "S2", "run_path": str(target), "platform_execution": "NOT_RUN",
            "fixture_class": "SYNTHETIC_TEST", "record": record.model_dump(mode="json")}
