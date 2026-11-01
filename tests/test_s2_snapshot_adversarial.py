"""Independent synthetic cache-tamper, publication and pre-I/O counterexamples."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from threading import Barrier, local

import pytest

from bbag.contracts import TimeRange
from bbag.s2_access import AccessGuard, AccessPolicy, DataRequest, STANDARD_FIELDS
from bbag.s2_data import canonical_request_sha256
from bbag.s2_snapshots import (
    GuardedSyntheticDataService, ProviderSnapshot, SnapshotConflictError, SnapshotIntegrityError,
    SyntheticSnapshotStore, make_snapshot_identity, plan_batches,
)


DAYS = [date(2024, 1, 2), date(2024, 1, 3)]
ASSETS = ["SYN_C1", "SYN_C2"]
CUTOFF = datetime(2024, 1, 3, 23, tzinfo=timezone.utc)
KNOWN = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
METADATA = dict(data_id="SYNTHETIC_ADVERSARIAL", provider_data_version="v1",
                calendar_id="SYNTHETIC_EXPLICIT_TWO_DAYS", calendar_version="v1")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def request(**changes):
    values = dict(provider="SYNTHETIC", start=DAYS[0], end=DAYS[-1], assets=ASSETS,
                  fields=list(STANDARD_FIELDS), as_of=CUTOFF, classification="DEVELOPMENT")
    values.update(changes)
    return DataRequest(**values)


def guard():
    return AccessGuard(AccessPolicy(policy_id="SYNTHETIC_ADVERSARIAL_POLICY", provider="SYNTHETIC",
        development=TimeRange(start=DAYS[0], end=DAYS[-1]), allowed_assets=ASSETS,
        allowed_fields=list(STANDARD_FIELDS)))


def snapshot(price=10.0, **changes):
    def moment(day, hour, minute=0):
        return datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=hour, minutes=minute)
    source = dict(sessions=[dict(session=day, open_at=moment(day, 14, 30), close_at=moment(day, 21),
                                signal_at=moment(day, 21, 5)) for day in DAYS],
        identities=[dict(sid=sid, listed_from=DAYS[0], known_at=KNOWN) for sid in ASSETS],
        tickers=[dict(sid=sid, ticker="C" + str(index), valid_from=DAYS[0], known_at=KNOWN) for index, sid in enumerate(ASSETS)],
        membership=[dict(sid=sid, effective_on=DAYS[0], member=True, known_at=KNOWN) for sid in ASSETS],
        corporate_actions=[], bars=[dict(sid=sid, session=day, open=price, high=price + 1.0, low=price - 1.0,
            close=price, volume=100.0, available_at=moment(day, 21, 1)) for day in DAYS for sid in ASSETS])
    raw = json.dumps(source, default=lambda value: value.isoformat(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    values = dict(raw_source=raw, retrieved_at=CUTOFF + timedelta(hours=1), classification="DEVELOPMENT", **METADATA)
    values.update(changes)
    return ProviderSnapshot(**values)


def setup(tmp_path):
    selected_guard = guard()
    original = request()
    store = SyntheticSnapshotStore(tmp_path / "cache")
    identity = make_snapshot_identity(original, selected_guard, code_commit="a" * 40, **METADATA)
    return store, identity, original, selected_guard


@pytest.mark.parametrize("mutation", ["calendar_id", "calendar_version", "provider_version", "source_digest", "retrieval_header", "future_metadata", "artifact_taint"])
def test_rehashed_outer_record_cannot_hide_inner_version_clock_or_source_disagreement(tmp_path, mutation):
    store, identity, original, selected_guard = setup(tmp_path)
    store.write(identity, original, snapshot(), selected_guard)
    path = store.path_for(identity)
    envelope = json.loads(path.read_bytes())
    record = envelope["record"]
    bundle = record["bundle"]
    if mutation in {"calendar_id", "calendar_version"}:
        bundle[mutation] = "different-version"
    elif mutation == "provider_version":
        bundle["provenance"]["provider_data_version"] = "different-version"
    elif mutation == "source_digest":
        bundle["provenance"]["source_sha256"] = "f" * 64
    elif mutation == "retrieval_header":
        record["retrieved_at"] = "2024-01-04T00:01:00Z"
    elif mutation == "future_metadata":
        bundle["membership"][0]["known_at"] = "2024-01-03T23:00:00.000001Z"
    else:
        bundle["request"]["classification"] = "SEALED_FINAL"
        bundle["provenance"]["classification"] = "SEALED_FINAL"
        changed_request = DataRequest.model_validate_json(canonical(bundle["request"]))
        bundle["provenance"]["request_sha256"] = canonical_request_sha256(changed_request)
    envelope["record_sha256"] = hashlib.sha256(canonical(record)).hexdigest()
    corrupted = canonical(envelope)
    path.write_bytes(corrupted)
    calls = []
    service = GuardedSyntheticDataService(store, selected_guard, code_commit="a" * 40)
    with pytest.raises(SnapshotIntegrityError):
        service.get(original, fetch=lambda _: calls.append("fetch"), **METADATA)
    assert calls == []
    assert path.read_bytes() == corrupted  # no repair, overwrite or fallback fetch


@pytest.mark.parametrize("different_content", [False, True])
def test_concurrent_exclusive_publication_is_idempotent_or_conflicts_without_pending_files(tmp_path, monkeypatch, different_content):
    store, identity, original, selected_guard = setup(tmp_path)
    first_reads = Barrier(2)
    thread = local()
    actual_load = SyntheticSnapshotStore._load
    def overlap_initial_misses(self, *args, **kwargs):
        found = actual_load(self, *args, **kwargs)
        count = getattr(thread, "reads", 0) + 1
        thread.reads = count
        if count == 1:
            assert found is None
            first_reads.wait(timeout=15)
        return found
    monkeypatch.setattr(SyntheticSnapshotStore, "_load", overlap_initial_misses)
    def publish(price):
        try:
            result = store.write(identity, original, snapshot(price), selected_guard)
            return "published", result.bars[0].close
        except SnapshotConflictError:
            return "conflict", None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, [10.0, 30.0 if different_content else 10.0]))
    assert sorted(row[0] for row in results) == (["conflict", "published"] if different_content else ["published", "published"])
    monkeypatch.setattr(SyntheticSnapshotStore, "_load", actual_load)
    winner = store.read(identity, original, selected_guard)
    assert winner is not None and winner.bars[0].close in ({10.0, 30.0} if different_content else {10.0})
    assert len(list(store.root.iterdir())) == 1
    assert store.path_for(identity).is_file()


def test_late_child_denial_happens_before_the_first_cache_read_or_fetch(tmp_path, monkeypatch):
    store, _, original, selected_guard = setup(tmp_path)
    plan = plan_batches(original, selected_guard, max_calendar_days=1, assets_per_batch=1)
    original_authorize = selected_guard.authorize
    observed = []
    def narrowed_authorize(selected, operation="query"):
        if selected.assets == ["SYN_C2"] and selected.start == selected.end == DAYS[-1]:
            raise ValueError("synthetic late-child denial")
        return original_authorize(selected, operation)
    monkeypatch.setattr(selected_guard, "authorize", narrowed_authorize)
    monkeypatch.setattr(store, "_read_bytes", lambda *_: observed.append("cache-read"))
    service = GuardedSyntheticDataService(store, selected_guard, code_commit="a" * 40)
    with pytest.raises(ValueError, match="late-child"):
        service.get_batches(plan, fetch=lambda _: observed.append("fetch"), **METADATA)
    assert observed == [] and not store.root.exists()


def test_a_sealed_late_child_in_a_mutated_typed_plan_is_not_partially_executed(tmp_path, monkeypatch):
    store, _, original, selected_guard = setup(tmp_path)
    plan = plan_batches(original, selected_guard, max_calendar_days=1, assets_per_batch=1)
    children = list(plan.requests)
    children[-1] = children[-1].model_copy(update={"classification": "SEALED_FINAL"})
    bad = plan.model_copy(update={"requests": children})
    observed = []
    monkeypatch.setattr(store, "_read_bytes", lambda *_: observed.append("cache-read"))
    service = GuardedSyntheticDataService(store, selected_guard, code_commit="a" * 40)
    with pytest.raises(ValueError):
        service.get_batches(bad, fetch=lambda _: observed.append("fetch"), **METADATA)
    assert observed == [] and not store.root.exists()


def test_provider_taint_is_checked_before_parsing_or_any_store_io(tmp_path, monkeypatch):
    import bbag.s2_snapshots as module
    store, identity, original, selected_guard = setup(tmp_path)
    observed = []
    def forbidden(*args, **kwargs):
        observed.append("payload-or-io")
        raise AssertionError("tainted snapshot inspected")
    monkeypatch.setattr(module, "standardize_daily_json", forbidden)
    monkeypatch.setattr(store, "_read_bytes", forbidden)
    poisoned = snapshot(raw_source=b"not JSON", classification="SEALED_FINAL")
    with pytest.raises(ValueError, match="sealed"):
        store.write(identity, original, poisoned, selected_guard)
    assert observed == [] and not store.root.exists()
