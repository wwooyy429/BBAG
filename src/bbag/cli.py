"""S0-only configuration, schema, provenance and advisory review commands."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .contracts import CONTRACT_MODELS, ExperimentRecord, ReviewReceipt, canonical_protocol_bytes, load_protocol
from .jsonio import read_json_object
from .provenance import SHA_PATTERN, git_provenance, json_bytes, safe_run_directory, sha256_bytes


def emit(value: dict[str, Any], *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bbag", description="BBAG stage-zero offline engineering commands")
    commands = result.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate a DRAFT engineering protocol")
    validate.add_argument("--config", required=True, type=Path)
    initialize = commands.add_parser("init-experiment", help="Write an immutable S0 experiment record")
    initialize.add_argument("--config", required=True, type=Path)
    initialize.add_argument("--output-dir", required=True, type=Path)
    initialize.add_argument("--run-id", required=True)
    schemas = commands.add_parser("schemas", help="Export or check versioned JSON schemas")
    schemas.add_argument("--output-dir", required=True, type=Path)
    schemas.add_argument("--check", action="store_true")
    review = commands.add_parser("check-review", help="Check receipt consistency; never merge or advance a stage")
    review.add_argument("--receipt", required=True, type=Path)
    review.add_argument("--commit", required=True)
    return result


def validate_command(config: Path) -> int:
    protocol = load_protocol(config)
    emit({"valid": True, "stage": "S0", "purpose": "ENGINEERING_TEST", "research_ready": False,
          "protocol_sha256": sha256_bytes(canonical_protocol_bytes(protocol)),
          "pending_decisions": protocol.pending_decisions})
    return 0


def initialize_command(config: Path, output_dir: Path, run_id: str) -> int:
    protocol = load_protocol(config)
    content = canonical_protocol_bytes(protocol)
    source = git_provenance()
    record = ExperimentRecord(
        run_id=run_id, protocol_sha256=sha256_bytes(content), code_commit=source["commit"],
        working_tree_dirty=source["dirty"], created_at=datetime.now(timezone.utc),
    )
    target = safe_run_directory(output_dir, run_id)
    target.mkdir(parents=True, exist_ok=False)
    protocol_path = target / "protocol.json"
    experiment_path = target / "experiment.json"
    protocol_path.write_bytes(content)
    experiment_path.write_bytes(json_bytes(record.model_dump(mode="json")))
    emit({"experiment_path": str(experiment_path), "protocol_path": str(protocol_path),
          "record": record.model_dump(mode="json")})
    return 0


def schemas_command(output_dir: Path, check: bool) -> int:
    expected: dict[str, bytes] = {}
    for name, model in sorted(CONTRACT_MODELS.items()):
        expected[f"{name}.schema.json"] = json_bytes(model.model_json_schema()) + b"\n"
    if check:
        actual = {path.name for path in output_dir.glob("*.schema.json")}
        if actual != set(expected):
            raise ValueError("schema inventory differs from contract models")
        for name, content in expected.items():
            if (output_dir / name).read_bytes() != content:
                raise ValueError(f"schema is stale: {name}")
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        for name, content in expected.items():
            (output_dir / name).write_bytes(content)
    emit({"schemas": len(expected), "output_dir": str(output_dir), "checked": check})
    return 0


def review_command(receipt_path: Path, commit: str) -> int:
    if not SHA_PATTERN.fullmatch(commit):
        raise ValueError("commit must be a full lowercase 40-character Git SHA")
    base: dict[str, Any] = {
        "audited_commit_expected": commit, "advisory_only": True, "reviewer_authenticated": False,
        "merge_performed": False, "next_stage_authorized": False,
    }
    if not receipt_path.is_file():
        emit({**base, "receipt_structurally_passed": False, "reason": "review_receipt_missing"})
        return 3
    # Parse strictly before JSON-mode contract validation (which accepts ISO timestamps).
    receipt = ReviewReceipt.model_validate_json(json.dumps(read_json_object(receipt_path), allow_nan=False))
    reasons = []
    if receipt.audited_commit != commit:
        reasons.append("review_commit_mismatch")
    if receipt.decision != "APPROVED":
        reasons.append("independent_review_not_approved")
    if receipt.engineering_status != "PASS" or receipt.blocking_findings != 0:
        reasons.append("engineering_acceptance_incomplete")
    passed = not reasons
    emit({**base, "receipt_structurally_passed": passed, "reasons": reasons,
          "decision": receipt.decision, "reviewer_identity_requires_external_confirmation": True})
    return 0 if passed else 3


def main(argv: list[str] | None = None) -> int:
    # JSON output is UTF-8 even when Windows redirects a legacy-codepage console.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        if args.command == "validate":
            return validate_command(args.config)
        if args.command == "init-experiment":
            return initialize_command(args.config, args.output_dir, args.run_id)
        if args.command == "schemas":
            return schemas_command(args.output_dir, args.check)
        if args.command == "check-review":
            return review_command(args.receipt, args.commit)
        raise ValueError("unsupported S0 command")
    except ValidationError as exc:
        errors = [{"path": list(item["loc"]), "message": item["msg"], "type": item["type"]}
                  for item in exc.errors(include_input=False, include_url=False)]
        emit({"valid": False, "command": args.command, "errors": errors}, error=True)
        return 2
    except (OSError, ValueError) as exc:
        emit({"valid": False, "command": args.command, "error": str(exc)}, error=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
