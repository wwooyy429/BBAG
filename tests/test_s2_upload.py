"""Synthetic temporary Git source planning; no QuantConnect or SDK execution."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import bbag.s2_upload as upload_module

from bbag.s2_upload import (
    S2_UPLOAD_CONTRACTS, UploadManifest, UploadSourceSpec, build_upload_manifest,
    canonical_upload_manifest_bytes, read_upload_sources, upload_manifest_sha256,
)


INIT = "src/bbag/__init__.py"
JSON = "src/bbag/jsonio.py"
CONTRACTS = "src/bbag/contracts.py"
ENTRY = "src/bbag/s2_entry.py"
MAIN = "cloud/main.py"
BODIES = {
    INIT: b'"""SYNTHETIC upload-only fixture, never platform evidence."""\n',
    JSON: b"import json\n\ndef object_value():\n    return {}\n",
    CONTRACTS: b"from .jsonio import object_value\nVALUE = 7\n",
    ENTRY: b"from .contracts import VALUE\nfrom . import jsonio\n",
    MAIN: b"from AlgorithmImports import QCAlgorithm\n",
    "src/bbag/unrelated.py": b"SYNTHETIC_EXTRA = True\n",
}


def git(root, *args):
    return subprocess.check_output(["git", "-c", "core.fsmonitor=false", *args], cwd=root,
        env=dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull), stderr=subprocess.PIPE).decode("utf-8").strip()


def commit(root):
    git(root, "add", "--all")
    git(root, "-c", "user.name=Synthetic Upload Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--no-gpg-sign", "-m", "synthetic upload source only")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture(scope="session")
def source_seed(tmp_path_factory):
    root = tmp_path_factory.mktemp("synthetic-upload-seed") / "source"
    root.mkdir()
    git(root, "init")
    git(root, "config", "core.autocrlf", "false")
    (root / ".gitignore").write_text("ignored_runtime/\nignored*.py\n", encoding="utf-8")
    for name, body in BODIES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return root, commit(root)


@pytest.fixture
def source(tmp_path, source_seed):
    # Each case owns a real, independent Git database; only immutable fixture
    # construction is shared, avoiding dozens of redundant git init/commit runs.
    seed, sha = source_seed
    root = tmp_path / "synthetic-source"
    shutil.copytree(seed, root)
    return root, sha


def specs():
    return [
        UploadSourceSpec(source_path=ENTRY, remote_path="bbag/s2_entry.py", dependencies=[INIT, JSON, CONTRACTS]),
        UploadSourceSpec(source_path=MAIN, remote_path="main.py"),
        UploadSourceSpec(source_path=CONTRACTS, remote_path="bbag/contracts.py", dependencies=[JSON, INIT]),
        UploadSourceSpec(source_path=INIT, remote_path="bbag/__init__.py"),
        UploadSourceSpec(source_path=JSON, remote_path="bbag/jsonio.py", dependencies=[INIT]),
    ]


def build(source, *, selected=None, entries=None, **kwargs):
    root, sha = source
    return build_upload_manifest(root, sha, specs() if selected is None else selected,
        [ENTRY, MAIN] if entries is None else entries, max_file_bytes=kwargs.pop("max_file_bytes", 65536),
        max_total_bytes=kwargs.pop("max_total_bytes", 524288), **kwargs)


def changed_source(source, path, body):
    root, _ = source
    (root / path).write_bytes(body)
    return root, commit(root)


def test_full_fresh_exact_source_and_hand_counted_compile_proposal(source, tmp_path, monkeypatch):
    unrelated = tmp_path / "unrelated-cwd"
    unrelated.mkdir()
    (unrelated / "requirements.lock").write_text("synthetic unrelated lock", encoding="utf-8")
    monkeypatch.chdir(unrelated)
    plan = build(source, extra_compile_calls=2)
    assert [file.source_path for file in plan.files] == [MAIN, INIT, JSON, CONTRACTS, ENTRY]
    assert plan.write_call_count == 5 and plan.extra_compile_calls == 2
    assert plan.required_compile_increment == 7 and plan.proposed_compile_total_cap == 9
    assert (plan.historical_compile_used, plan.historical_compile_cap, plan.current_compile_remaining) == (2, 2, 0)
    assert plan.authorized_compile_increment == plan.available_compile_calls == 0
    assert not plan.executable and not plan.authorization_granted and not plan.budget_change_applied
    assert plan.profile == "FULL_FRESH" and plan.remote_state == "UNKNOWN_NOT_QUERIED"
    assert plan.platform_execution == "NOT_RUN" and plan.independent_review == "PENDING"
    assert set(plan.external_import_roots) == {"AlgorithmImports", "json"}
    assert plan.total_source_bytes == sum(len(BODIES[file.source_path]) for file in plan.files)
    for order, file in enumerate(plan.files, 1):
        assert file.source_commit == source[1] and file.order == order and file.compile_charge == 1
        assert file.size_bytes == len(BODIES[file.source_path])
        assert file.source_sha256 == hashlib.sha256(BODIES[file.source_path]).hexdigest()
        assert file.git_blob == git(source[0], "rev-parse", source[1] + ":" + file.source_path)
        assert file.write_mode == "CREATE_OR_EDIT_AFTER_REMOTE_PREFLIGHT"
    payload = read_upload_sources(source[0], plan)
    assert payload == {file.remote_path: BODIES[file.source_path] for file in plan.files}
    assert "bbag/unrelated.py" not in payload
    assert git(source[0], "status", "--porcelain") == ""


def test_default_extra_zero_and_order_independent_canonical_manifest(source):
    first = build(source)
    reverse = specs()[::-1]
    for item in reverse:
        item.dependencies.reverse()
    second = build(source, selected=reverse, entries=[MAIN, ENTRY])
    assert first == second
    assert (first.write_call_count, first.extra_compile_calls, first.required_compile_increment) == (5, 0, 5)
    raw = canonical_upload_manifest_bytes(first)
    assert upload_manifest_sha256(first) == hashlib.sha256(raw).hexdigest()
    assert UploadManifest.model_validate_json(raw) == first
    assert set(S2_UPLOAD_CONTRACTS) == {"UploadSourceSpec", "UploadFile", "UploadManifest"}


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_selected_source_change_cannot_masquerade_as_committed(source, flag):
    root, _ = source
    git(root, "update-index", flag, JSON)
    (root / JSON).write_bytes(BODIES[JSON] + b"HIDDEN_SYNTHETIC_CHANGE = True\n")
    assert git(root, "status", "--porcelain") == ""
    with pytest.raises(ValueError, match="disk bytes differ"):
        build(source)


@pytest.mark.parametrize("kind", ["normal_untracked", "ignored_selected", "dirty_selected", "wrong_commit", "ancestor_root"])
def test_rejects_uncommitted_or_wrong_repository_source(source, kind):
    root, sha = source
    selected = specs()
    if kind == "normal_untracked":
        (root / "src/bbag/untracked.py").write_text("UNCOMMITTED = True\n", encoding="utf-8")
    elif kind == "ignored_selected":
        path = "src/bbag/ignored_new.py"
        (root / path).write_text("UNCOMMITTED = True\n", encoding="utf-8")
        assert git(root, "status", "--porcelain") == ""
        selected.append(UploadSourceSpec(source_path=path, remote_path="bbag/ignored_new.py", dependencies=[INIT]))
    elif kind == "dirty_selected":
        (root / JSON).write_bytes(BODIES[JSON] + b"DIRTY = True\n")
    elif kind == "wrong_commit":
        sha = "0" * 40
    else:
        root = root.parent
    with pytest.raises(ValueError):
        build_upload_manifest(root, sha, selected, [ENTRY, MAIN], max_file_bytes=65536, max_total_bytes=524288)


@pytest.mark.parametrize("path", ["../x.py", "/x.py", "src/bbag//x.py", "src/bbag/./x.py", "src/bbag/../x.py",
    "src\\bbag\\x.py", "src/bbag/x.PY", "src/bbag/NUL.py", "src/bbag/a?.py", "src/bbag/a.py ",
    "tests/test_x.py", "src/bbag/*.py", "src/bbag/a:b.py", "src/bbag/from.py"])
def test_rejects_unsafe_or_nonexplicit_source_paths(path):
    with pytest.raises(ValueError):
        UploadSourceSpec(source_path=path, remote_path="bbag/x.py")


def test_rejects_remote_rename_and_case_collision(source):
    with pytest.raises(ValueError, match="module identity"):
        UploadSourceSpec(source_path=JSON, remote_path="bbag/renamed.py", dependencies=[INIT])
    selected = specs() + [UploadSourceSpec(source_path="src/bbag/JSONIO.py", remote_path="bbag/JSONIO.py", dependencies=[INIT])]
    with pytest.raises(ValueError, match="colliding"):
        build(source, selected=selected)


@pytest.mark.parametrize("kind", ["duplicate_source", "missing_imported_file", "missing_init", "wrong_declaration", "extra_unrelated", "missing_entry", "duplicate_entry"])
def test_rejects_incomplete_or_nonexact_dependency_closure(source, kind):
    selected, entries = specs(), [ENTRY, MAIN]
    if kind == "duplicate_source":
        selected.append(deepcopy(selected[0]))
    elif kind == "missing_imported_file":
        selected = [item for item in selected if item.source_path != JSON]
    elif kind == "missing_init":
        selected = [item for item in selected if item.source_path != INIT]
    elif kind == "wrong_declaration":
        selected[0].dependencies = [INIT, CONTRACTS]
    elif kind == "extra_unrelated":
        selected.append(UploadSourceSpec(source_path="src/bbag/unrelated.py", remote_path="bbag/unrelated.py", dependencies=[INIT]))
    elif kind == "missing_entry":
        entries = ["src/bbag/no_entry.py"]
    else:
        entries = [ENTRY, ENTRY]
    with pytest.raises(ValueError):
        build(source, selected=selected, entries=entries)


def test_cycle_in_real_static_imports_is_rejected(source):
    source = changed_source(source, JSON, b"from .contracts import VALUE\n")
    selected = specs()
    selected[-1].dependencies = [INIT, CONTRACTS]
    with pytest.raises(ValueError, match="cyclic"):
        build(source, selected=selected)


@pytest.mark.parametrize("body", [
    b"from .not_committed import VALUE\n", b"from .contracts import *\n",
    b"import importlib\nimportlib.import_module('bbag.secret')\n",
    b"from builtins import __import__ as load\n", b"__import__('bbag.secret')\n",
    b"exec('import bbag.secret')\n", b"import unreviewed_third_party\n", b"from ... import escaped\n",
    b"not valid Python !\n", b"\xff\xfe\x00\x00",
])
def test_unknown_wildcard_dynamic_or_invalid_python_is_not_a_closed_upload(source, body):
    source = changed_source(source, ENTRY, body)
    with pytest.raises(ValueError):
        build(source)


def test_absolute_from_import_module_alias_has_exact_implicit_package_dependencies(source):
    source = changed_source(source, ENTRY, b"import bbag.contracts as names\nfrom bbag import jsonio\n")
    plan = build(source)
    assert next(file for file in plan.files if file.source_path == ENTRY).dependencies == [INIT, CONTRACTS, JSON]


def test_caller_explicit_byte_limits_apply_at_exact_boundary(source):
    maximum = max(len(BODIES[item.source_path]) for item in specs())
    total = sum(len(BODIES[item.source_path]) for item in specs())
    assert build(source, max_file_bytes=maximum, max_total_bytes=total).total_source_bytes == total
    with pytest.raises(ValueError, match="per-file"):
        build(source, max_file_bytes=maximum - 1)
    with pytest.raises(ValueError, match="total engineering"):
        build(source, max_total_bytes=total - 1)


@pytest.mark.parametrize("name,value", [("max_file_bytes", True), ("max_file_bytes", 0), ("max_file_bytes", 10.5),
    ("max_total_bytes", -1), ("max_total_bytes", None), ("extra_compile_calls", True), ("extra_compile_calls", -1),
    ("extra_compile_calls", "1")])
def test_limits_and_extra_compiles_are_strict_not_entitlements(source, name, value):
    with pytest.raises(ValueError):
        build(source, **{name: value})


@pytest.mark.parametrize("method", ["copy", "construct", "inplace"])
def test_unchecked_spec_instances_are_revalidated(source, method):
    selected = specs()
    if method == "copy":
        selected[0] = selected[0].model_copy(update={"dependencies": "not-a-list"})
    elif method == "construct":
        selected[0] = UploadSourceSpec.model_construct(source_path="../evil.py", remote_path="evil.py", dependencies=[])
    else:
        selected[0].dependencies.append(selected[0].dependencies[0])
    with pytest.raises(ValueError):
        build(source, selected=selected)


def test_unchecked_extra_field_cannot_be_silently_dropped_by_dump(source):
    selected = specs()
    selected[0] = selected[0].model_copy(update={"unexpected_copy_field": True})
    with pytest.raises(ValueError):
        build(source, selected=selected)
    plan = build(source).model_copy(update={"unexpected_copy_field": True})
    for operation in (canonical_upload_manifest_bytes, lambda value: read_upload_sources(source[0], value)):
        with pytest.raises(ValueError):
            operation(plan)


def test_tree_uses_immutable_requested_commit_during_real_head_aba(source, monkeypatch):
    root, first = source
    second_bytes = BODIES[JSON] + b"SYNTHETIC_FUTURE_B = True\n"
    _, second = changed_source(source, JSON, second_bytes)
    git(root, "reset", "--hard", first)
    git(root, "update-index", "--assume-unchanged", JSON)
    original_tree = upload_module._tree
    switched = []

    def head_aba(actual_root, *arguments):
        git(root, "update-ref", "HEAD", second)
        (root / JSON).write_bytes(second_bytes)
        try:
            switched.append(git(root, "rev-parse", "HEAD"))
            return original_tree(actual_root, *arguments)
        finally:
            git(root, "update-ref", "HEAD", first)

    monkeypatch.setattr(upload_module, "_tree", head_aba)
    with pytest.raises(ValueError, match="disk bytes differ"):
        build(source)
    assert switched == [second] and git(root, "rev-parse", "HEAD") == first
    assert git(root, "status", "--porcelain") == ""


@pytest.mark.parametrize("method", ["copy", "construct", "nested_inplace"])
def test_unchecked_manifests_cannot_set_authority_or_erase_compile_charge(source, method):
    plan = build(source)
    if method == "copy":
        plan = plan.model_copy(update={"executable": True})
    elif method == "construct":
        plan = UploadManifest.model_construct(**{**plan.model_dump(), "required_compile_increment": 0})
    else:
        object.__setattr__(plan.files[0], "compile_charge", False)
    for operation in (canonical_upload_manifest_bytes, upload_manifest_sha256, lambda value: read_upload_sources(source[0], value)):
        with pytest.raises(ValueError):
            operation(plan)


def test_source_payload_release_rechecks_declared_hash_and_real_git_mode(source):
    plan = build(source)
    for field, value in (("source_sha256", "0" * 64), ("git_blob", "0" * 40), ("git_mode", "100755")):
        tampered = deepcopy(plan)
        object.__setattr__(tampered.files[0], field, value)
        with pytest.raises(ValueError, match="differs from the exact"):
            read_upload_sources(source[0], tampered)


@pytest.mark.parametrize("field,value", [("current_compile_remaining", False), ("authorized_compile_increment", 1),
    ("available_compile_calls", 1), ("authorization_granted", 0), ("platform_execution", "PASS"),
    ("independent_review", "APPROVED"), ("remote_state", "EXISTS"), ("write_call_count", 1)])
def test_machine_contract_cannot_turn_plan_into_execution_or_grant(source, field, value):
    value_json = build(source).model_dump(mode="json")
    value_json[field] = value
    with pytest.raises(ValueError):
        UploadManifest.model_validate_json(json.dumps(value_json))
