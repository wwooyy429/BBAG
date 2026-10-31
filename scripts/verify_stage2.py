"""Collect the fixed ten S2 developer checks; never grant independent approval."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import bbag.s2_contracts as s2_contracts
from bbag.provenance import SHA_PATTERN, json_bytes
from bbag.s2_contracts import S2CheckRecord, S2ReviewReceipt, S2Verification


def git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=root, text=True, encoding="utf-8", stderr=subprocess.PIPE).strip()


def source_snapshot(root: Path) -> tuple[str, bool]:
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("Source directory must be the real Git checkout root, not an ancestor repository")
    commit = git(root, "rev-parse", "HEAD")
    if not SHA_PATTERN.fullmatch(commit):
        raise ValueError("Git must return a full implementation SHA")
    return commit, bool(git(root, "status", "--porcelain", "--untracked-files=normal"))


def require_tracked_inputs(root: Path) -> None:
    required = ["requirements.lock", "configs/s2.example.json", "configs/s2.cloud-scope.json", "configs/s2.offline-campaign.json",
                "tests/fixtures/s2_raw.json", "tests/fixtures/s2_expected.json", "scripts/check_stage2_results.py",
                "scripts/verify_stage2.py", "scripts/build_stage2_package.py"]
    required.extend(f"src/bbag/{name}.py" for name in ("s2_contracts", "s2_access", "s2_data", "s2_quantconnect", "s2_tasks", "s2_cli", "s2_snapshots", "s2_diagnostics", "s2_readiness"))
    git(root, "ls-files", "--error-unmatch", "--", *required)


def commands(output: Path) -> list[tuple[str, list[str], int]]:
    return [
        ("pip_check", ["-m", "pip", "check"], 0),
        ("pytest", ["-m", "pytest", "--junitxml", str(output / "junit.xml")], 0),
        ("schemas_s0", ["-m", "bbag", "schemas", "--stage", "S0", "--output-dir", "contracts", "--check"], 0),
        ("schemas_s1", ["-m", "bbag", "schemas", "--stage", "S1", "--output-dir", "contracts/s1", "--check"], 0),
        ("schemas_s2", ["-m", "bbag", "schemas", "--stage", "S2", "--output-dir", "contracts/s2", "--check"], 0),
        ("config_synthetic", ["-m", "bbag", "s2-validate", "--config", "configs/s2.example.json"], 0),
        ("config_cloud", ["-m", "bbag", "s2-validate", "--config", "configs/s2.cloud-scope.json"], 0),
        ("synthetic_run", ["-m", "bbag", "s2-run-synthetic", "--config", "configs/s2.example.json",
                           "--output-dir", str(output / "runs"), "--run-id", "synthetic"], 0),
        ("reference_check", ["scripts/check_stage2_results.py", "--run", str(output / "runs/synthetic")], 0),
        ("pending_review_gate", ["-m", "bbag", "check-review", "--stage", "S2", "--receipt",
                                 str(output / "review_request.json"), "--commit", "{commit}"], 3),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    commit, dirty_start = source_snapshot(root)
    if Path(s2_contracts.__file__).resolve() != root / "src/bbag/s2_contracts.py":
        raise ValueError("S2 contracts must be imported from this exact source checkout")
    require_tracked_inputs(root)
    if os.path.lexists(args.output_dir):
        raise ValueError("Verification output already exists; preserve it and choose a fresh directory")
    lock_hash = hashlib.sha256((root / "requirements.lock").read_bytes()).hexdigest()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    receipt = S2ReviewReceipt(audited_commit=commit, reviewer="S2 independent review pending")
    (output / "review_request.json").write_bytes(json_bytes(receipt.model_dump(mode="json")))
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    records = []
    for name, arguments, expected in commands(output):
        command = [sys.executable, *(commit if value == "{commit}" else value for value in arguments)]
        started_at, started = datetime.now(timezone.utc), time.perf_counter()
        result = subprocess.run(command, cwd=root, env=environment, capture_output=True)
        (output / f"{name}.stdout.txt").write_bytes(result.stdout)
        (output / f"{name}.stderr.txt").write_bytes(result.stderr)
        records.append(S2CheckRecord(name=name, command=command, exit_code=result.returncode,
            expected_exit_code=expected, passed=result.returncode == expected,
            started_at_utc=started_at, duration_seconds=time.perf_counter() - started))
    commit_end, dirty_end = source_snapshot(root)
    if lock_hash != hashlib.sha256((root / "requirements.lock").read_bytes()).hexdigest():
        raise ValueError("Dependency lock changed during verification; retain raw output and verify a fresh SHA")
    summary = S2Verification(implementation_commit=commit, code_commit_end=commit_end,
        working_tree_dirty_start=dirty_start, working_tree_dirty_end=dirty_end,
        generated_at_utc=datetime.now(timezone.utc), python=sys.version, platform=platform.platform(),
        dependency_lock_sha256=lock_hash, checks=records,
        developer_checks_passed=all(record.passed for record in records) and not dirty_start and not dirty_end and commit == commit_end)
    (output / "verification.json").write_bytes(json_bytes(summary.model_dump(mode="json")))
    print(summary.model_dump_json(indent=2))
    return 0 if summary.developer_checks_passed else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"stage": "S2", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
