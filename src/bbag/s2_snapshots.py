"""Guarded immutable local snapshots of explicitly synthetic daily inputs.

No Cloud transport, licensed-data persistence or Cloud/S1 relabeling is provided.
Each batch retains its own original bytes and source hash; no merged provider
snapshot is invented. Cache directories must be controlled by the application:
hashes detect corruption, not a hostile operator replacing all local evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from typing import Annotated, Callable, Literal, Self
import uuid

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from .contracts import StrictContract
from .s1_contracts import utc
from .s2_access import AccessGuard, DataClassification, DataRequest
from .s2_data import (StandardDataBundle, canonical_request_sha256, standardize_daily_json,
                      validate_snapshot_selection)


Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
Commit = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{40}$")]
VersionText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
MAX_RAW_BYTES = 4_000_000
MAX_CACHE_BYTES = 16_000_000
MAX_BATCHES = 32
MAX_BATCH_CELLS = 8192


class SnapshotError(ValueError):
    pass


class SnapshotConflictError(SnapshotError):
    """An existing identity cannot silently acquire revised bytes or clocks."""


class SnapshotIntegrityError(SnapshotError):
    pass


class SnapshotFetchError(SnapshotError):
    def __init__(self, category: Literal["TIMEOUT", "PROVIDER_ERROR"]) -> None:
        self.error_category = category
        super().__init__("synthetic snapshot fetch failed; no automatic retry")


def _require(value: bool, message: str) -> None:
    if not value:
        raise SnapshotError(message)


def _canonical(model: StrictContract) -> bytes:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class SnapshotIdentity(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    provider: Literal["SYNTHETIC"] = "SYNTHETIC"
    data_id: VersionText
    provider_data_version: VersionText
    calendar_id: VersionText
    calendar_version: VersionText
    request_sha256: Digest
    policy_sha256: Digest
    code_commit: Commit

    @field_validator("data_id", "provider_data_version", "calendar_id", "calendar_version")
    @classmethod
    def exact_version_text(cls, value: str) -> str:
        if value != value.strip() or any(c in value for c in "\r\n\0"):
            raise ValueError("snapshot version metadata must be exact bounded text")
        return value


def snapshot_key(identity: SnapshotIdentity) -> str:
    if not isinstance(identity, SnapshotIdentity):
        raise SnapshotError("a typed synthetic snapshot identity is required")
    return _sha(_canonical(SnapshotIdentity.model_validate(identity)))


def _parts(request: DataRequest, days: int, assets: int) -> list[DataRequest]:
    span = (request.end - request.start).days + 1
    _require(span * len(request.assets) <= MAX_BATCH_CELLS,
             "batch engineering workload exceeds 8192 calendar-date/asset cells")
    result = []
    for offset in range(0, span, days):
        start = request.start + timedelta(days=offset)
        end = request.start + timedelta(days=min(offset + days, span) - 1)
        for first in range(0, len(request.assets), assets):
            payload = request.model_dump(mode="python")
            payload.update(start=start, end=end, assets=request.assets[first:first + assets])
            result.append(DataRequest.model_validate(payload))
    _require(len(result) <= MAX_BATCHES, "batch plan exceeds 32 child requests")
    return result


class SnapshotBatchPlan(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["S2"] = "S2"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    original_request: DataRequest
    policy_sha256: Digest
    max_calendar_days: Annotated[int, Field(strict=True, ge=1, le=256)]
    assets_per_batch: Annotated[int, Field(strict=True, ge=1, le=32)]
    requests: list[DataRequest] = Field(min_length=1, max_length=MAX_BATCHES)

    @model_validator(mode="after")
    def complete_partition(self) -> Self:
        _require(self.original_request.provider == "SYNTHETIC" and self.original_request.output_mode == "LOCAL_DATA",
                 "local batch plans accept SYNTHETIC LOCAL_DATA only")
        expected = _parts(self.original_request, self.max_calendar_days, self.assets_per_batch)
        _require(self.requests == expected, "batch requests must be the exact complete ordered partition")
        return self


@dataclass(frozen=True)
class ProviderSnapshot:
    """Internal synthetic input, never a control-channel or Cloud data model."""
    raw_source: bytes
    retrieved_at: datetime
    data_id: str
    provider_data_version: str
    calendar_id: str
    calendar_version: str
    classification: DataClassification
    derived_from: tuple[DataClassification, ...] = ()
    provider: Literal["SYNTHETIC"] = "SYNTHETIC"


@dataclass(frozen=True)
class SnapshotResult:
    identity: SnapshotIdentity
    bundle: StandardDataBundle
    selection: DataRequest
    cache_hit: bool


@dataclass(frozen=True)
class SnapshotBatchResult:
    plan: SnapshotBatchPlan
    snapshots: tuple[SnapshotResult, ...]


class _StoredRecord(StrictContract):
    identity: SnapshotIdentity
    original_request: DataRequest
    raw_source: Annotated[bytes, Field(min_length=1, max_length=MAX_RAW_BYTES)]
    retrieved_at: AwareDatetime
    bundle: StandardDataBundle


class _StoredEnvelope(StrictContract):
    record: _StoredRecord
    record_sha256: Digest


def _authorized(original: DataRequest, selection: DataRequest | None, guard: AccessGuard,
                operation: Literal["query", "cache_read", "cache_write"]) -> tuple[DataRequest, DataRequest]:
    _require(isinstance(guard, AccessGuard), "a concrete S2 access guard is required")
    original = guard.authorize(original, operation=operation)
    selected = guard.authorize(selection if selection is not None else original, operation=operation)
    # These expected artifact labels are bound by the original request hash.
    # Loaded payload provenance is independently checked again after decoding.
    guard.authorize_artifact(original, classification=original.classification,
        derived_from=original.derived_from, operation=operation)
    guard.authorize_artifact(selected, classification=selected.classification,
        derived_from=selected.derived_from, operation=operation)
    _require(original.provider == selected.provider == "SYNTHETIC"
             and original.output_mode == selected.output_mode == "LOCAL_DATA",
             "local snapshots accept SYNTHETIC LOCAL_DATA only; Cloud persistence is not implemented")
    _require(original.start <= selected.start <= selected.end <= original.end
             and set(selected.assets).issubset(original.assets) and set(selected.fields).issubset(original.fields)
             and selected.purpose == original.purpose and selected.classification == original.classification
             and selected.derived_from == original.derived_from and utc(selected.as_of) <= utc(original.as_of),
             "selection must remain within the exact original snapshot request and knowledge cutoff")
    return original, selected


def _identity(identity: SnapshotIdentity, request: DataRequest, guard: AccessGuard) -> SnapshotIdentity:
    _require(isinstance(identity, SnapshotIdentity), "a typed synthetic snapshot identity is required")
    identity = SnapshotIdentity.model_validate(identity)
    _require(identity.request_sha256 == canonical_request_sha256(request)
             and identity.policy_sha256 == guard.policy_sha256,
             "snapshot identity differs from the exact original request or current policy")
    return identity


def make_snapshot_identity(request: DataRequest, guard: AccessGuard, *, data_id: str,
                           provider_data_version: str, calendar_id: str, calendar_version: str,
                           code_commit: str) -> SnapshotIdentity:
    original, _ = _authorized(request, None, guard, "query")
    return SnapshotIdentity(data_id=data_id, provider_data_version=provider_data_version,
        calendar_id=calendar_id, calendar_version=calendar_version, code_commit=code_commit,
        request_sha256=canonical_request_sha256(original), policy_sha256=guard.policy_sha256)


def plan_batches(request: DataRequest, guard: AccessGuard, *, max_calendar_days: int = 128,
                 assets_per_batch: int = 32) -> SnapshotBatchPlan:
    """Partition inclusive calendar dates without inventing trading sessions.

    Empty-session provider responses remain invalid under the existing bundle
    contract. Callers choose ranges with explicit sessions; bars=[] is valid.
    """
    original, _ = _authorized(request, None, guard, "query")
    _require(type(max_calendar_days) is int and 1 <= max_calendar_days <= 256
             and type(assets_per_batch) is int and 1 <= assets_per_batch <= 32,
             "batch dimensions must be bounded positive integers")
    children = _parts(original, max_calendar_days, assets_per_batch)
    guard.authorize_batch(children, operation="query")
    return SnapshotBatchPlan(original_request=original, policy_sha256=guard.policy_sha256,
        max_calendar_days=max_calendar_days, assets_per_batch=assets_per_batch, requests=children)


def _no_redirects(path: Path) -> None:
    for item in (path, *path.parents):
        if os.path.lexists(item):
            info = item.lstat()
            _require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                     "snapshot paths must not contain symlinks or reparse points")


def _decoded(raw: bytes) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            _require(key not in value, "duplicate snapshot JSON key")
            value[key] = item
        return value
    def finite(value):
        result = float(value)
        _require(math.isfinite(result), "nonfinite snapshot JSON")
        return result
    def forbidden(value):
        raise SnapshotError("nonfinite snapshot JSON")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_float=finite, parse_constant=forbidden)
    _require(type(value) is dict, "snapshot envelope must be one JSON object")
    return value


class SyntheticSnapshotStore:
    """One fixed digest filename per immutable request/version/policy/source identity."""

    def __init__(self, root: str | Path) -> None:
        _require(isinstance(root, (str, Path)), "snapshot root must be a filesystem path")
        self.root = Path(root).absolute()
        _require(".." not in self.root.parts, "snapshot root must not contain parent traversal")

    def path_for(self, identity: SnapshotIdentity) -> Path:
        # Pure metadata hashing does not access the filesystem or a provider.
        return self.root / (snapshot_key(identity) + ".json")

    def _read_bytes(self, path: Path) -> bytes | None:
        _no_redirects(self.root)
        if not self.root.exists():
            return None
        _require(self.root.is_dir(), "snapshot root must be an ordinary directory")
        _no_redirects(path)
        if not os.path.lexists(path):
            return None
        before = path.lstat()
        _require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_CACHE_BYTES,
                 "snapshot entry must be a bounded ordinary file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            _no_redirects(path)
            _require(stat.S_ISREG(opened.st_mode) and opened.st_size <= MAX_CACHE_BYTES
                     and (opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino),
                     "snapshot entry changed while being opened")
            raw = stream.read(MAX_CACHE_BYTES + 1)
        _require(len(raw) == opened.st_size and len(raw) <= MAX_CACHE_BYTES, "snapshot entry changed or exceeded its bound")
        return raw

    def _load(self, identity: SnapshotIdentity, original: DataRequest, guard: AccessGuard,
              selection: DataRequest | None = None) -> tuple[bytes, StandardDataBundle] | None:
        original, selected = _authorized(original, selection, guard, "cache_read")
        identity = _identity(identity, original, guard)
        raw = self._read_bytes(self.path_for(identity))
        if raw is None:
            return None
        try:
            _decoded(raw)
            envelope = _StoredEnvelope.model_validate_json(raw)
            record = envelope.record
            _require(_canonical(envelope) == raw and _sha(_canonical(record)) == envelope.record_sha256,
                     "snapshot canonical bytes/checksum mismatch")
            _require(record.identity == identity and record.original_request == original,
                     "snapshot original identity/request mismatch")
            bundle, _ = validate_snapshot_selection(record.bundle, selected, guard, operation="cache_read")
            _require(bundle.provenance.provider == "SYNTHETIC", "Cloud snapshots must never be loaded locally")
            expected = standardize_daily_json(record.raw_source, request=original, guard=guard,
                data_id=identity.data_id, provider_data_version=identity.provider_data_version,
                retrieved_at=record.retrieved_at, calendar_id=identity.calendar_id,
                calendar_version=identity.calendar_version, timezone=guard.policy.timezone)
            _require(bundle == expected, "snapshot raw bytes and standardized bundle disagree")
            return raw, bundle
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise SnapshotIntegrityError("stored synthetic snapshot failed integrity validation") from None

    def read(self, identity: SnapshotIdentity, original_request: DataRequest, guard: AccessGuard, *,
             selection: DataRequest | None = None) -> StandardDataBundle | None:
        """Return the original bundle; selection is checked, not substituted into provenance."""
        found = self._load(identity, original_request, guard, selection)
        return found[1] if found is not None else None

    def write(self, identity: SnapshotIdentity, original_request: DataRequest,
              snapshot: ProviderSnapshot, guard: AccessGuard) -> StandardDataBundle:
        original, _ = _authorized(original_request, None, guard, "cache_write")
        identity = _identity(identity, original, guard)
        _require(isinstance(snapshot, ProviderSnapshot) and snapshot.provider == "SYNTHETIC"
                 and type(snapshot.raw_source) is bytes and 1 <= len(snapshot.raw_source) <= MAX_RAW_BYTES,
                 "a bounded explicitly synthetic ProviderSnapshot is required")
        guard.authorize_artifact(original, classification=snapshot.classification,
            derived_from=snapshot.derived_from, operation="cache_write")
        _require(type(snapshot.derived_from) is tuple and snapshot.classification == original.classification
                 and list(snapshot.derived_from) == original.derived_from,
                 "provider snapshot classification/ancestry differs from the original request")
        _require((snapshot.data_id, snapshot.provider_data_version, snapshot.calendar_id, snapshot.calendar_version)
                 == (identity.data_id, identity.provider_data_version, identity.calendar_id, identity.calendar_version),
                 "provider snapshot version/calendar metadata differs from the requested identity")
        retrieved = utc(snapshot.retrieved_at)
        bundle = standardize_daily_json(snapshot.raw_source, request=original, guard=guard,
            data_id=identity.data_id, provider_data_version=identity.provider_data_version,
            retrieved_at=retrieved, calendar_id=identity.calendar_id, calendar_version=identity.calendar_version,
            timezone=guard.policy.timezone)
        bundle, _ = validate_snapshot_selection(bundle, original, guard, operation="cache_write")
        record = _StoredRecord(identity=identity, original_request=original,
            raw_source=snapshot.raw_source, retrieved_at=retrieved, bundle=bundle)
        raw = _canonical(_StoredEnvelope(record=record, record_sha256=_sha(_canonical(record))))
        _require(len(raw) <= MAX_CACHE_BYTES, "snapshot envelope exceeds its storage bound")
        existing = self._load(identity, original, guard)
        if existing is not None:
            if existing[0] != raw:
                raise SnapshotConflictError("snapshot identity already contains different data or metadata; use a new version")
            return existing[1]
        # Every authorization and payload check precedes directory/file creation.
        _authorized(original, None, guard, "cache_write")
        _identity(identity, original, guard)
        _no_redirects(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        _no_redirects(self.root)
        target = self.path_for(identity)
        pending = self.root / (".snapshot-pending-" + uuid.uuid4().hex + ".tmp")
        created = False
        try:
            with pending.open("xb") as stream:
                created = True
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            _no_redirects(self.root)
            try:
                os.link(pending, target, follow_symlinks=False)
            except FileExistsError:
                existing = self._load(identity, original, guard)
                if existing is None or existing[0] != raw:
                    raise SnapshotConflictError("snapshot publication conflicts with existing data or metadata") from None
        finally:
            if created:
                pending.unlink()
        # Reopen and validate the published immutable object, including races.
        published = self._load(identity, original, guard)
        _require(published is not None and published[0] == raw, "published snapshot bytes differ")
        return published[1]


class GuardedSyntheticDataService:
    """Cache first, otherwise fetch exactly once; never retry failed acquisition.

    code_commit is a caller-supplied identity, not Git authentication. A CLI
    must bind it to an exact clean source checkout before reusing snapshots.
    Completed child snapshots can remain after a later batch fetch fails;
    no aggregate success, rollback overwrite, or automatic retry is invented.
    """

    def __init__(self, store: SyntheticSnapshotStore, guard: AccessGuard, *, code_commit: str) -> None:
        _require(isinstance(store, SyntheticSnapshotStore) and isinstance(guard, AccessGuard), "typed store and guard are required")
        # Reuse the exact identity field validator without accessing a path.
        SnapshotIdentity(data_id="validation", provider_data_version="validation", calendar_id="validation",
            calendar_version="validation", request_sha256="0" * 64, policy_sha256="0" * 64, code_commit=code_commit)
        self.store, self.guard, self.code_commit = store, guard, code_commit

    def get(self, original_request: DataRequest, *, data_id: str, provider_data_version: str,
            calendar_id: str, calendar_version: str, fetch: Callable[[DataRequest], ProviderSnapshot],
            selection: DataRequest | None = None) -> SnapshotResult:
        original, selected = _authorized(original_request, selection, self.guard, "query")
        identity = make_snapshot_identity(original, self.guard, data_id=data_id, provider_data_version=provider_data_version,
            calendar_id=calendar_id, calendar_version=calendar_version, code_commit=self.code_commit)
        _require(callable(fetch), "a synthetic snapshot callback is required")
        cached = self.store.read(identity, original, self.guard, selection=selected)
        if cached is not None:
            return SnapshotResult(identity, cached, selected, True)
        _authorized(original, selected, self.guard, "query")
        _identity(identity, original, self.guard)
        try:
            # Give the callback a separate object; its mutations cannot change
            # the identity, selection or immutable request used by this call.
            snapshot = fetch(original.model_copy(deep=True))
        except TimeoutError:
            raise SnapshotFetchError("TIMEOUT") from None
        except Exception:
            raise SnapshotFetchError("PROVIDER_ERROR") from None
        bundle = self.store.write(identity, original, snapshot, self.guard)
        validate_snapshot_selection(bundle, selected, self.guard, operation="query")
        return SnapshotResult(identity, bundle, selected, False)

    def get_batches(self, plan: SnapshotBatchPlan, *, data_id: str, provider_data_version: str,
                    calendar_id: str, calendar_version: str,
                    fetch: Callable[[DataRequest], ProviderSnapshot]) -> SnapshotBatchResult:
        _require(isinstance(plan, SnapshotBatchPlan), "a typed complete batch plan is required")
        plan = SnapshotBatchPlan.model_validate(plan)
        _authorized(plan.original_request, None, self.guard, "query")
        _require(plan.policy_sha256 == self.guard.policy_sha256, "batch policy differs from the current policy")
        # Full request and identity preflight before the first cache read/fetch.
        self.guard.authorize_batch(plan.requests, operation="query")
        for child in plan.requests:
            make_snapshot_identity(child, self.guard, data_id=data_id, provider_data_version=provider_data_version,
                calendar_id=calendar_id, calendar_version=calendar_version, code_commit=self.code_commit)
        results = tuple(self.get(child, data_id=data_id, provider_data_version=provider_data_version,
            calendar_id=calendar_id, calendar_version=calendar_version, fetch=fetch) for child in plan.requests)
        return SnapshotBatchResult(plan, results)


# Raw ProviderSnapshot, stored payloads and in-memory data results are internal;
# registering these metadata-only contracts does not create a data export path.
S2_SNAPSHOT_CONTRACTS = {model.__name__: model for model in (SnapshotIdentity, SnapshotBatchPlan)}
