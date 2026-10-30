"""Offline S1 commands; future labels have their own explicit entry point."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .expression import audit_expression
from .provenance import git_provenance, json_bytes, safe_run_directory, sha256_bytes
from .s1_contracts import AuditSummary, FactorResult, LabelResult, S1Config, S1RunManifest, canonical_s1_bytes, load_s1_config


def audit_config(config: S1Config) -> AuditSummary:
    config = S1Config.model_validate(config.model_dump(mode="python"))
    limits = config.audit_limits
    result = audit_expression(
        config.factor.formula, config.panel.fields,
        max_nodes=limits.max_nodes, max_depth=limits.max_depth,
        max_parameters=limits.max_parameters, max_window=limits.max_window,
        max_lookback_sessions=limits.max_lookback_sessions,
        declared_fields=config.factor.required_fields,
        declared_lookback_sessions=config.factor.lookback_sessions,
    )
    return AuditSummary(
        normalized_expression=result.normalized_expression, fields=list(result.fields),
        lookback_sessions=result.lookback_sessions, node_count=result.node_count,
        max_depth=result.max_depth, parameter_count=result.parameter_count,
    )


def audit_command(path: Path) -> dict[str, Any]:
    config = load_s1_config(path)
    audit = audit_config(config)
    return {"valid": True, "stage": "S1", "purpose": "ENGINEERING_TEST", "status": "DRAFT",
            "research_ready": False, "config_sha256": sha256_bytes(canonical_s1_bytes(config)),
            "audit": audit.model_dump(mode="json"), "pending_decisions": config.pending_decisions}


def write_run(config: S1Config, result: FactorResult | LabelResult, output_dir: Path, run_id: str,
              kind: Literal["factors_batch", "factors_incremental", "labels"]) -> dict[str, Any]:
    if not isinstance(result, (FactorResult, LabelResult)):
        raise ValueError("S1 output must be a validated factor or label result")
    result = type(result).model_validate(result.model_dump(mode="python"))
    content = canonical_s1_bytes(config)
    result_bytes = json_bytes(result.model_dump(mode="json"))
    source = git_provenance()
    manifest = S1RunManifest(
        run_id=run_id, kind=kind, config_sha256=sha256_bytes(content),
        result_sha256=sha256_bytes(result_bytes), code_commit=source["commit"],
        working_tree_dirty=source["dirty"], created_at=datetime.now(timezone.utc),
    )
    target = safe_run_directory(output_dir, run_id)
    target.mkdir(parents=True, exist_ok=False)
    (target / "config.json").write_bytes(content)
    (target / "result.json").write_bytes(result_bytes)
    (target / "run.json").write_bytes(json_bytes(manifest.model_dump(mode="json")))
    return {"stage": "S1", "purpose": "ENGINEERING_TEST", "status": "DRAFT",
            "run_path": str(target), "rows": len(result.rows), "record": manifest.model_dump(mode="json")}


def factor_command(path: Path, output_dir: Path, run_id: str, mode: Literal["batch", "incremental"]) -> dict[str, Any]:
    from .factors import compute_factors
    config = load_s1_config(path)
    audit_config(config)
    result = compute_factors(config.panel, config.factor, config.audit_limits,
                             config.universe, config.as_of, mode=mode)
    kind = "factors_batch" if mode == "batch" else "factors_incremental"
    return write_run(config, result, output_dir, run_id, kind)


def label_command(path: Path, output_dir: Path, run_id: str) -> dict[str, Any]:
    from .labels import compute_labels
    config = load_s1_config(path)
    result = compute_labels(config.panel, config.label, config.universe, config.as_of)
    return write_run(config, result, output_dir, run_id, "labels")
