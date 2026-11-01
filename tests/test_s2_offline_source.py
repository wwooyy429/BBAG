"""Real temporary Git controls; fixture commits are not project/audit commits."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_provenance_boundaries import fixture_git, make_repository


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source_repository(tmp_path):
    repository, _ = make_repository(tmp_path / "SYNTHETIC_SOURCE")
    source = repository / "src/bbag"
    source.mkdir(parents=True)
    for path in (ROOT / "src/bbag").glob("*.py"):
        shutil.copyfile(path, source / path.name)
    for relative in ("configs/s2.example.json", "tests/fixtures/s2_raw.json", "pyproject.toml"):
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    fixture_git(repository, "add", ".")
    tree = fixture_git(repository, "write-tree")
    text = (f"tree {tree}\n"
            "author Offline Source Fixture <fixture@example.invalid> 1700000000 +0000\n"
            "committer Offline Source Fixture <fixture@example.invalid> 1700000000 +0000\n\n"
            "SYNTHETIC full offline source identity only\n")
    commit = fixture_git(repository, "hash-object", "-t", "commit", "-w", "--stdin", input_text=text)
    fixture_git(repository, "update-ref", "refs/heads/fixture-main", commit)
    assert fixture_git(repository, "status", "--porcelain") == ""
    return repository, commit


def run_offline(repository, cache, *, cwd=None):
    environment = os.environ.copy()
    environment.update(PYTHONPATH=str(repository / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
                       GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run([sys.executable, "-B", "-X", "utf8", "-m", "bbag", "s2-offline-check",
        "--config", str(repository / "configs/s2.example.json"), "--cache-dir", str(cache)],
        cwd=cwd or repository, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=40)


@pytest.mark.parametrize("unrelated_cwd", [False, True])
def test_full_cli_binds_real_fixture_git_bytes_and_works_from_unrelated_cwd(tmp_path, source_repository, unrelated_cwd):
    repository, commit = source_repository
    cwd = tmp_path / "UNRELATED_CWD"
    cwd.mkdir()
    cache = tmp_path / "cache"
    result = run_offline(repository, cache, cwd=cwd if unrelated_cwd else repository)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    tracked_sources = fixture_git(repository, "ls-tree", "-r", "--name-only", commit, "--", "src/bbag").splitlines()
    assert output["code_commit"] == commit and not output["working_tree_dirty"]
    assert output["bound_source_files"] == len(tracked_sources) + 4
    assert output["synthetic_fetch_count"] == output["repeat_cache_hits"] == 4
    assert output["new_real_platform_calls"] == 0 and output["platform_execution"] == "NOT_RUN"
    assert fixture_git(repository, "status", "--porcelain") == ""


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("target", ["src/bbag/s2_data.py", "configs/s2.example.json", "requirements.lock", "tests/fixtures/s2_raw.json"])
def test_full_cli_rejects_hidden_source_or_fixed_input_before_creating_cache(tmp_path, source_repository, flag, target):
    repository, commit = source_repository
    path = repository / target
    committed = path.read_bytes()
    fixture_git(repository, "update-index", flag, target)
    path.write_bytes(committed + (b"\n \n" if path.suffix == ".json" else b"\n# SYNTHETIC HIDDEN SOURCE CONTROL\n"))
    assert fixture_git(repository, "status", "--porcelain") == ""
    assert fixture_git(repository, "rev-parse", "HEAD") == commit
    assert hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(committed).digest()
    cache = tmp_path / "MUST_NOT_BE_CREATED"
    result = run_offline(repository, cache)
    witness = {"scope": "SYNTHETIC_TEMPORARY_GIT_SOURCE_NOT_A_PROJECT_SHA", "source_commit": commit,
               "flag": flag, "target": target, "status": fixture_git(repository, "status", "--porcelain"),
               "committed_sha256": hashlib.sha256(committed).hexdigest(),
               "disk_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
               "exit_code": result.returncode, "cache_created": cache.exists()}
    (tmp_path / "witness.json").write_text(json.dumps(witness, indent=2) + "\n", encoding="utf-8")
    assert result.returncode == 2, (witness, result.stdout, result.stderr)
    rejected = json.loads(result.stderr)
    assert rejected["valid"] is False and "exact committed bytes" in rejected["error"]
    assert not cache.exists()
    assert path.read_bytes() != committed and fixture_git(repository, "status", "--porcelain") == ""
