"""SYNTHETIC_TEST S2-004 regressions: real A/B disk writes and real parsers/SQL.

The wrappers below schedule I/O only. They never replace a JSON/SQL result,
change the expected rejection, or access the historical platform ledgers.
"""
from contextlib import closing
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Event, Thread

import pytest

import bbag.s2_readiness as ready
from bbag.jsonio import parse_json_object, read_json_object
from bbag.s2_readiness import CampaignCaps, ExpectedLedger, ReadinessError, inspect_campaign
from bbag.s2_tasks import RawMetadataRef, TaskBudget, TaskLedger, TaskResponse, TaskSpec, canonical_task_bytes


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _inventory(root):
    return {p.relative_to(root).as_posix(): _sha(p.read_bytes()) for p in root.rglob("*") if p.is_file()}


def _campaign(root, *, capture_kind="CALLER_METADATA_BYTES", journal="wal"):
    anchors, ledgers = [], []
    for number in range(2):
        home = root/f"SYNTHETIC-ledger-{number}"
        ledger = TaskLedger(home/"tasks.sqlite", home/"metadata", TaskBudget(
            response_class="SYNTHETIC_TEST", max_configurations=1, max_attempts=3,
            max_concurrent_attempts=1, max_cost_microusd=0,
            operation_limits={"compile": 1, "research_check": 2}))
        for operation in ("compile", "research_check"):
            spec = TaskSpec(correlation_id=f"synthetic_{number}_{operation}", operation=operation,
                source_commit="a"*40, config_sha256=str(number+1)*64,
                request_sha256=("c" if operation == "compile" else "d")*64,
                project_id=123, response_class="SYNTHETIC_TEST")
            ledger.register(spec)
            attempt = ledger.begin_attempt(spec.correlation_id)
            response = TaskResponse(**{key: getattr(spec, key) for key in (
                "operation", "source_commit", "config_sha256", "request_sha256", "project_id", "response_class")},
                status="completed", success=True, check_passed=False,
                platform_task_id=f"SYNTHETIC_PROVIDER_{number}_{operation}")
            ledger.record_event(attempt.local_attempt_id, "synthetic_observed", response,
                raw_metadata=canonical_task_bytes(response) if capture_kind == "CALLER_METADATA_BYTES" else None)
        if journal == "delete":
            with closing(sqlite3.connect(ledger.db_path)) as connection:
                assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        assert not any(Path(str(ledger.db_path)+suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
        anchors.append(ExpectedLedger(database=str(ledger.db_path), metadata_directory=str(ledger.raw_dir),
                                      expected_sha256=_sha(ledger.db_path.read_bytes())))
        ledgers.append(ledger)
    caps = CampaignCaps(expected_ledgers=anchors, compile_attempt_limit=2, research_attempt_limit=3,
        max_new_cost_microusd=0, project_id=123, response_class="SYNTHETIC_TEST",
        start=date(2020, 1, 2), end=date(2020, 1, 10), assets=["SPY", "AAPL"],
        approval_reference="SYNTHETIC_ABA_FIXTURE_NOT_AUTHORIZATION")
    return caps, ledgers


def _repin(caps, ledgers):
    return caps.model_copy(update={"expected_ledgers": [ExpectedLedger(database=str(ledger.db_path),
        metadata_directory=str(ledger.raw_dir), expected_sha256=_sha(ledger.db_path.read_bytes())) for ledger in ledgers]})


def _payload_image(ledger, make_image):
    """Forge only synthetic outer pins/refs, leaving TaskSpec and attempt values.

    A may be invalid JSON or a semantic contradiction; B is the original valid
    event. The static control must therefore reject the complete pinned A chain.
    """
    with closing(sqlite3.connect(ledger.db_path)) as connection:
        identity, event_id, raw_ref = connection.execute(
            "SELECT attempt_id,event_id,raw_json FROM events ORDER BY attempt_id LIMIT 1").fetchone()
        reference = RawMetadataRef.model_validate_json(raw_ref)
        old_path = ledger.raw_dir/reference.relative_path
        valid = old_path.read_bytes()
        pinned = make_image(valid)
        digest = _sha(pinned)
        new_name = reference.relative_path.rsplit("_", 1)[0]+"_"+digest+".json"
        updated = RawMetadataRef.model_validate(reference.model_copy(update={
            "relative_path": new_name, "sha256": digest, "size_bytes": len(pinned)}))
        victim = ledger.raw_dir/new_name
        victim.write_bytes(pinned)
        if old_path != victim:
            old_path.unlink()
        attempt_raw = connection.execute("SELECT attempt_json FROM attempts WHERE id=?", (identity,)).fetchone()[0]
        attempt = json.loads(attempt_raw)
        attempt["raw_metadata"] = [updated.model_dump(mode="json") if item["relative_path"] == reference.relative_path else item
                                   for item in attempt["raw_metadata"]]
        connection.execute("UPDATE attempts SET attempt_json=? WHERE id=?", (
            json.dumps(attempt, sort_keys=True, separators=(",", ":")), identity))
        connection.execute("UPDATE events SET payload_sha256=?,raw_json=? WHERE attempt_id=? AND event_id=?",
                           (digest, canonical_task_bytes(updated).decode(), identity, event_id))
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert victim.read_bytes() == pinned
    return victim, pinned, valid


class _DiskABA:
    """A real writer holds B until the real parse/connection has completed."""

    def __init__(self, path, pinned, transient):
        self.path, self.pinned, self.transient = path, pinned, transient
        self.written, self.release, self.restored = Event(), Event(), Event()
        self.actions, self.errors = [], []
        self.thread = None

    def _writer(self):
        try:
            self.path.write_bytes(self.transient)
            self.actions.append({"action": "WRITE_B", "actual_sha256": _sha(self.path.read_bytes())})
            self.written.set()
            if not self.release.wait(15):
                raise AssertionError("real operation did not release the scheduled writer")
        except BaseException as error:
            self.errors.append(repr(error))
        finally:
            try:
                self.path.write_bytes(self.pinned)
                self.actions.append({"action": "RESTORE_A", "actual_sha256": _sha(self.path.read_bytes())})
            except BaseException as error:
                self.errors.append(repr(error))
            self.restored.set()

    def begin(self):
        assert self.thread is None and self.path.read_bytes() == self.pinned
        self.thread = Thread(target=self._writer, daemon=True)
        self.thread.start()
        assert self.written.wait(15) and not self.errors
        assert self.path.read_bytes() == self.transient

    def finish(self):
        self.release.set()
        if self.thread is not None:
            assert self.restored.wait(15)
            self.thread.join(15)
            assert not self.thread.is_alive() and not self.errors
            assert self.path.read_bytes() == self.pinned

    def assert_executed(self):
        assert self.thread is not None
        assert self.actions == [{"action": "WRITE_B", "actual_sha256": _sha(self.transient)},
                                {"action": "RESTORE_A", "actual_sha256": _sha(self.pinned)}]


def _witness(root, name, swap, before, **extra):
    assert _inventory(root) == before
    swap.assert_executed()
    result = {"scope": "SYNTHETIC_REAL_FILE_ABA_NOT_PLATFORM_EXECUTION", "real_platform_calls": 0,
              "pinned_A_sha256": _sha(swap.pinned), "transient_B_sha256": _sha(swap.transient),
              "writer_actions": swap.actions, "initial_final_input_bytes_equal": True, **extra}
    (root/name).write_text(json.dumps(result, indent=2), encoding="utf-8")


def _changed_body(valid, field, value):
    body = json.loads(valid)
    body[field] = value
    return json.dumps(body, sort_keys=True, indent=2).encode()


def _invalid_payload(valid, fault):
    if fault in {"source_commit", "config_sha256", "request_sha256"}:
        return _changed_body(valid, fault, "f"*(40 if fault == "source_commit" else 64))
    if fault == "duplicate_key":
        return b'{"status":"completed",'+valid[1:]
    if fault == "extra":
        return _changed_body(valid, "SYNTHETIC_EXTRA_FIELD", "REJECT_ME")
    if fault in {"NaN", "Infinity", "-Infinity", "1e400"}:
        assert valid.count(b'"actual_cost_microusd":null') == 1
        return valid.replace(b'"actual_cost_microusd":null', b'"actual_cost_microusd":'+fault.encode())
    if fault == "bool_type":
        return _changed_body(valid, "check_passed", 0)
    if fault == "integer_type":
        return _changed_body(valid, "project_id", True)
    if fault == "money_type":
        return _changed_body(valid, "actual_cost_microusd", False)
    if fault == "array":
        return b"[]"
    if fault == "invalid_utf8":
        return b'\xff{"status":"completed"}'
    raise AssertionError(fault)


@pytest.mark.parametrize("fault", ["source_commit", "config_sha256", "request_sha256", "duplicate_key", "extra",
    "NaN", "Infinity", "-Infinity", "1e400", "bool_type", "integer_type", "money_type", "array", "invalid_utf8"])
def test_invalid_pinned_metadata_a_cannot_be_replaced_by_valid_disk_b(tmp_path, monkeypatch, fault):
    caps, ledgers = _campaign(tmp_path)
    victim, pinned, valid = _payload_image(ledgers[0], lambda raw: _invalid_payload(raw, fault))
    caps = _repin(caps, ledgers)
    before = _inventory(tmp_path)
    with pytest.raises(ReadinessError) as control:
        inspect_campaign(caps, root=tmp_path)
    assert _inventory(tmp_path) == before
    swap = _DiskABA(victim, pinned, valid)
    original_parser = ready.parse_json_object
    calls = []
    def scheduled_parser(payload):
        if payload == pinned and not calls:
            assert type(payload) is bytes and _sha(payload) == _sha(pinned)
            calls.append(_sha(payload))
            swap.begin()
            try:
                return original_parser(payload)  # Real parser receives captured A, never a substituted result.
            finally:
                swap.finish()
        return original_parser(payload)
    monkeypatch.setattr(ready, "parse_json_object", scheduled_parser)
    try:
        with pytest.raises(ReadinessError) as raced:
            inspect_campaign(caps, root=tmp_path)
    finally:
        swap.finish()
    assert calls == [_sha(pinned)] and str(raced.value) == str(control.value)
    _witness(tmp_path, "METADATA_ABA_WITNESS.json", swap, before,
             invalid_case=fault, actual_parser_input_sha256=calls[0], expected_rejection_preserved=True)


def _legal_encoding(valid, kind):
    if kind == "canonical":
        return valid
    if kind == "escaped":
        source = json.loads(valid)["source_commit"]
        escaped = '"'+"".join(chr(92)+"u"+format(ord(char), "04x") for char in source)+'"'
        return valid.decode().replace(json.dumps(source), escaped).encode()
    reverse_keys = dict(reversed(list(json.loads(valid).items())))
    return b" \r\n\t"+json.dumps(reverse_keys, indent=3).encode()+b"\n\t "


@pytest.mark.parametrize("capture_kind,encoding", [("CALLER_METADATA_BYTES", "pretty"),
    ("CALLER_METADATA_BYTES", "escaped"), ("LOCAL_NORMALIZED_METADATA", "canonical")])
def test_valid_pinned_metadata_encoding_survives_bad_transient_disk_bytes(tmp_path, monkeypatch, capture_kind, encoding):
    caps, ledgers = _campaign(tmp_path, capture_kind=capture_kind)
    victim, pinned, _ = _payload_image(ledgers[0], lambda raw: _legal_encoding(raw, encoding))
    caps = _repin(caps, ledgers)
    invalid = _changed_body(pinned, "source_commit", "f"*40)
    swap = _DiskABA(victim, pinned, invalid)
    original_parser = ready.parse_json_object
    calls = []
    def scheduled_parser(payload):
        if payload == pinned and not calls:
            calls.append(_sha(payload)); swap.begin()
            try:
                return original_parser(payload)
            finally:
                swap.finish()
        return original_parser(payload)
    monkeypatch.setattr(ready, "parse_json_object", scheduled_parser)
    before = _inventory(tmp_path)
    try:
        result = inspect_campaign(caps, root=tmp_path)
    finally:
        swap.finish()
    assert calls == [_sha(pinned)] and result.remaining_attempts == {"compile": 0, "research_check": 1}
    assert result.billing_unknown_attempt_count == 4 and result.actual_billing_total_microusd is None
    assert result.dispatch_allowed is False
    _witness(tmp_path, "VALID_ENCODING_ABA_WITNESS.json", swap, before,
             capture_kind=capture_kind, encoding=encoding, actual_parser_input_sha256=calls[0])


def test_local_noncanonical_a_is_not_rescued_by_canonical_disk_b(tmp_path, monkeypatch):
    caps, ledgers = _campaign(tmp_path, capture_kind="LOCAL_NORMALIZED_METADATA")
    victim, pinned, valid = _payload_image(ledgers[0], lambda raw: _legal_encoding(raw, "pretty"))
    caps = _repin(caps, ledgers)
    with pytest.raises(ReadinessError, match="normalized local metadata"):
        inspect_campaign(caps, root=tmp_path)
    swap = _DiskABA(victim, pinned, valid)
    original_parser = ready.parse_json_object
    calls = []
    def scheduled_parser(payload):
        if payload == pinned and not calls:
            calls.append(True); swap.begin()
            try:
                return original_parser(payload)
            finally:
                swap.finish()
        return original_parser(payload)
    monkeypatch.setattr(ready, "parse_json_object", scheduled_parser)
    before = _inventory(tmp_path)
    try:
        with pytest.raises(ReadinessError, match="normalized local metadata"):
            inspect_campaign(caps, root=tmp_path)
    finally:
        swap.finish()
    assert calls == [True]
    _witness(tmp_path, "LOCAL_ENCODING_ABA_WITNESS.json", swap, before, expected_rejection_preserved=True)


def _bad_database_image(ledger, fault):
    valid = ledger.db_path.read_bytes()
    with closing(sqlite3.connect(ledger.db_path)) as connection:
        if fault == "schema":
            connection.execute("UPDATE ledger_meta SET value='SYNTHETIC_INVALID_SCHEMA' WHERE key='schema_version'")
        else:
            row = connection.execute("SELECT attempt_id,event_id FROM events ORDER BY attempt_id LIMIT 1").fetchone()
            connection.execute("UPDATE events SET payload_sha256=? WHERE attempt_id=? AND event_id=?", ("0"*64, *row))
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return ledger.db_path.read_bytes(), valid


class _ScheduledConnection:
    def __init__(self, actual, swap, pinned, observed):
        self.actual, self.swap, self.pinned, self.observed = actual, swap, pinned, observed

    def __getattr__(self, name):
        return getattr(self.actual, name)

    def deserialize(self, image, *args, **kwargs):
        assert type(image) is bytes and image[:18] == self.pinned[:18] and image[20:] == self.pinned[20:]
        assert image[18:20] == b"\x01\x01"
        self.observed.append({"snapshot_page_image_sha256": _sha(image)})
        return self.actual.deserialize(image, *args, **kwargs)  # Actual SQLite image loading, no query stubs.

    def close(self):
        try:
            self.actual.close()
        finally:
            self.swap.finish()


@pytest.mark.parametrize("fault", ["schema", "event"])
def test_invalid_pinned_database_a_is_not_rescued_by_valid_disk_b(tmp_path, monkeypatch, fault):
    caps, ledgers = _campaign(tmp_path)
    victim = ledgers[0].db_path
    pinned, valid = _bad_database_image(ledgers[0], fault)
    caps = _repin(caps, ledgers)
    before = _inventory(tmp_path)
    with pytest.raises(ReadinessError) as control:
        inspect_campaign(caps, root=tmp_path)
    assert _inventory(tmp_path) == before
    swap = _DiskABA(victim, pinned, valid)
    real_connect = ready.sqlite3.connect
    opens, observed = [], []
    def scheduled_connect(database, *args, **kwargs):
        opens.append(database)
        assert database == ":memory:"  # No input-path reopen or fresh runtime ledger fallback.
        if len(opens) == 1:
            swap.begin()
            return _ScheduledConnection(real_connect(database, *args, **kwargs), swap, pinned, observed)
        return real_connect(database, *args, **kwargs)
    monkeypatch.setattr(ready.sqlite3, "connect", scheduled_connect)
    try:
        with pytest.raises(ReadinessError) as raced:
            inspect_campaign(caps, root=tmp_path)
    finally:
        swap.finish()
    assert opens == [":memory:"] and len(observed) == 1
    assert str(raced.value) == str(control.value)
    _witness(tmp_path, "DATABASE_ABA_WITNESS.json", swap, before, invalid_case=fault,
             actual_memory_image=observed, real_sql_results_not_forged=True)


@pytest.mark.parametrize("journal,header", [("wal", b"\x02\x02"), ("delete", b"\x01\x01")])
def test_snapshot_is_isolated_query_only_for_both_legitimate_journal_headers(tmp_path, journal, header):
    _, ledgers = _campaign(tmp_path, journal=journal)
    raw = ledgers[0].db_path.read_bytes()
    assert raw[18:20] == header
    before = _inventory(tmp_path)
    with closing(ready._ledger_snapshot(raw)) as connection:
        assert isinstance(connection, sqlite3.Connection)
        assert connection.execute("PRAGMA database_list").fetchall() == [(0, "main", "")]
        assert connection.execute("PRAGMA query_only").fetchone() == (1,)
        assert connection.execute("PRAGMA trusted_schema").fetchone() == (0,)
        assert connection.execute("PRAGMA temp_store").fetchone() == (2,)
        assert connection.execute("SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone() == ("1.0",)
        for statement in ("UPDATE ledger_meta SET value='BAD' WHERE key='schema_version'",
                          "CREATE TEMP TABLE forbidden(value TEXT)",
                          "ATTACH DATABASE ':memory:' AS forbidden",
                          "PRAGMA query_only=OFF", "PRAGMA trusted_schema=ON", "PRAGMA writable_schema=ON"):
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute(statement)
        assert connection.execute("PRAGMA query_only").fetchone() == (1,)
        assert connection.execute("PRAGMA database_list").fetchall() == [(0, "main", "")]
    assert _inventory(tmp_path) == before and ledgers[0].db_path.read_bytes() == raw


def test_missing_deserialize_fails_closed_and_closes_only_the_verification_connection(tmp_path, monkeypatch):
    _, ledgers = _campaign(tmp_path)
    raw = ledgers[0].db_path.read_bytes()
    before = _inventory(tmp_path)
    real_connect, opens, closed = ready.sqlite3.connect, [], []
    class WithoutDeserialize:
        def __init__(self, actual): self.actual = actual
        def __getattr__(self, name):
            if name == "deserialize": raise AttributeError(name)
            return getattr(self.actual, name)
        def close(self):
            self.actual.close(); closed.append(True)
    def connect(database, *args, **kwargs):
        opens.append(database)
        assert database == ":memory:"
        return WithoutDeserialize(real_connect(database, *args, **kwargs))
    monkeypatch.setattr(ready.sqlite3, "connect", connect)
    with pytest.raises(ReadinessError, match="deserialize support"):
        ready._ledger_snapshot(raw)
    assert opens == [":memory:"] and closed == [True] and _inventory(tmp_path) == before


@pytest.mark.parametrize("header", [b"\x01\x02", b"\x02\x01", b"\x03\x03"])
def test_unsupported_journal_header_is_not_reinterpreted_or_reopened(tmp_path, monkeypatch, header):
    _, ledgers = _campaign(tmp_path)
    raw = ledgers[0].db_path.read_bytes()
    malformed = raw[:18]+header+raw[20:]
    monkeypatch.setattr(ready.sqlite3, "connect", lambda *_a, **_k: pytest.fail("unsupported image must precede any connection"))
    with pytest.raises(ReadinessError, match="snapshot header"):
        ready._ledger_snapshot(malformed)


@pytest.mark.parametrize("text", [' { "名称" : "测试", "items": [{"n":1.25}, true, null] } ',
    r'{"emoji":"\ud83d\ude00","name":"\u6d4b\u8bd5","nested":[{"key":"value"}]}'])
def test_common_utf8_parser_keeps_nonascii_whitespace_key_order_and_escapes(tmp_path, text):
    payload = text.encode("utf-8")
    expected = json.loads(text)
    assert parse_json_object(payload) == parse_json_object(text) == expected
    path = tmp_path/"synthetic-utf8.json"
    path.write_bytes(payload)
    assert read_json_object(path) == expected and path.read_bytes() == payload


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"])
def test_common_parser_does_not_autodetect_non_utf8_input(tmp_path, encoding):
    payload = '{"名称":"测试"}'.encode(encoding)
    path = tmp_path/"synthetic-other-encoding.json"
    path.write_bytes(payload)
    with pytest.raises((ValueError, UnicodeError)):
        parse_json_object(payload)
    with pytest.raises((ValueError, UnicodeError)):
        read_json_object(path)
    assert path.read_bytes() == payload


@pytest.mark.parametrize("payload", [b'\xef\xbb\xbf{"a":1}', '\ufeff{"a":1}', b'\xff{"a":1}',
    b'{"a":"\xed\xa0\x80"}', b'{"a":1,"a":1}', b'{"nested":[{"a":1,"a":2}]}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e400}', b'[]', b'null'])
def test_common_parser_preserves_strict_rejections(payload):
    with pytest.raises((ValueError, UnicodeError)):
        parse_json_object(payload)


@pytest.mark.parametrize("payload", [None, 0, True, bytearray(b'{"a":1}'), memoryview(b'{"a":1}')])
def test_common_parser_rejects_types_outside_its_str_or_bytes_contract(payload):
    with pytest.raises(TypeError):
        parse_json_object(payload)
