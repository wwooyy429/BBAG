"""Shared S0 development-test fixtures; these are not independent Work approval."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def example_path() -> Path:
    return PROJECT_ROOT / "configs" / "s0.example.json"


@pytest.fixture
def protocol_payload(example_path: Path) -> dict[str, Any]:
    return json.loads(example_path.read_text(encoding="utf-8"))


@pytest.fixture
def write_json(tmp_path: Path) -> Callable[..., Path]:
    def write(value: Any, name: str = "input.json") -> Path:
        target = tmp_path / name
        target.write_text(
            json.dumps(value, ensure_ascii=False, allow_nan=True),
            encoding="utf-8",
        )
        return target

    return write


@pytest.fixture
def run_cli() -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(*arguments: object, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        source = str(PROJECT_ROOT / "src")
        previous = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = source if not previous else source + os.pathsep + previous
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-m", "bbag", *(str(argument) for argument in arguments)],
            cwd=cwd or PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

    return run


@pytest.fixture
def cli_json() -> Callable[[subprocess.CompletedProcess[str]], dict[str, Any]]:
    def parse(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        raw = result.stderr if result.returncode == 2 else result.stdout
        assert raw.strip(), (result.returncode, result.stdout, result.stderr)
        assert "Traceback" not in result.stderr
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
        return parsed

    return parse


@pytest.fixture
def approved_receipt_payload() -> dict[str, Any]:
    # A synthetic receipt tests consistency only; it never represents real approval.
    return {
        "schema_version": "1.0",
        "stage": "S0",
        "audited_commit": "a" * 40,
        "reviewer": "Synthetic reviewer for development tests",
        "decision": "APPROVED",
        "engineering_status": "PASS",
        "platform_status": "NOT_APPLICABLE",
        "economic_status": "NOT_APPLICABLE",
        "blocking_findings": 0,
        "evidence": ["synthetic://development-test-evidence"],
        "reviewed_at": "2026-10-31T08:00:00+08:00",
    }


@pytest.fixture
def pending_receipt_payload(approved_receipt_payload: dict[str, Any]) -> dict[str, Any]:
    receipt = copy.deepcopy(approved_receipt_payload)
    receipt.update(
        decision="PENDING",
        engineering_status="INCOMPLETE",
        evidence=[],
        reviewed_at=None,
    )
    return receipt
