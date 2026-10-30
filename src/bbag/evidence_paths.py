"""Contain S0 evidence copying and preserve existing delivery outputs."""

from __future__ import annotations

import os
from pathlib import Path
import stat


# Cloud placeholder tags also set FILE_ATTRIBUTE_REPARSE_POINT. Only actual
# redirect tags are links; rejecting every reparse point would reject synced
# ordinary files. Numeric constants keep the same check on Python 3.11.
_REDIRECT_TAGS = {0xA0000003, 0xA000000C}  # MOUNT_POINT, SYMLINK


def _resolved(path: Path, *, strict: bool) -> Path:
    try:
        return path.resolve(strict=strict)
    except RuntimeError as exc:
        raise ValueError(f"Evidence or output path contains a link cycle: {path}") from exc


def validate_package_paths(evidence: Path, output: Path, archive: Path) -> None:
    """Reject existing destinations and input/output overlap before writing.

    ``output`` is the actual per-SHA directory, not its possibly shared parent.
    ``lexists`` also preserves dangling links that ``Path.exists`` misses.
    """
    for label, path in (("Package output", output), ("Audit archive", archive)):
        if os.path.lexists(path):
            raise ValueError(f"{label} already exists: {path}")
    root = _resolved(evidence, strict=True)
    for label, path in (("Package output", output), ("Audit archive", archive)):
        candidate = _resolved(path, strict=False)
        if candidate.is_relative_to(root) or root.is_relative_to(candidate):
            raise ValueError(f"{label} must not overlap the evidence directory: {path}")


def validate_evidence_tree(evidence: Path) -> None:
    """Require regular evidence files/directories without redirected children.

    The caller explicitly selects the root, so a root alias is resolved once.
    Child links and junctions are rejected even when they point within that
    root, preventing cycles as well as copying outside the selected evidence.
    """
    root = _resolved(evidence, strict=True)
    if not root.is_dir():
        raise ValueError(f"Evidence must be a directory: {evidence}")
    visited = {root}
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            metadata = child.lstat()
            if (stat.S_ISLNK(metadata.st_mode)
                    or getattr(metadata, "st_reparse_tag", 0) in _REDIRECT_TAGS):
                raise ValueError(f"Evidence must not contain child links or junctions: {child}")
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise ValueError(f"Evidence contains a non-regular file: {child}")
            actual = _resolved(child, strict=True)
            if not actual.is_relative_to(root):
                raise ValueError(f"Evidence path escapes the selected directory: {child}")
            if stat.S_ISDIR(metadata.st_mode):
                if actual in visited:
                    raise ValueError(f"Evidence contains a directory cycle: {child}")
                visited.add(actual)
                pending.append(child)
