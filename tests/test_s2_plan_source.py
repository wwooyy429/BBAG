"""Exercise actual Git hidden flags in isolated synthetic source repositories."""
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("scripts/plan_stage2_upload.py", "scripts/plan_stage2_continuation.py",
    "configs/s2.cloud-scope.json", "configs/s2.offline-campaign.json", "requirements.lock", "pyproject.toml",
    "src/bbag/__init__.py", "src/bbag/s2_continuation.py")


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE).decode().strip()


@pytest.fixture
def repo(tmp_path):
    for name in REQUIRED:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# SYNTHETIC_TEST\n", encoding="utf-8")
    checker = tmp_path / "scripts/s2_plan_source.py"
    checker.write_bytes((ROOT / "scripts/s2_plan_source.py").read_bytes())
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "SYNTHETIC_TEST")
    git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    git(tmp_path, "config", "core.autocrlf", "false")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "SYNTHETIC_TEST")
    return tmp_path, git(tmp_path, "rev-parse", "HEAD")


def invoke(root, sha, *, foreign=False):
    code = "import sys,pathlib,types;sys.path.insert(0,str(pathlib.Path.cwd()/'scripts'));from s2_plan_source import check_source;"
    if foreign:
        code += "m=types.ModuleType('bbag');m.__file__='foreign/bbag/__init__.py';sys.modules['bbag']=m;"
    code += "check_source(pathlib.Path.cwd(),sys.argv[1])"
    return subprocess.run([sys.executable, "-B", "-c", code, sha], cwd=root, capture_output=True)


def test_clean_exact_source_accepts_without_importing_bbag(repo):
    root, sha = repo
    result = invoke(root, sha)
    assert result.returncode == 0 and result.stdout == b"" and result.stderr == b""


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("name", ["scripts/plan_stage2_continuation.py", "src/bbag/s2_continuation.py"])
def test_hidden_script_and_module_bytes_cannot_claim_clean_sha(repo, flag, name):
    root, sha = repo
    git(root, "update-index", flag, "--", name)
    (root / name).write_text("# MODIFIED_SYNTHETIC_TEST\n", encoding="utf-8")
    assert git(root, "status", "--porcelain") == ""
    result = invoke(root, sha)
    assert result.returncode != 0 and b"source bytes differ" in result.stderr


def test_clean_checkout_cannot_use_foreign_loaded_bbag(repo):
    root, sha = repo
    result = invoke(root, sha, foreign=True)
    assert result.returncode != 0 and b"exact source checkout" in result.stderr


def test_wrong_head_rejected_before_source_import(repo):
    root, sha = repo
    result = invoke(root, "a"*40)
    assert result.returncode != 0 and b"exact clean Git source" in result.stderr


@pytest.mark.parametrize("relative", ["src/new", "docs/new", "artifacts/s2-bootstrap/metadata/new", "input-evidence/new"])
def test_output_cannot_pollute_source_or_retained_evidence(repo, relative):
    root, _ = repo
    code = "import sys,pathlib;sys.path.insert(0,str(pathlib.Path.cwd()/'scripts'));from s2_plan_source import check_output;"
    code += "r=pathlib.Path.cwd();check_output(r,r/sys.argv[1],protected=(r/'artifacts/s2-bootstrap',r/'input-evidence'))"
    result = subprocess.run([sys.executable, "-B", "-c", code, relative], cwd=root, capture_output=True)
    assert result.returncode != 0 and not (root / relative).exists()
