"""Independent synthetic snapshot/cache expectations; no provider or Cloud I/O."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from bbag.contracts import TimeRange
from bbag.s2_access import AccessGuard, AccessPolicy, DataRequest, STANDARD_FIELDS, canonical_policy_sha256
from bbag.s2_data import canonical_request_sha256, data_quality
from bbag.s2_snapshots import (GuardedSyntheticDataService, ProviderSnapshot, SnapshotBatchPlan,
    SnapshotConflictError, SnapshotError, SnapshotFetchError, SnapshotIdentity, SnapshotIntegrityError,
    SyntheticSnapshotStore, make_snapshot_identity, plan_batches, snapshot_key)


DAYS = [date(2024, 1, n) for n in (2, 3, 4, 5)]
META = dict(data_id="synthetic-local-dataset", provider_data_version="fixture-v1",
            calendar_id="four-explicit-synthetic-days", calendar_version="calendar-v1")
CODE = "a" * 40  # Synthetic source identity, never a real implementation commit.


def at(day, hour=22, minute=0):
    return datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=hour, minutes=minute)


def request(**updates):
    value = dict(provider="SYNTHETIC", start=DAYS[0], end=DAYS[-1], assets=["SID_A", "SID_B"],
                 fields=list(STANDARD_FIELDS), as_of=at(DAYS[-1]), classification="DEVELOPMENT")
    value.update(updates)
    return DataRequest(**value)


def guard(**updates):
    value = dict(policy_id="synthetic-cache-policy", provider="SYNTHETIC",
        development=TimeRange(start=DAYS[0], end=DAYS[-1]),
        sealed=[TimeRange(start="2024-01-06", end="2024-02-01")],
        allowed_assets=["SID_A", "SID_B"], allowed_fields=list(STANDARD_FIELDS))
    value.update(updates)
    return AccessGuard(AccessPolicy(**value))


def raw(req=None):
    req = req or request()
    days = [day for day in DAYS if req.start <= day <= req.end]
    value = dict(sessions=[dict(session=d.isoformat(), open_at=at(d, 14, 30).isoformat(),
        close_at=at(d, 21).isoformat(), signal_at=at(d, 21, 5).isoformat()) for d in days],
        identities=[dict(sid=s, listed_from=DAYS[0].isoformat(), known_at=at(date(2024, 1, 1)).isoformat()) for s in req.assets],
        tickers=[dict(sid=s, ticker=s, valid_from=DAYS[0].isoformat(), known_at=at(date(2024, 1, 1)).isoformat()) for s in req.assets],
        membership=[dict(sid=s, effective_on=DAYS[0].isoformat(), member=True,
                         known_at=at(date(2024, 1, 1)).isoformat()) for s in req.assets],
        corporate_actions=[], bars=[])
    for day in days:
        for sid in req.assets:
            close = (10.0 if sid == "SID_A" else 20.0) + DAYS.index(day)
            bar = dict(sid=sid, session=day.isoformat(), available_at=at(day, 21, 1).isoformat())
            bar.update({field: (100.0 if field == "volume" else close + 1 if field == "high" else close - 1 if field == "low" else close)
                        for field in req.fields})
            value["bars"].append(bar)
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def provider(req=None, **updates):
    req = req or request()
    value = dict(raw_source=raw(req), retrieved_at=at(DAYS[-1], 23), classification="DEVELOPMENT", **META)
    value.update(updates)
    return ProviderSnapshot(**value)


def identity(req=None, access=None, **updates):
    value = dict(META, code_commit=CODE)
    value.update(updates)
    return make_snapshot_identity(req or request(), access or guard(), **value)


def service(tmp_path, access=None, code=CODE):
    return GuardedSyntheticDataService(SyntheticSnapshotStore(tmp_path / "cache"), access or guard(), code_commit=code)


def get(svc, req=None, callback=None, **updates):
    value = dict(META, fetch=callback or provider)
    value.update(updates)
    return svc.get(req or request(), **value)


def test_exact_identity_cache_hit_restart_and_original_bytes(tmp_path):
    svc = service(tmp_path)
    calls = []
    def fetch(req):
        calls.append(req)
        return provider(req)
    first = get(svc, callback=fetch)
    assert first.cache_hit is False and len(calls) == 1
    assert first.bundle.provenance.provider == "SYNTHETIC"
    assert first.bundle.provenance.source_sha256 == hashlib.sha256(raw()).hexdigest()
    assert [row.close for row in first.bundle.bars] == [10., 20., 11., 21., 12., 22., 13., 23.]
    path = svc.store.path_for(first.identity)
    before = path.read_bytes()
    stamp = path.stat().st_mtime_ns
    reopened = service(tmp_path)
    second = get(reopened, callback=lambda _: pytest.fail("cache hit must not fetch"))
    assert second.cache_hit and second.bundle == first.bundle
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    assert re.fullmatch(r"[0-9a-f]{64}\.json", path.name)
    assert list(path.parent.iterdir()) == [path]


def test_duplicate_write_is_immutable_and_identical_content_is_reused(tmp_path):
    store, access, req, key = SyntheticSnapshotStore(tmp_path / "cache"), guard(), request(), identity()
    one = store.write(key, req, provider(), access)
    path = store.path_for(key); original, stamp = path.read_bytes(), path.stat().st_mtime_ns
    two = store.write(key, req, provider(), access)
    assert one == two and path.read_bytes() == original and path.stat().st_mtime_ns == stamp


@pytest.mark.parametrize("revision", ["raw_bytes", "retrieval_clock"])
def test_same_key_revision_never_overwrites_even_when_raw_hash_is_same(tmp_path, revision):
    store, access, req, key = SyntheticSnapshotStore(tmp_path / "cache"), guard(), request(), identity()
    store.write(key, req, provider(), access); path = store.path_for(key); original = path.read_bytes()
    if revision == "raw_bytes":
        changed = json.loads(raw()); changed["bars"][0].update(open=15., high=16., low=14., close=15.)
        incoming = provider(raw_source=json.dumps(changed).encode())
    else:
        incoming = provider(retrieved_at=at(DAYS[-1], 23, 1))
    with pytest.raises(SnapshotConflictError):
        store.write(key, req, incoming, access)
    assert path.read_bytes() == original and store.read(key, req, access).bars[0].close == 10.


@pytest.mark.parametrize("field,new", [("data_id", "other-data"), ("provider_data_version", "fixture-v2"),
    ("calendar_id", "other-calendar"), ("calendar_version", "calendar-v2"), ("code_commit", "b" * 40)])
def test_every_declared_version_dimension_changes_the_key(field, new):
    old, changed = identity(), identity(**{field: new})
    assert snapshot_key(old) != snapshot_key(changed)
    assert snapshot_key(old) == hashlib.sha256(json.dumps(old.model_dump(mode="json"), sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def test_request_and_policy_identity_are_exact_and_metadata_never_becomes_a_path(tmp_path):
    old = identity()
    assert snapshot_key(old) != snapshot_key(identity(request(as_of=at(DAYS[-1], 23))))
    assert snapshot_key(old) != snapshot_key(identity(request(fields=["close"])))
    assert snapshot_key(old) != snapshot_key(identity(request(assets=["SID_B", "SID_A"])))
    changed_guard = guard(policy_id="policy-v2")
    assert snapshot_key(old) != snapshot_key(identity(access=changed_guard))
    tricky = identity(data_id="../../somewhere/else", provider_data_version="revision/../v2")
    path = SyntheticSnapshotStore(tmp_path / "cache").path_for(tricky)
    assert path.parent == tmp_path / "cache" and re.fullmatch(r"[0-9a-f]{64}\.json", path.name)


def test_prefix_selection_keeps_original_snapshot_identity_and_provenance(tmp_path):
    svc = service(tmp_path)
    first = get(svc)
    selection = request(end=DAYS[1], assets=["SID_A"], fields=["close"], as_of=at(DAYS[1]))
    selected = get(svc, selection=selection, callback=lambda _: pytest.fail("selection reuses the original exact snapshot"))
    assert selected.cache_hit and selected.selection == selection
    assert selected.bundle.request == request() and selected.bundle == first.bundle
    assert selected.bundle.provenance.source_sha256 == hashlib.sha256(raw()).hexdigest()
    assert selected.identity == first.identity
    with pytest.raises(ValueError):
        svc.store.read(first.identity, selection, svc.guard)  # selection must not become the original key request


@pytest.mark.parametrize("change", [dict(start=date(2024, 1, 1)), dict(end=date(2024, 1, 6)),
    dict(assets=["UNKNOWN"]), dict(classification="UNKNOWN"), dict(classification="SEALED_FINAL"),
    dict(derived_from=["SEALED_DERIVATIVE"]), dict(output_mode="CONTROL_ONLY")])
def test_rejected_request_cannot_read_cache_create_paths_or_fetch(tmp_path, monkeypatch, change):
    svc = service(tmp_path)
    def unexpected(_):
        pytest.fail("cache filesystem must not be read before request authorization")
    monkeypatch.setattr(svc.store, "_read_bytes", unexpected)
    with pytest.raises(ValueError):
        get(svc, request(**change), callback=lambda _: pytest.fail("rejected request must not fetch"))
    assert not svc.store.root.exists()


def test_cloud_provider_is_refused_even_with_a_valid_trusted_cloud_guard(tmp_path, monkeypatch):
    payload = guard().policy.model_dump(mode="python")
    payload.update(provider="QUANTCONNECT_CLOUD", authorization="OWNER_AUTHORIZED", approval_reference="SYNTHETIC GRANT TEST ONLY",
                   approved_by="SYNTHETIC OWNER", approved_at=at(DAYS[0]))
    policy = AccessPolicy.model_validate(payload)
    access = AccessGuard(policy, trusted_policy_sha256s=frozenset({canonical_policy_sha256(policy)}), execution_location="QUANTCONNECT_CLOUD")
    svc = service(tmp_path, access)
    monkeypatch.setattr(svc.store, "_read_bytes", lambda _: pytest.fail("Cloud must never reach local filesystem"))
    with pytest.raises(ValueError, match="SYNTHETIC"):
        get(svc, request(provider="QUANTCONNECT_CLOUD", output_mode="IN_CLOUD_DATA"), callback=lambda _: pytest.fail("no Cloud callback"))
    assert not svc.store.root.exists()


@pytest.mark.parametrize("taint", [dict(classification="UNKNOWN"), dict(classification="SEALED_FINAL"),
                                  dict(derived_from=("SEALED_DERIVATIVE",)), dict(provider="QUANTCONNECT_CLOUD")])
def test_actual_callback_artifact_cannot_be_relabelled_as_development(tmp_path, taint):
    svc, calls = service(tmp_path), []
    def callback(req):
        calls.append(req)
        return provider(req, **taint)
    with pytest.raises(ValueError):
        get(svc, callback=callback)
    assert len(calls) == 1 and not svc.store.root.exists()


def test_callback_request_mutation_cannot_rewrite_identity_or_selected_scope(tmp_path):
    svc = service(tmp_path)
    def callback(req):
        original = provider(req)
        req.assets[:] = ["INJECTED"]
        object.__setattr__(req, "start", date(1999, 1, 1))
        return original
    result = get(svc, callback=callback)
    assert result.bundle.request == request() and result.selection == request()
    assert result.identity.request_sha256 == canonical_request_sha256(request())


def test_policy_change_or_wrong_original_identity_is_rejected_before_disk_read(tmp_path, monkeypatch):
    store = SyntheticSnapshotStore(tmp_path / "cache")
    key = identity()
    monkeypatch.setattr(store, "_read_bytes", lambda _: pytest.fail("wrong identity must not read cache"))
    with pytest.raises(ValueError):
        store.read(key, request(), guard(policy_id="revoked-and-replaced"))
    with pytest.raises(ValueError):
        store.read(key, request(assets=["SID_A"]), guard())


def test_advancing_selection_clock_is_rejected_before_cache_io(tmp_path, monkeypatch):
    store = SyntheticSnapshotStore(tmp_path / "cache")
    monkeypatch.setattr(store, "_read_bytes", lambda _: pytest.fail("selection cannot advance snapshot knowledge"))
    with pytest.raises(ValueError):
        store.read(identity(), request(), guard(), selection=request(as_of=at(date(2024, 1, 6))))


def test_callback_version_mismatch_does_not_enter_a_different_cache_namespace(tmp_path):
    svc, calls = service(tmp_path), []
    def callback(req):
        calls.append(req)
        return provider(req, provider_data_version="unexpected-revision")
    with pytest.raises(ValueError, match="version/calendar"):
        get(svc, callback=callback)
    assert len(calls) == 1 and not svc.store.root.exists()


def test_mutated_return_value_cannot_change_the_persisted_snapshot(tmp_path):
    svc = service(tmp_path); result = get(svc)
    result.bundle.bars[0].close = 10.5
    result.bundle.request.assets[:] = ["INJECTED"]
    fresh = get(svc, callback=lambda _: pytest.fail("cache remains valid independently of returned object"))
    assert fresh.cache_hit and fresh.bundle.bars[0].close == 10.0 and fresh.bundle.request == request()


def test_raw_and_stored_file_size_bounds_fail_without_fetch_retry(tmp_path):
    svc, calls = service(tmp_path), []
    def callback(req):
        calls.append(req); return provider(req, raw_source=b" " * 4_000_001)
    with pytest.raises(ValueError): get(svc, callback=callback)
    assert len(calls) == 1 and not svc.store.root.exists()
    key = identity(); path = svc.store.path_for(key); path.parent.mkdir()
    with path.open("wb") as stream: stream.truncate(16_000_001)
    with pytest.raises(ValueError): get(svc, callback=lambda _: pytest.fail("oversized cache is never a cache miss"))


def rewrite_envelope(path, mutate):
    payload = json.loads(path.read_bytes())
    mutate(payload)
    payload["record_sha256"] = hashlib.sha256(json.dumps(payload["record"], sort_keys=True, ensure_ascii=False,
                                                        separators=(",", ":")).encode()).hexdigest()
    path.write_bytes(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())


@pytest.mark.parametrize("fault", ["corrupt", "bundle_rehashed", "raw_rehashed", "identity_rehashed", "duplicate_json"])
def test_corrupt_cache_never_falls_back_to_fetch_or_silently_revalidates_bad_bytes(tmp_path, fault):
    svc = service(tmp_path); result = get(svc); path = svc.store.path_for(result.identity)
    if fault == "corrupt": path.write_bytes(b"not json")
    elif fault == "bundle_rehashed":
        def mutate(value): value["record"]["bundle"]["bars"][0].update(open=15., high=16., low=14., close=15.)
        rewrite_envelope(path, mutate)
    elif fault == "raw_rehashed":
        def mutate(value): value["record"]["raw_source"] += " "
        rewrite_envelope(path, mutate)
    elif fault == "identity_rehashed":
        def mutate(value): value["record"]["identity"]["code_commit"] = "b" * 40
        rewrite_envelope(path, mutate)
    else:
        path.write_bytes(b'{"record_sha256":"' + b"0" * 64 + b'",' + path.read_bytes()[1:])
    corrupted = path.read_bytes()
    with pytest.raises(SnapshotIntegrityError):
        get(svc, callback=lambda _: pytest.fail("corrupt cache is not a cache miss"))
    assert path.read_bytes() == corrupted


@pytest.mark.parametrize("fault", ["empty_sessions", "duplicate_json", "nonfinite", "bad_clock"])
def test_invalid_provider_input_has_no_snapshot_file_or_automatic_retry(tmp_path, fault):
    svc, calls = service(tmp_path), []
    def callback(req):
        calls.append(req)
        data = json.loads(raw(req))
        if fault == "empty_sessions": data.update(sessions=[], bars=[])
        elif fault == "nonfinite": data["bars"][0]["close"] = float("nan")
        data = json.dumps(data).encode()
        if fault == "duplicate_json": data = b'{"bars":[],' + data[1:]
        return provider(req, raw_source=data, retrieved_at=at(DAYS[-1], 21, 30) if fault == "bad_clock" else at(DAYS[-1], 23))
    with pytest.raises(ValueError): get(svc, callback=callback)
    assert len(calls) == 1 and not svc.store.root.exists()


def test_explicit_sessions_with_no_bars_are_valid_and_cache_missingness(tmp_path):
    svc, calls = service(tmp_path), []
    def fetch(req):
        calls.append(req); data = json.loads(raw(req)); data["bars"] = []
        return provider(req, raw_source=json.dumps(data).encode())
    one, two = get(svc, callback=fetch), get(svc, callback=fetch)
    assert not one.bundle.bars and len(one.bundle.sessions) == 4 and two.cache_hit and len(calls) == 1
    report = data_quality(two.bundle, request(), svc.guard)
    assert (report.expected_bars, report.available_bars, report.complete_bars, report.coverage) == (8, 0, 0, 0.)


@pytest.mark.parametrize("error,category", [(TimeoutError("do not echo provider input"), "TIMEOUT"),
                                         (RuntimeError("do not echo provider values"), "PROVIDER_ERROR")])
def test_fetch_exception_is_sanitized_not_retried_or_persisted(tmp_path, error, category):
    svc, calls = service(tmp_path), []
    def callback(req): calls.append(req); raise error
    with pytest.raises(SnapshotFetchError) as captured: get(svc, callback=callback)
    assert captured.value.error_category == category and "do not echo" not in str(captured.value)
    assert len(calls) == 1 and not svc.store.root.exists()


def test_batch_plan_has_exact_calendar_partition_and_unchanged_semantics():
    req = request()
    plan = plan_batches(req, guard(), max_calendar_days=2, assets_per_batch=1)
    assert [(p.start, p.end, p.assets) for p in plan.requests] == [
        (DAYS[0], DAYS[1], ["SID_A"]), (DAYS[0], DAYS[1], ["SID_B"]),
        (DAYS[2], DAYS[3], ["SID_A"]), (DAYS[2], DAYS[3], ["SID_B"])]
    assert all((p.as_of, p.fields, p.purpose, p.classification, p.derived_from) ==
               (req.as_of, req.fields, req.purpose, req.classification, req.derived_from) for p in plan.requests)
    assert plan.original_request == req


def test_batch_service_retains_each_raw_snapshot_and_second_run_fetches_nothing(tmp_path):
    svc, calls = service(tmp_path), []
    plan = plan_batches(request(), svc.guard, max_calendar_days=2, assets_per_batch=1)
    def fetch(req): calls.append(req); return provider(req)
    one = svc.get_batches(plan, **META, fetch=fetch)
    assert len(calls) == len(one.snapshots) == 4
    assert all(not item.cache_hit for item in one.snapshots)
    assert [item.bundle.provenance.source_sha256 for item in one.snapshots] == [hashlib.sha256(raw(req)).hexdigest() for req in plan.requests]
    assert sum(len(item.bundle.bars) for item in one.snapshots) == 8
    assert [item.bundle.request for item in one.snapshots] == plan.requests
    two = svc.get_batches(plan, **META, fetch=lambda _: pytest.fail("all four cached originals must be reused"))
    assert all(item.cache_hit for item in two.snapshots) and not hasattr(two, "source_sha256")


def test_entire_batch_original_and_all_children_are_checked_before_first_fetch(tmp_path, monkeypatch):
    svc = service(tmp_path); plan = plan_batches(request(), svc.guard, max_calendar_days=2, assets_per_batch=1)
    object.__setattr__(plan.requests[-1], "end", date(2024, 1, 6))
    monkeypatch.setattr(svc.store, "_read_bytes", lambda _: pytest.fail("malformed whole batch cannot read first cache"))
    with pytest.raises(ValueError): svc.get_batches(plan, **META, fetch=lambda _: pytest.fail("cannot fetch a valid prefix"))
    assert not svc.store.root.exists()
    with pytest.raises(ValueError): plan_batches(request(end=date(2024, 1, 6)), svc.guard, max_calendar_days=2)


@pytest.mark.parametrize("kwargs", [dict(max_calendar_days=True), dict(max_calendar_days=0), dict(max_calendar_days=257),
                                  dict(assets_per_batch=False), dict(assets_per_batch=33)])
def test_batch_dimensions_are_strict_and_bounded(kwargs):
    with pytest.raises(ValueError): plan_batches(request(), guard(), **kwargs)


def test_batch_count_and_calendar_asset_workload_are_bounded():
    extended = guard(development=TimeRange(start="2024-01-01", end="2024-12-31"), sealed=[])
    with pytest.raises(ValueError, match="32"):
        plan_batches(request(start=date(2024, 1, 1), end=date(2024, 2, 2), as_of=at(date(2024, 12, 31))), extended, max_calendar_days=1)
    names = ["SID_" + str(i) for i in range(32)]
    extended = guard(development=TimeRange(start="2024-01-01", end="2024-12-31"), sealed=[], allowed_assets=names)
    with pytest.raises(ValueError, match="8192"):
        plan_batches(request(start=date(2024, 1, 1), end=date(2024, 12, 31), assets=names, as_of=at(date(2024, 12, 31))), extended)


@pytest.mark.parametrize("same", [True, False])
def test_concurrent_exclusive_publish_reuses_identical_data_and_rejects_conflict(tmp_path, same):
    store, access, req, key = SyntheticSnapshotStore(tmp_path / "cache"), guard(), request(), identity()
    incoming = provider() if same else provider(retrieved_at=at(DAYS[-1], 23, 1))
    def write_one(item):
        try: return store.write(key, req, item, access)
        except SnapshotConflictError: return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write_one, [provider(), incoming]))
    assert sum(value == "conflict" for value in results) == (0 if same else 1)
    assert store.read(key, req, access) is not None
    assert [p.name for p in store.root.iterdir()] == [snapshot_key(key) + ".json"]


@pytest.mark.parametrize("where", ["root", "entry"])
def test_symlink_or_reparse_cache_paths_are_refused(tmp_path, where):
    actual = tmp_path / "actual"; actual.mkdir()
    store, access, req, key = SyntheticSnapshotStore(tmp_path / "cache"), guard(), request(), identity()
    try:
        if where == "root": os.symlink(actual, store.root, target_is_directory=True)
        else:
            store.root.mkdir(); other = actual / "other.json"; other.write_bytes(b"SYNTHETIC POISON")
            os.symlink(other, store.path_for(key))
    except OSError: pytest.skip("symlink creation requires privileges on this host")
    with pytest.raises(ValueError): store.read(key, req, access)
    with pytest.raises(ValueError): store.write(key, req, provider(), access)
    assert not list(actual.glob("*.tmp"))


def test_directory_at_snapshot_filename_is_not_followed_or_overwritten(tmp_path):
    store, access, req, key = SyntheticSnapshotStore(tmp_path / "cache"), guard(), request(), identity()
    path = store.path_for(key); path.mkdir(parents=True)
    sentinel = path / "sentinel.txt"; sentinel.write_bytes(b"DO NOT OVERWRITE")
    with pytest.raises(ValueError): store.read(key, req, access)
    with pytest.raises(ValueError): store.write(key, req, provider(), access)
    assert sentinel.read_bytes() == b"DO NOT OVERWRITE"


@pytest.mark.skipif(os.name != "nt", reason="actual Windows junction boundary")
def test_actual_windows_junction_cache_root_is_rejected(tmp_path):
    actual, link = tmp_path / "actual", tmp_path / "junction"
    actual.mkdir()
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(actual)], capture_output=True, check=False)
    if made.returncode != 0:
        pytest.skip("this Windows host cannot create a temporary junction")
    try:
        assert link.lstat().st_file_attributes & 0x400
        store = SyntheticSnapshotStore(link)
        with pytest.raises(ValueError): store.read(identity(), request(), guard())
        with pytest.raises(ValueError): store.write(identity(), request(), provider(), guard())
        assert not list(actual.iterdir())
    finally:
        os.rmdir(link)  # Remove only the temporary junction itself, never its target.
