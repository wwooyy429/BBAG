"""Offline exact-source upload planning; no provider access or budget grant.

FULL_FRESH means every file in the declared static import closure is included.
Remote existence is unknown. Each future create/edit choice charges one compile;
the generated proposal never authorizes that write or changes the old budget.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import keyword
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from .contracts import StrictContract

SourcePath = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Commit = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{40}$")]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
ByteLimit = Annotated[int, Field(strict=True, gt=0, le=256_000_000)]
ByteCount = Annotated[int, Field(strict=True, ge=0, le=256_000_000)]
_SDK_IMPORTS = frozenset({"AlgorithmImports", "QuantConnect", "pydantic"})
_DYNAMIC = frozenset({"__import__", "exec", "eval", "import_module", "exec_module", "load_module", "spec_from_file_location", "module_from_spec"})
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _path(value: str, *, source: bool) -> str:
    parts = PurePosixPath(value).parts
    if (not parts or PurePosixPath(value).is_absolute() or PurePosixPath(value).as_posix() != value
            or "\\" in value or ":" in value or any(p in {".", ".."} for p in parts)
            or not value.endswith(".py")):
        raise ValueError("source and remote paths must be canonical relative Python paths")
    for index, part in enumerate(parts):
        name = part[:-3] if index == len(parts) - 1 else part
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None or keyword.iskeyword(name) or name.upper() in _RESERVED:
            raise ValueError("unsafe or ambiguous Python path component")
    if source and not (value.startswith("src/bbag/") or value.startswith("cloud/")):
        raise ValueError("only explicitly named own bbag/cloud source may be planned")
    return value


def _remote(source: str) -> str:
    return "main.py" if source == "cloud/main.py" else source.removeprefix("src/")


class UploadSourceSpec(StrictContract):
    source_path: SourcePath
    remote_path: SourcePath
    dependencies: list[SourcePath] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def paths(self) -> Self:
        _path(self.source_path, source=True)
        _path(self.remote_path, source=False)
        if self.remote_path != _remote(self.source_path):
            raise ValueError("remote layout must preserve the own Python module identity")
        if len(self.dependencies) != len(set(self.dependencies)) or self.source_path in self.dependencies:
            raise ValueError("source dependencies must be unique and cannot name the file itself")
        for value in self.dependencies:
            _path(value, source=True)
        return self


class UploadFile(UploadSourceSpec):
    source_commit: Commit
    git_blob: Commit
    git_mode: Literal["100644", "100755"]
    source_sha256: Digest
    size_bytes: ByteCount
    order: Annotated[int, Field(strict=True, ge=1, le=64)]
    write_mode: Literal["CREATE_OR_EDIT_AFTER_REMOTE_PREFLIGHT"] = "CREATE_OR_EDIT_AFTER_REMOTE_PREFLIGHT"
    compile_charge: Literal[1] = 1

    @field_validator("compile_charge", mode="before")
    @classmethod
    def integer_charge(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("compile charge must be an explicit integer")
        return value


class UploadManifest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    source_commit: Commit
    profile: Literal["FULL_FRESH"] = "FULL_FRESH"
    remote_state: Literal["UNKNOWN_NOT_QUERIED"] = "UNKNOWN_NOT_QUERIED"
    executable: Literal[False] = False
    authorization_granted: Literal[False] = False
    budget_change_applied: Literal[False] = False
    platform_execution: Literal["NOT_RUN"] = "NOT_RUN"
    independent_review: Literal["PENDING"] = "PENDING"
    economic_evidence: Literal["NOT_APPLICABLE"] = "NOT_APPLICABLE"
    limit_kind: Literal["CALLER_ENGINEERING_LIMIT_NOT_PROVIDER_ENTITLEMENT"] = "CALLER_ENGINEERING_LIMIT_NOT_PROVIDER_ENTITLEMENT"
    max_file_bytes: ByteLimit
    max_total_bytes: ByteLimit
    total_source_bytes: ByteCount
    entrypoints: list[SourcePath] = Field(min_length=1, max_length=16)
    files: list[UploadFile] = Field(min_length=1, max_length=64)
    external_import_roots: list[Annotated[str, Field(strict=True, min_length=1)]] = Field(max_length=128)
    historical_compile_used: Literal[2] = 2
    historical_compile_cap: Literal[2] = 2
    current_compile_remaining: Literal[0] = 0
    authorized_compile_increment: Literal[0] = 0
    available_compile_calls: Literal[0] = 0
    write_call_count: Count
    extra_compile_calls: Count = 0
    required_compile_increment: Count
    proposed_compile_total_cap: Count

    @field_validator("executable", "authorization_granted", "budget_change_applied", mode="before")
    @classmethod
    def explicit_bool(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("non-executable plan flags must be explicit booleans")
        return value

    @field_validator("historical_compile_used", "historical_compile_cap", "current_compile_remaining",
                     "authorized_compile_increment", "available_compile_calls", mode="before")
    @classmethod
    def explicit_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("budget declarations must be explicit integers")
        return value

    @model_validator(mode="after")
    def consistent(self) -> Self:
        paths = [file.source_path for file in self.files]
        remotes = [file.remote_path.casefold() for file in self.files]
        if (len(paths) != len(set(p.casefold() for p in paths)) or len(remotes) != len(set(remotes))
                or len(self.entrypoints) != len(set(self.entrypoints)) or not set(self.entrypoints) <= set(paths)):
            raise ValueError("upload files and entrypoints must have unique noncolliding identities")
        seen = set()
        for order, file in enumerate(self.files, 1):
            if file.order != order or file.source_commit != self.source_commit or not set(file.dependencies) <= seen:
                raise ValueError("upload order/commit/dependencies are inconsistent")
            if file.size_bytes > self.max_file_bytes:
                raise ValueError("source exceeds the caller-declared per-file bound")
            seen.add(file.source_path)
        if (self.total_source_bytes != sum(file.size_bytes for file in self.files)
                or self.total_source_bytes > self.max_total_bytes
                or self.write_call_count != len(self.files)
                or self.required_compile_increment != self.write_call_count + self.extra_compile_calls
                or self.proposed_compile_total_cap != 2 + self.required_compile_increment):
            raise ValueError("upload byte or m+e compile arithmetic is inconsistent")
        graph = {file.source_path: set(file.dependencies) for file in self.files}
        reachable, pending = set(), list(self.entrypoints)
        while pending:
            current = pending.pop()
            if current not in reachable:
                reachable.add(current)
                pending.extend(graph[current])
        if reachable != set(paths):
            raise ValueError("FULL_FRESH must be the exact entrypoint dependency closure, without unrelated files")
        if self.external_import_roots != sorted(set(self.external_import_roots)):
            raise ValueError("external imports must be sorted and unique")
        return self


def _ordinary(path: Path, *, directory: bool = False) -> Path:
    path = path.absolute()
    for item in (path, *path.parents):
        if os.path.lexists(item):
            meta = item.lstat()
            if stat.S_ISLNK(meta.st_mode) or getattr(meta, "st_file_attributes", 0) & 0x400:
                raise ValueError("source symlinks/reparse points are forbidden")
    if directory:
        if not path.is_dir():
            raise ValueError("source root must be a directory")
    elif not path.is_file() or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("source must be an ordinary file")
    return path.resolve()


def _git(root: Path, *arguments: str, data: bytes | None = None) -> bytes:
    try:
        return subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.quotepath=false", *arguments],
            cwd=root, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    except subprocess.CalledProcessError:
        raise ValueError("exact committed Git source could not be verified") from None


def _tree(root: Path, commit: str) -> dict[str, tuple[str, str, str]]:
    result = {}
    for item in _git(root, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if item:
            meta, path = item.split(b"\t", 1)
            mode, kind, identity = meta.decode("ascii").split()
            result[path.decode("utf-8")] = (mode, kind, identity)
    return result


def _module(path: str) -> str:
    remote = _remote(path)
    return remote[:-12].replace("/", ".") if remote.endswith("/__init__.py") else remote[:-3].replace("/", ".")


def _imports(path: str, raw: bytes, modules: dict[str, str]) -> tuple[set[str], set[str]]:
    try:
        code = ast.parse(raw.decode("utf-8"), filename=path)
    except (UnicodeError, SyntaxError, ValueError):
        raise ValueError("upload source must be valid UTF-8 Python") from None
    own, external = set(), set()
    current = _module(path)
    package = current if path.endswith("/__init__.py") else current.rpartition(".")[0]

    def packages(name: str):
        parts = name.split(".")
        for i in range(1, len(parts)):
            candidate = modules.get(".".join(parts[:i]))
            if candidate and candidate.endswith("/__init__.py"):
                own.add(candidate)

    def resolve(name: str, *, namespace: bool = False):
        root_name = name.split(".")[0]
        if root_name in {"bbag", "cloud"}:
            packages(name)
            if name in modules:
                own.add(modules[name])
            elif not namespace or not any(n.startswith(name + ".") for n in modules):
                raise ValueError("unresolved committed own import: " + name)
        else:
            if root_name in {"importlib", "runpy"}:
                raise ValueError("dynamic import machinery is outside the static upload contract")
            if root_name not in sys.stdlib_module_names and root_name not in _SDK_IMPORTS:
                raise ValueError("unreviewed external import root: " + root_name)
            external.add(root_name)

    packages(current)
    if current.startswith("bbag.") and "bbag" not in modules:
        raise ValueError("bbag package initializer is required")
    for node in ast.walk(code):
        if isinstance(node, ast.Call) and (isinstance(node.func, ast.Name) and node.func.id in _DYNAMIC
                or isinstance(node.func, ast.Attribute) and node.func.attr in _DYNAMIC):
            raise ValueError("dynamic code/import calls cannot define a static upload closure")
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolve(alias.name)
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" or alias.name in _DYNAMIC for alias in node.names):
                raise ValueError("wildcard/dynamic imports cannot define an exact upload closure")
            if node.level:
                prefix = package.split(".") if package else []
                if node.level > len(prefix):
                    raise ValueError("relative import escapes the own package")
                base = ".".join(prefix[:len(prefix) - node.level + 1])
                name = base + ("." + node.module if node.module else "")
            else:
                name = node.module or ""
            resolve(name, namespace=True)
            for alias in node.names:
                if name + "." + alias.name in modules:
                    resolve(name + "." + alias.name)
    if path.endswith("/__init__.py"):
        own.discard(path)
    return own, external


def _checked_spec(value: UploadSourceSpec) -> UploadSourceSpec:
    if type(value) is not UploadSourceSpec:
        raise ValueError("source specifications must be UploadSourceSpec instances")
    validated = UploadSourceSpec.model_validate(value)
    return UploadSourceSpec.model_validate(validated.model_dump(mode="python", warnings=False))


def _checked_manifest(value: UploadManifest) -> UploadManifest:
    if type(value) is not UploadManifest:
        raise ValueError("manifest must be an UploadManifest instance")
    validated = UploadManifest.model_validate(value)
    return UploadManifest.model_validate(validated.model_dump(mode="python", warnings=False))


def build_upload_manifest(root: str | Path, commit: str, sources: list[UploadSourceSpec], entrypoints: list[str], *,
                          max_file_bytes: int, max_total_bytes: int, extra_compile_calls: int = 0) -> UploadManifest:
    """Read a clean exact HEAD and generate a proposal; perform no writes/calls."""
    root = _ordinary(Path(root), directory=True)
    if type(commit) is not str or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("source commit must be a full lowercase SHA")
    if type(sources) is not list or not 1 <= len(sources) <= 64 or type(entrypoints) is not list:
        raise ValueError("provide bounded explicit source and entrypoint lists")
    specs = [_checked_spec(item) for item in sources]
    for value in (max_file_bytes, max_total_bytes):
        if type(value) is not int or not 1 <= value <= 256_000_000:
            raise ValueError("explicit positive engineering byte limits are required")
    if type(extra_compile_calls) is not int or not 0 <= extra_compile_calls <= 1_000_000 - 66:
        raise ValueError("extra compile calls must be a bounded explicit integer")
    paths = [item.source_path for item in specs]
    if len(paths) != len(set(p.casefold() for p in paths)) or len(specs) != len({s.remote_path.casefold() for s in specs}):
        raise ValueError("duplicate or case-colliding upload paths")
    if any(type(p) is not str for p in entrypoints) or not entrypoints or len(entrypoints) != len(set(entrypoints)) or not set(entrypoints) <= set(paths):
        raise ValueError("entrypoints must uniquely name explicit selected source files")
    if (Path(_git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve() != root
            or _git(root, "rev-parse", "HEAD").decode("ascii").strip() != commit
            or _git(root, "status", "--porcelain", "--untracked-files=normal").strip()):
        raise ValueError("upload planning requires the exact clean Git checkout root and HEAD")
    tracked = _tree(root, commit)
    modules = {}
    for name, (mode, kind, _) in tracked.items():
        if name.endswith(".py") and name.startswith(("src/bbag/", "cloud/")):
            logical = _module(name)
            if logical in modules:
                raise ValueError("ambiguous committed Python module paths")
            modules[logical] = name
    for name in paths:
        if name not in tracked or tracked[name][0] not in {"100644", "100755"} or tracked[name][1] != "blob":
            raise ValueError("selected source must be a committed regular Git blob")
    batch = _git(root, "cat-file", "--batch", data=("\n".join(tracked[p][2] for p in paths) + "\n").encode("ascii"))
    stream, bodies, dependencies, external = io.BytesIO(batch), {}, {}, set()
    for spec in specs:
        name = spec.source_path
        header = stream.readline().decode("ascii").strip().split()
        if len(header) != 3 or header[:2] != [tracked[name][2], "blob"]:
            raise ValueError("unexpected Git blob response")
        body = stream.read(int(header[2]))
        if stream.read(1) != b"\n" or _ordinary(root / name).read_bytes() != body:
            raise ValueError("selected disk bytes differ from committed source, including hidden Git flags")
        if len(body) > max_file_bytes:
            raise ValueError("source exceeds the explicit per-file engineering limit")
        bodies[name] = body
        own, imports = _imports(name, body, modules)
        if own != set(spec.dependencies):
            raise ValueError("declared dependencies differ from static imports for " + name + ": " + ",".join(sorted(own)))
        if not own <= set(paths):
            raise ValueError("FULL_FRESH omits required committed own dependencies")
        dependencies[name] = own
        external.update(imports)
    if stream.read():
        raise ValueError("unexpected trailing Git blob bytes")
    if sum(map(len, bodies.values())) > max_total_bytes:
        raise ValueError("source set exceeds the explicit total engineering limit")
    ordered = []
    while len(ordered) < len(paths):
        ready = sorted(p for p in paths if p not in ordered and dependencies[p] <= set(ordered))
        if not ready:
            raise ValueError("cyclic upload dependencies are forbidden")
        ordered.extend(ready)
    by_path = {item.source_path: item for item in specs}
    files = [UploadFile(**{**by_path[name].model_dump(), "dependencies": sorted(dependencies[name])}, source_commit=commit, git_blob=tracked[name][2],
        git_mode=tracked[name][0], source_sha256=hashlib.sha256(bodies[name]).hexdigest(),
        size_bytes=len(bodies[name]), order=i) for i, name in enumerate(ordered, 1)]
    manifest = UploadManifest(source_commit=commit, max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes,
        total_source_bytes=sum(map(len, bodies.values())), entrypoints=sorted(entrypoints), files=files,
        external_import_roots=sorted(external), write_call_count=len(files), extra_compile_calls=extra_compile_calls,
        required_compile_increment=len(files) + extra_compile_calls, proposed_compile_total_cap=2 + len(files) + extra_compile_calls)
    if (_git(root, "rev-parse", "HEAD").decode("ascii").strip() != commit
            or _git(root, "status", "--porcelain", "--untracked-files=normal").strip()
            or any(_ordinary(root / name).read_bytes() != raw for name, raw in bodies.items())):
        raise ValueError("source changed during offline upload planning")
    return manifest


def canonical_upload_manifest_bytes(manifest: UploadManifest) -> bytes:
    checked = _checked_manifest(manifest)
    return json.dumps(checked.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def upload_manifest_sha256(manifest: UploadManifest) -> str:
    return hashlib.sha256(canonical_upload_manifest_bytes(manifest)).hexdigest()


def read_upload_sources(root: str | Path, manifest: UploadManifest) -> dict[str, bytes]:
    """Return only reverified local own source bytes; never upload anything."""
    checked = _checked_manifest(manifest)
    specs = [UploadSourceSpec(source_path=f.source_path, remote_path=f.remote_path, dependencies=f.dependencies) for f in checked.files]
    rebuilt = build_upload_manifest(root, checked.source_commit, specs, checked.entrypoints,
        max_file_bytes=checked.max_file_bytes, max_total_bytes=checked.max_total_bytes, extra_compile_calls=checked.extra_compile_calls)
    if rebuilt != checked:
        raise ValueError("upload manifest differs from the exact committed source")
    result = {}
    for file in rebuilt.files:
        raw = _ordinary(Path(root) / file.source_path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != file.source_sha256 or len(raw) != file.size_bytes:
            raise ValueError("source changed before releasing own upload bytes")
        result[file.remote_path] = raw
    root = _ordinary(Path(root), directory=True)
    if (_git(root, "rev-parse", "HEAD").decode("ascii").strip() != checked.source_commit
            or _git(root, "status", "--porcelain", "--untracked-files=normal").strip()
            or any(_ordinary(root / file.source_path).read_bytes() != result[file.remote_path] for file in rebuilt.files)):
        raise ValueError("source changed while releasing the captured upload bytes")
    return result


S2_UPLOAD_CONTRACTS = {model.__name__: model for model in (UploadSourceSpec, UploadFile, UploadManifest)}
