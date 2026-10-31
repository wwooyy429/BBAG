"""S2 local synthetic workflow and explicit cloud control plans, without remote IO."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import io
import stat
import subprocess

from .provenance import git_provenance, json_bytes, safe_run_directory, sha256_bytes
from .s2_access import AccessGuard, DataRequest, canonical_policy_sha256
from .s2_contracts import S2RunManifest, S2SyntheticResult, canonical_s2_config, load_s2_config
from .s2_data import canonical_request_sha256, data_quality, historical_universe, to_s1_panel
from .s2_quantconnect import QuantConnectCheckRequest, canonical_qc_request
from .s2_tasks import TaskLedger, TaskResponse, TaskSpec


def _check_offline_source(root: Path, commit: str) -> int:
    """Bind files actually used by this persistent-cache demo to Git bytes.

    git status alone cannot reveal assume-unchanged/skip-worktree mutations.
    This is a source consistency check, not a sandbox or package authentication.
    """
    required = {"requirements.lock", "pyproject.toml", "configs/s2.example.json", "tests/fixtures/s2_raw.json"}
    required.update("src/bbag/" + name + ".py" for name in (
        "__init__", "__main__", "cli", "contracts", "jsonio", "provenance", "s1_contracts",
        "s2_access", "s2_cli", "s2_contracts", "s2_data", "s2_diagnostics",
        "s2_quantconnect", "s2_readiness", "s2_snapshots", "s2_tasks"))
    try:
        actual_root = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=root, stderr=subprocess.PIPE)
        if Path(actual_root.decode("utf-8").strip()).resolve() != root.resolve():
            raise ValueError("offline source must be the real Git checkout root")
        listing = subprocess.check_output(["git", "ls-tree", "-rz", "--full-tree", commit, "--",
            "src/bbag", "requirements.lock", "pyproject.toml", "configs/s2.example.json", "tests/fixtures/s2_raw.json"],
            cwd=root, stderr=subprocess.PIPE)
        entries = {}
        for record in listing.split(b"\0"):
            if not record:
                continue
            meta, name = record.split(b"\t", 1)
            mode, kind, oid = meta.split()
            if kind != b"blob" or mode not in {b"100644", b"100755"}:
                raise ValueError("offline source requires regular committed files")
            entries[name.decode("utf-8")] = oid
        if not required <= set(entries):
            raise ValueError("offline source inputs must all belong to the exact commit")
        objects = subprocess.check_output(["git", "cat-file", "--batch"], cwd=root,
            input=b"\n".join(entries.values()) + b"\n", stderr=subprocess.PIPE)
        stream = io.BytesIO(objects)
        for name, oid in entries.items():
            path = root / name
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise ValueError("offline source files must be ordinary files")
            header = stream.readline().split()
            if len(header) != 3 or header[:2] != [oid, b"blob"]:
                raise ValueError("offline source Git object identity differs")
            expected = stream.read(int(header[2]))
            if stream.read(1) != b"\n" or path.read_bytes() != expected:
                raise ValueError("offline source or fixed inputs differ from the exact committed bytes")
        if stream.read():
            raise ValueError("offline source Git object stream has extra bytes")
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ValueError("offline source cannot be verified against its exact Git commit") from exc
    return len(entries)


def campaign_command(path: Path, history_root: Path | None = None) -> dict:
    """Inspect exact historical inputs; never create or enroll a new ledger."""
    from .s2_readiness import CampaignCaps, inspect_campaign
    from .jsonio import read_json_object
    import json

    caps = CampaignCaps.model_validate_json(json.dumps(read_json_object(path), allow_nan=False))
    base = history_root if history_root is not None else Path(__file__).resolve().parents[2]
    return inspect_campaign(caps, root=base).model_dump(mode="json")


def offline_command(path: Path, cache_dir: Path) -> dict:
    """Execute the fixed synthetic data workflow without a real task reservation."""
    from datetime import date
    import json
    from .s2_data import DailyBar, RawDailyInput, standardize_daily_json
    from .s2_diagnostics import action_entitlement_chain, identity_audit, validate_quality_consistency
    from .s2_snapshots import GuardedSyntheticDataService, ProviderSnapshot, SyntheticSnapshotStore, plan_batches

    config = load_s2_config(path)
    if config.request.provider != "SYNTHETIC" or config.bundle is None:
        raise ValueError("offline-check accepts the fixed synthetic example only; Cloud data stays in Cloud")
    guard = AccessGuard(config.policy)
    guard.authorize(config.request)
    root = Path(__file__).resolve().parents[2]
    if canonical_s2_config(config) != canonical_s2_config(load_s2_config(root / "configs/s2.example.json")):
        raise ValueError("offline-check requires the committed fixed synthetic example")
    source = git_provenance()
    if source["commit"] is None or source["dirty"]:
        raise ValueError("offline-check requires an exact clean committed source for the cache code identity")
    bound_source_files = _check_offline_source(root, source["commit"])
    raw_path = root / "tests/fixtures/s2_raw.json"
    raw_bytes = raw_path.read_bytes()
    if sha256_bytes(raw_bytes) != config.bundle.provenance.source_sha256:
        raise ValueError("fixed synthetic original bytes differ from their declared provenance")
    def unique_fixture(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("fixed synthetic fixture contains duplicate JSON keys")
            result[key] = value
        return result
    parsed = json.loads(raw_bytes, object_pairs_hook=unique_fixture)
    raw = RawDailyInput.model_validate_json(json.dumps(parsed, allow_nan=False)).model_dump(mode="json")
    reference_bundle = standardize_daily_json(raw_bytes, request=config.request, guard=guard,
        data_id=config.bundle.data_id,
        provider_data_version=config.bundle.provenance.provider_data_version,
        retrieved_at=config.bundle.provenance.retrieved_at,
        calendar_id=config.bundle.calendar_id, calendar_version=config.bundle.calendar_version,
        timezone=config.bundle.timezone)
    if reference_bundle != config.bundle:
        raise ValueError("fixed synthetic bundle metadata and fields differ from the original raw fixture")
    store = SyntheticSnapshotStore(cache_dir)
    service = GuardedSyntheticDataService(store, guard, code_commit=source["commit"])
    plan = plan_batches(config.request, guard, max_calendar_days=2, assets_per_batch=1)
    count = 0

    def fixture_fetch(request: DataRequest) -> ProviderSnapshot:
        nonlocal count
        guard.authorize(request)
        count += 1
        selected = {name: [item for item in raw[name] if item["sid"] in request.assets]
                    for name in ("identities", "tickers", "membership", "corporate_actions")}
        selected["sessions"] = [item for item in raw["sessions"]
                                if request.start <= date.fromisoformat(item["session"]) <= request.end]
        selected["bars"] = [item for item in raw["bars"] if item["sid"] in request.assets
                            and request.start <= date.fromisoformat(item["session"]) <= request.end]
        return ProviderSnapshot(raw_source=json_bytes(selected),
            retrieved_at=config.bundle.provenance.retrieved_at,
            data_id=config.bundle.data_id,
            provider_data_version=config.bundle.provenance.provider_data_version,
            calendar_id=config.bundle.calendar_id, calendar_version=config.bundle.calendar_version,
            classification=request.classification, derived_from=tuple(request.derived_from))

    versions = dict(data_id=config.bundle.data_id,
        provider_data_version=config.bundle.provenance.provider_data_version,
        calendar_id=config.bundle.calendar_id, calendar_version=config.bundle.calendar_version)
    first = service.get_batches(plan, fetch=fixture_fetch, **versions)
    first_fetches = count
    repeated = service.get_batches(plan, fetch=fixture_fetch, **versions)
    if count != first_fetches or not all(item.cache_hit for item in repeated.snapshots):
        raise ValueError("fixed synthetic cache repetition unexpectedly fetched data")
    observed = [bar for item in first.snapshots for bar in item.bundle.bars]
    expected = [DailyBar.model_validate_json(json.dumps(item)) for item in raw["bars"]]
    order = lambda bar: (bar.session, bar.sid)
    if sorted(observed, key=order) != sorted(expected, key=order):
        raise ValueError("batched synthetic raw fields differ from the original literal fixture")
    quality = validate_quality_consistency(data_quality(config.bundle, config.request, guard, rule=config.quality))
    identities = identity_audit(config.bundle, config.request, guard)
    actions = [action_entitlement_chain(config.bundle, config.request, guard,
        sid=sid, shares_before=10.0).model_dump(mode="json") for sid in sorted(config.request.assets)]
    source_end = git_provenance()
    if source_end["commit"] != source["commit"] or source_end["dirty"]:
        raise ValueError("source changed during the fixed synthetic snapshot check")
    _check_offline_source(root, source["commit"])
    return {"stage": "S2", "purpose": "ENGINEERING_TEST", "status": "DRAFT",
        "code_commit": source["commit"], "working_tree_dirty": False,
        "bound_source_files": bound_source_files,
        "fixture_class": "SYNTHETIC_TEST", "platform_execution": "NOT_RUN",
        "economic_evidence": "NOT_APPLICABLE", "independent_review": "PENDING",
        "new_real_platform_calls": 0, "runtime_grant_enrolled": False,
        "new_real_task_reserved": False, "batch_count": len(plan.requests),
        "synthetic_fetch_count": first_fetches, "repeat_synthetic_fetch_count": count - first_fetches,
        "first_cache_hits": sum(item.cache_hit for item in first.snapshots),
        "repeat_cache_hits": sum(item.cache_hit for item in repeated.snapshots),
        "checks": {"complete_batch_raw_fields_preserved": True, "repeat_cache_no_fetch": True,
                   "quality_self_consistent": True},
        "identity_audit": identities.model_dump(mode="json"),
        "quality": quality.model_dump(mode="json"),
        "action_chains": actions}


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
