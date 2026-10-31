"""Local exact-source consistency for offline handoff scripts; not a sandbox."""
from __future__ import annotations

import io
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


def check_output(root: Path, output: Path, *, protected: tuple[Path, ...] = ()) -> Path:
    """Reject links and overlap with source or retained input directories."""
    target = output.absolute()
    for parent in (target, *target.parents):
        if os.path.lexists(parent):
            meta = parent.lstat()
            if stat.S_ISLNK(meta.st_mode) or getattr(meta, "st_file_attributes", 0) & 0x400:
                raise ValueError("planning output must not follow links/reparse points")
    target = target.resolve()
    denied = [root / name for name in ("src", "cloud", "configs", "scripts", "tests", "contracts", "docs", ".git")]
    denied.extend(path.resolve() for path in protected)
    if target == root or any(target.is_relative_to(path) or path.is_relative_to(target) for path in denied):
        raise ValueError("planning output overlaps committed source or retained input evidence")
    if os.path.lexists(target):
        raise ValueError("planning output already exists; preserve previous evidence")
    return target


def check_source(root: Path, commit: str) -> None:
    def git(*args, data=None):
        return subprocess.check_output(["git", "-c", "core.fsmonitor=false", *args], cwd=root,
            input=data, stderr=subprocess.PIPE)
    if (type(commit) is not str or re.fullmatch(r"[0-9a-f]{40}", commit) is None
            or Path(git("rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve() != root
            or git("rev-parse", "HEAD").decode("ascii").strip() != commit
            or git("status", "--porcelain", "--untracked-files=normal").strip()):
        raise ValueError("exact clean Git source required for offline planning")
    required = {"scripts/plan_stage2_upload.py", "scripts/plan_stage2_continuation.py", "scripts/s2_plan_source.py",
        "configs/s2.cloud-scope.json", "configs/s2.offline-campaign.json", "requirements.lock", "pyproject.toml"}
    records = git("ls-tree", "-rz", "--full-tree", commit, "--", "src/bbag", *sorted(required))
    entries = {}
    for record in records.split(b"\0"):
        if record:
            meta, path = record.split(b"\t", 1)
            mode, kind, blob = meta.split()
            if kind != b"blob" or mode not in {b"100644", b"100755"}:
                raise ValueError("planning source must contain regular Git blobs")
            entries[path.decode("utf-8")] = blob
    if not required <= entries.keys():
        raise ValueError("planning source inputs are missing from the exact commit")
    stream = io.BytesIO(git("cat-file", "--batch", data=b"\n".join(entries.values()) + b"\n"))
    for path, blob in entries.items():
        target = root / path
        meta = target.lstat()
        header = stream.readline().split()
        if (not stat.S_ISREG(meta.st_mode) or getattr(meta, "st_file_attributes", 0) & 0x400
                or len(header) != 3 or header[:2] != [blob, b"blob"]):
            raise ValueError("planning source file identity differs")
        body = stream.read(int(header[2]))
        if stream.read(1) != b"\n" or target.read_bytes() != body:
            raise ValueError("planning source bytes differ from Git, including hidden flags")
    if stream.read():
        raise ValueError("unexpected source object stream")
    expected_scripts = root / "scripts"
    for name, module in tuple(sys.modules.items()):
        location = getattr(module, "__file__", None)
        if name == "bbag" or name.startswith("bbag."):
            if location is None or not Path(location).resolve().is_relative_to(root / "src/bbag"):
                raise ValueError("bbag modules must load from the exact source checkout")
        if name == "s2_plan_source" and Path(location).resolve() != expected_scripts / "s2_plan_source.py":
            raise ValueError("source checker must load from this checkout")
