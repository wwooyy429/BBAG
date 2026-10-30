"""Collect repeatable S0 developer evidence, never independent acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        checkout_root = Path(subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], cwd=root, text=True,
            encoding="utf-8", stderr=subprocess.PIPE,
        ).strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        parser.error("Verification requires a real Git checkout rooted at the source directory")
    if checkout_root != root:
        parser.error("Source directory must be the Git checkout root; ancestor repositories are not valid provenance")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True, encoding="utf-8").strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True, encoding="utf-8").strip())
    receipt = {
        "schema_version": "1.0", "stage": "S0", "audited_commit": commit,
        "reviewer": "GPT Work independent review pending", "decision": "PENDING",
        "engineering_status": "INCOMPLETE", "platform_status": "NOT_APPLICABLE",
        "economic_status": "NOT_APPLICABLE", "blocking_findings": 0,
        "evidence": [], "reviewed_at": None,
    }
    request = output / "review_request.json"
    request.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    commands = [
        ("pip_check", ["-m", "pip", "check"], 0),
        ("pytest", ["-m", "pytest", "--junitxml", str(output / "junit.xml")], 0),
        ("validate", ["-m", "bbag", "validate", "--config", "configs/s0.example.json"], 0),
        ("schemas", ["-m", "bbag", "schemas", "--output-dir", "contracts", "--check"], 0),
        ("experiment", ["-m", "bbag", "init-experiment", "--config", "configs/s0.example.json",
                        "--output-dir", str(output / "experiments"), "--run-id", "s0-demo"], 0),
        ("pending_review_gate", ["-m", "bbag", "check-review", "--receipt", str(request), "--commit", commit], 3),
    ]
    records = []
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    for name, arguments, expected in commands:
        command = [sys.executable, *arguments]
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.perf_counter()
        result = subprocess.run(command, cwd=root, capture_output=True, env=environment)
        (output / f"{name}.stdout.txt").write_bytes(result.stdout)
        (output / f"{name}.stderr.txt").write_bytes(result.stderr)
        records.append({"name": name, "command": command, "exit_code": result.returncode,
                        "expected_exit_code": expected, "passed": result.returncode == expected,
                        "started_at_utc": started_at, "duration_seconds": time.perf_counter() - started})
    summary = {
        "stage": "S0", "implementation_commit": commit, "working_tree_dirty": dirty,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version,
        "python_executable": sys.executable, "platform": platform.platform(),
        "subprocess_output_encoding": "UTF-8 requested; original stdout/stderr bytes preserved",
        "dependency_lock_sha256": hashlib.sha256((root / "requirements.lock").read_bytes()).hexdigest(),
        "checks": records, "developer_checks_passed": all(item["passed"] for item in records),
        "independent_review": "PENDING", "platform_integration": "NOT_APPLICABLE",
        "economic_evidence": "NOT_APPLICABLE", "next_stage_authorized": False,
    }
    (output / "verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["developer_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
