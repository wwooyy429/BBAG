"""Collect ten S1 developer checks from one real checkout; never approve S1."""
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

import bbag.s1_contracts as s1_contracts
from bbag.provenance import SHA_PATTERN, json_bytes
from bbag.s1_contracts import S1CheckRecord, S1ReviewReceipt, S1Verification


def git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=root, text=True,
                                   encoding="utf-8", stderr=subprocess.PIPE).strip()


def source_snapshot(root: Path) -> tuple[str, bool]:
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("Source directory must be the real Git checkout root, not an ancestor repository")
    commit = git(root, "rev-parse", "HEAD")
    if not SHA_PATTERN.fullmatch(commit):
        raise ValueError("Git must return a full implementation SHA")
    return commit, bool(git(root, "status", "--porcelain", "--untracked-files=normal"))


def commands(output: Path) -> list[tuple[str, list[str], int]]:
    """Fixed S1 protocol; the reference checker computes no factor values."""
    runs = output / "runs"
    return [
        ("pip_check", ["-m", "pip", "check"], 0),
        ("pytest", ["-m", "pytest", "--junitxml", str(output / "junit.xml")], 0),
        ("audit", ["-m", "bbag", "s1-audit", "--config", "configs/s1.example.json"], 0),
        ("schemas_s0", ["-m", "bbag", "schemas", "--stage", "S0", "--output-dir", "contracts", "--check"], 0),
        ("schemas_s1", ["-m", "bbag", "schemas", "--stage", "S1", "--output-dir", "contracts/s1", "--check"], 0),
        ("factors_batch", ["-m", "bbag", "s1-factors", "--config", "configs/s1.example.json",
                           "--output-dir", str(runs), "--run-id", "batch", "--mode", "batch"], 0),
        ("factors_incremental", ["-m", "bbag", "s1-factors", "--config", "configs/s1.example.json",
                                 "--output-dir", str(runs), "--run-id", "incremental", "--mode", "incremental"], 0),
        ("labels", ["-m", "bbag", "s1-labels", "--config", "configs/s1.example.json",
                    "--output-dir", str(runs), "--run-id", "labels"], 0),
        ("reference_check", ["scripts/check_stage1_results.py", "--expected", "tests/fixtures/s1_expected.json",
                             "--batch", str(runs / "batch"), "--incremental", str(runs / "incremental"),
                             "--labels", str(runs / "labels")], 0),
        ("pending_review_gate", ["-m", "bbag", "check-review", "--stage", "S1", "--receipt",
                                 str(output / "review_request.json"), "--commit", "{commit}"], 3),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    commit, dirty_start = source_snapshot(root)
    if Path(s1_contracts.__file__).resolve() != root / "src/bbag/s1_contracts.py":
        raise ValueError("S1 contracts must be imported from this exact source checkout")
    if os.path.lexists(args.output_dir):
        raise ValueError("Verification output already exists; preserve it and choose a fresh directory")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    receipt = S1ReviewReceipt(audited_commit=commit, reviewer="S1 independent review pending")
    (output / "review_request.json").write_bytes(json_bytes(receipt.model_dump(mode="json")))
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    records = []
    for name, arguments, expected in commands(output):
        arguments = [commit if item == "{commit}" else item for item in arguments]
        command = [sys.executable, *arguments]
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        result = subprocess.run(command, cwd=root, env=environment, capture_output=True)
        (output / f"{name}.stdout.txt").write_bytes(result.stdout)
        (output / f"{name}.stderr.txt").write_bytes(result.stderr)
        records.append(S1CheckRecord(name=name, command=command, exit_code=result.returncode,
            expected_exit_code=expected, passed=result.returncode == expected,
            started_at_utc=started_at, duration_seconds=time.perf_counter() - started))
    commit_end, dirty_end = source_snapshot(root)
    dirty = dirty_start or dirty_end
    summary = S1Verification(
        implementation_commit=commit, code_commit_end=commit_end,
        working_tree_dirty_start=dirty_start, working_tree_dirty_end=dirty_end, working_tree_dirty=dirty,
        generated_at_utc=datetime.now(timezone.utc), python=sys.version, python_executable=sys.executable,
        platform=platform.platform(),
        dependency_lock_sha256=hashlib.sha256((root / "requirements.lock").read_bytes()).hexdigest(),
        subprocess_output_encoding="UTF-8 requested; original stdout/stderr bytes preserved",
        checks=records,
        developer_checks_passed=all(item.passed for item in records) and not dirty and commit == commit_end,
    )
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
        print(json.dumps({"stage": "S1", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
