"""Reinspect the original campaign and emit a zero-increment offline preview."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from s2_plan_source import check_output, check_source


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--baseline-sha256", required=True,
        help="Externally retained canonical CampaignReadiness digest; not identity authentication")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    check_source(root, args.commit)
    from bbag.jsonio import parse_json_object
    from bbag.s2_continuation import ContinuationPlan, campaign_baseline_sha256, inspect_continuation
    from bbag.s2_contracts import S2Config, canonical_s2_config
    from bbag.s2_readiness import CampaignReadiness
    if git(root, "rev-parse", "HEAD").decode("ascii").strip() != args.commit or git(root, "status", "--porcelain").strip():
        raise ValueError("exact clean committed source required")
    config_path = root / "configs/s2.cloud-scope.json"
    config_raw = config_path.read_bytes()
    if config_raw != git(root, "show", f"{args.commit}:configs/s2.cloud-scope.json"):
        raise ValueError("cloud config disk bytes differ from exact committed config")
    parse_json_object(config_raw)
    config = S2Config.model_validate_json(config_raw)
    # Parse the captured bytes, never hash A and reopen B for parsing.
    baseline_raw = args.baseline.read_bytes()
    parse_json_object(baseline_raw)
    baseline = CampaignReadiness.model_validate_json(baseline_raw)
    if campaign_baseline_sha256(baseline) != args.baseline_sha256:
        raise ValueError("baseline differs from retained external context anchor")
    if baseline.caps.response_class != "CONTROL_ONLY" or baseline.caps.project_id != 38698074:
        raise ValueError("this handoff command accepts the original real campaign only")
    plan = ContinuationPlan(source_commit=args.commit,
        config_sha256=hashlib.sha256(canonical_s2_config(config)).hexdigest(),
        historical_campaign=baseline, additional_synthetic_ledgers=[], increment=None)
    preview = inspect_continuation(plan, expected_baseline_sha256=args.baseline_sha256,
        required_synthetic_ledgers=[], root=root)
    if (git(root, "rev-parse", "HEAD").decode("ascii").strip() != args.commit
            or git(root, "status", "--porcelain").strip() or config_path.read_bytes() != config_raw):
        raise ValueError("source/config changed during continuation preview")
    check_source(root, args.commit)
    protected = [args.baseline.resolve().parent, root / "artifacts/s2-bootstrap"]
    for anchor in baseline.caps.expected_ledgers:
        db = Path(anchor.database)
        metadata = Path(anchor.metadata_directory)
        protected.append((db if db.is_absolute() else root / db).parent)
        protected.append(metadata if metadata.is_absolute() else root / metadata)
    output = check_output(root, args.output_dir, protected=tuple(protected))
    output.mkdir(parents=True, exist_ok=False)
    (output / "CONTINUATION_PLAN.json").write_text(plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (output / "CONTINUATION_PREVIEW.json").write_text(preview.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"source_commit": args.commit, "baseline_sha256": args.baseline_sha256,
        "used_attempts": preview.used_attempts.model_dump(), "available_attempts": preview.available_attempts.model_dump(),
        "dispatch_allowed": False, "new_real_task_reserved": False,
        "platform_execution": "NOT_RUN", "output_dir": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc), "platform_execution": "NOT_RUN"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
