"""Delivery path regressions; all external targets are synthetic fixtures."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from bbag.evidence_paths import validate_evidence_tree, validate_package_paths
from test_provenance_boundaries import (
    copy_script_source,
    fixture_git,
    guarded_script,
    make_repository,
    make_synthetic_package_evidence,
)


def test_normal_nested_evidence_and_shared_output_parent_are_allowed(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    nested = evidence / "nested"
    nested.mkdir(parents=True)
    (nested / "raw.txt").write_text("Synthetic raw output", encoding="utf-8")
    validate_evidence_tree(evidence)
    validate_package_paths(evidence, tmp_path / "implementation-sha", tmp_path / "audit.zip")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["evidence"]


@pytest.mark.parametrize("kind", ["same", "child", "ancestor", "archive_child"])
def test_reject_input_output_overlap_before_writes(tmp_path: Path, kind: str) -> None:
    evidence = tmp_path / "parent" / "evidence"
    evidence.mkdir(parents=True)
    output, archive = tmp_path / "fresh-output", tmp_path / "fresh.zip"
    if kind == "same":
        output = evidence
    elif kind == "child":
        output = evidence / "fresh-output"
    elif kind == "ancestor":
        output = evidence.parent
    else:
        archive = evidence / "fresh.zip"
    with pytest.raises(ValueError):
        validate_package_paths(evidence, output, archive)
    assert not (tmp_path / "fresh-output").exists()
    assert not (tmp_path / "fresh.zip").exists()
    assert list(evidence.iterdir()) == []


@pytest.mark.parametrize("kind", ["archive", "output"])
def test_existing_destination_is_preserved_without_other_output(tmp_path: Path, kind: str) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    output, archive = tmp_path / "fresh-output", tmp_path / "fresh.zip"
    if kind == "archive":
        archive.write_bytes(b"SYNTHETIC EXISTING ARCHIVE")
    else:
        output.mkdir()
        (output / "sentinel.txt").write_bytes(b"SYNTHETIC EXISTING OUTPUT")
    with pytest.raises(ValueError, match="already exists"):
        validate_package_paths(evidence, output, archive)
    if kind == "archive":
        assert archive.read_bytes() == b"SYNTHETIC EXISTING ARCHIVE"
        assert not output.exists()
    else:
        assert (output / "sentinel.txt").read_bytes() == b"SYNTHETIC EXISTING OUTPUT"
        assert not archive.exists()


def make_symlink(path: Path, target: Path, *, directory: bool = False) -> None:
    try:
        path.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Actual symlinks are unavailable in this environment: {exc}")


@pytest.mark.parametrize("kind", ["output", "archive"])
def test_dangling_destination_link_is_existing(tmp_path: Path, kind: str) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    output, archive = tmp_path / "fresh-output", tmp_path / "fresh.zip"
    destination = output if kind == "output" else archive
    make_symlink(destination, tmp_path / "missing-target", directory=kind == "output")
    assert os.path.lexists(destination) and not destination.exists()
    with pytest.raises(ValueError, match="already exists"):
        validate_package_paths(evidence, output, archive)
    assert destination.is_symlink()


@pytest.mark.parametrize("kind", ["outside_file", "inside_file", "root_cycle", "dangling"])
def test_actual_evidence_child_symlinks_are_rejected(tmp_path: Path, kind: str) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("SYNTHETIC OUTSIDE FILE", encoding="utf-8")
    inside = evidence / "inside.txt"
    inside.write_text("SYNTHETIC INSIDE FILE", encoding="utf-8")
    target = {"outside_file": outside, "inside_file": inside,
              "root_cycle": evidence, "dangling": tmp_path / "missing"}[kind]
    make_symlink(evidence / "link", target, directory=kind == "root_cycle")
    with pytest.raises(ValueError, match="child links or junctions"):
        validate_evidence_tree(evidence)
    assert outside.read_text(encoding="utf-8") == "SYNTHETIC OUTSIDE FILE"


def test_explicit_root_alias_is_allowed(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "raw.txt").write_text("SYNTHETIC FILE", encoding="utf-8")
    alias = tmp_path / "root-alias"
    make_symlink(alias, selected, directory=True)
    validate_evidence_tree(alias)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
@pytest.mark.parametrize("kind", ["outside", "root_cycle"])
def test_actual_windows_junction_is_rejected(tmp_path: Path, kind: str) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "raw.txt").write_text("SYNTHETIC OUTSIDE", encoding="utf-8")
    target = outside if kind == "outside" else evidence
    link = evidence / "junction"
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is required to create the Windows junction fixture")
    command = ("New-Item -ItemType Junction -Path '" + str(link).replace("'", "''")
               + "' -Target '" + str(target).replace("'", "''") + "' | Out-Null")
    result = subprocess.run([shell, "-NoProfile", "-Command", command], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    with pytest.raises(ValueError, match="child links or junctions"):
        validate_evidence_tree(evidence)
    assert (outside / "raw.txt").read_text(encoding="utf-8") == "SYNTHETIC OUTSIDE"


def test_cloud_placeholder_reparse_tag_is_not_a_redirect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    path = evidence / "ordinary-cloud-file.txt"
    path.write_text("SYNTHETIC CLOUD PLACEHOLDER", encoding="utf-8")
    original = Path.lstat

    def cloud_lstat(selected: Path, *args: Any, **kwargs: Any) -> Any:
        if selected == path:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_reparse_tag=0x9000001A)
        return original(selected, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", cloud_lstat)
    validate_evidence_tree(evidence)


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_evidence_root_must_be_a_directory(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "not-a-directory"
    if kind == "file":
        path.write_text("SYNTHETIC FILE", encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        validate_evidence_tree(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX special-file boundary")
def test_fifo_evidence_is_rejected_without_reading(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    os.mkfifo(evidence / "fifo")
    with pytest.raises(ValueError, match="non-regular file"):
        validate_evidence_tree(evidence)


@pytest.mark.parametrize("fault", ["nested_output", "existing_archive", "junction"])
def test_packager_rejects_path_faults_before_archive_or_copy(
    tmp_path: Path, project_root: Path, example_path: Path, fault: str,
) -> None:
    if fault == "junction" and os.name != "nt":
        pytest.skip("Actual Windows junction integration")
    repository, commit = make_repository(tmp_path / "repository")
    copy_script_source(project_root, repository)
    (repository / ".git/info/exclude").write_text("src/\nscripts/\n", encoding="utf-8")
    assert fixture_git(repository, "status", "--porcelain") == ""
    evidence = tmp_path / "SYNTHETIC_EVIDENCE_ONLY"
    make_synthetic_package_evidence(evidence, commit, example_path)
    output_base = tmp_path / "output"
    if fault == "nested_output":
        output_base = evidence / "output"
    elif fault == "existing_archive":
        output_base.mkdir()
        archive = output_base / f"BBAG_S0_GPT_Work_{commit[:12]}.zip"
        archive.write_bytes(b"SYNTHETIC EXISTING ARCHIVE DO NOT OVERWRITE")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "raw.txt").write_text("SYNTHETIC OUTSIDE FILE", encoding="utf-8")
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            pytest.skip("PowerShell is required to create the junction integration fixture")
        link = evidence / "junction"
        command = ("New-Item -ItemType Junction -Path '" + str(link).replace("'", "''")
                   + "' -Target '" + str(outside).replace("'", "''") + "' | Out-Null")
        result = subprocess.run([shell, "-NoProfile", "-Command", command], capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
    result, marker = guarded_script(repository, "build_review_package.py", [
        "--commit", commit, "--evidence-dir", str(evidence), "--output-dir", str(output_base),
    ], tmp_path / "guard")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "Traceback" not in result.stderr
    assert not marker.exists()
    assert not (output_base / commit).exists()
    if fault == "existing_archive":
        assert archive.read_bytes() == b"SYNTHETIC EXISTING ARCHIVE DO NOT OVERWRITE"
        assert "already exists" in result.stderr
    else:
        assert not output_base.exists()
        expected = "overlap" if fault == "nested_output" else "child links or junctions"
        assert expected in result.stderr
