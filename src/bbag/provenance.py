"""Deterministic serialization and local Git provenance for S0 artifacts."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any


SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def git_provenance(root: Path | None = None) -> dict[str, Any]:
    root = (root or Path(__file__).resolve().parents[2]).resolve()

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, text=True,
            encoding="utf-8", capture_output=True, timeout=15,
        ).stdout.strip()

    try:
        if Path(run("rev-parse", "--show-toplevel")).resolve() != root:
            raise ValueError("Source directory is not the Git checkout root")
        commit = run("rev-parse", "HEAD")
        if not SHA_PATTERN.fullmatch(commit):
            raise ValueError("Git did not return a full SHA")
        return {"commit": commit, "branch": run("branch", "--show-current"),
                "dirty": bool(run("status", "--porcelain", "--untracked-files=normal"))}
    except (OSError, ValueError, subprocess.SubprocessError):
        # Unknown source state must never be represented as a clean commit.
        return {"commit": None, "branch": None, "dirty": True}


def safe_run_directory(output_dir: Path, run_id: str) -> Path:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run-id must contain 1-64 ASCII letters, digits, underscores or hyphens")
    base = output_dir.resolve()
    target = (base / run_id).resolve()
    if target.parent != base:
        raise ValueError("run-id must remain inside output-dir")
    if target.exists():
        raise ValueError("run-id already exists; existing experiment artifacts are immutable")
    return target
