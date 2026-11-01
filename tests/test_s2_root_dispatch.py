"""SYNTHETIC static-dispatch oracles; no SDK, platform or budget operations."""
from contextlib import contextmanager
import hashlib
import io
import logging
import sys

import pytest

from bbag.s2_cloud_check import run_cloud_data_check
from test_s2_cloud_check import synthetic


# Bypass user-defined __dict__ properties only in fixture setup/cleanup. This is
# the original stdlib instance-storage getset, not the descriptor under test.
STATE_SLOT = type.__getattribute__(logging.Filterer, "__dict__")["__dict__"]


def raw_state(logger):
    return STATE_SLOT.__get__(logger, type(logger))


@contextmanager
def root_manager_field(value, present):
    state = raw_state(logging.root)
    had = "manager" in state
    previous = state.get("manager")
    if present:
        state["manager"] = value
    else:
        state.pop("manager", None)
    try:
        yield state
    finally:
        if had:
            state["manager"] = previous
        else:
            state.pop("manager", None)


def invoke(inputs, calls):
    plan, _, guard, resolution, snapshot = inputs

    def resolver(*_):
        calls.append("resolver")
        logging.getLogger("SYNTHETIC_NORMAL_ROOT_DISPATCH_CALLBACK")
        return resolution

    def fetcher(*_):
        calls.append("fetcher")
        return snapshot

    return run_cloud_data_check(plan, guard=guard, resolver=resolver, fetcher=fetcher)


def descriptor(kind, getter, setter=None):
    if kind == "property":
        return property(getter, setter)

    class PlainDescriptor:
        def __get__(self, instance, owner):
            return getter(instance)

    return PlainDescriptor()


@pytest.mark.parametrize("kind", ["property", "plain"])
@pytest.mark.parametrize("masked", [False, True])
def test_root_manager_descriptor_is_rejected_statically_even_with_legal_instance_field(synthetic, monkeypatch, kind, masked):
    manager = type.__getattribute__(logging.Logger, "__dict__")["manager"]
    hooks, calls = [], []
    root = logging.root
    before_class = dict(type.__getattribute__(logging.RootLogger, "__dict__"))

    def getter(_):
        hooks.append("root.manager getter")
        return manager

    with root_manager_field(manager, masked) as state:
        with monkeypatch.context() as patch:
            patch.setattr(logging.RootLogger, "manager", descriptor(kind, getter), raising=False)
            receipt = invoke(synthetic, calls)
            # A legitimate plain instance field cannot hide a class data
            # descriptor. Even non-data descriptors are outside this profile.
            assert receipt.outcome == "ERROR" and receipt.checks is None
            assert hooks == [] and calls == []
            assert (state.get("manager") is manager) if masked else ("manager" not in state)
        assert dict(type.__getattribute__(logging.RootLogger, "__dict__")) == before_class
    assert logging.root is root


@pytest.mark.parametrize("target", [logging.Logger, logging.RootLogger])
def test_cache_data_descriptor_cannot_be_probed_through_legitimate_instance_cache(synthetic, monkeypatch, target):
    hooks, calls = [], []
    original_cache = raw_state(logging.root)["_cache"]
    contents = dict(original_cache)

    def getter(value):
        hooks.append("cache getter")
        return raw_state(value)["_cache"]

    def setter(value, cache):
        raw_state(value)["_cache"] = cache

    with monkeypatch.context() as patch:
        patch.setattr(target, "_cache", property(getter, setter), raising=False)
        receipt = invoke(synthetic, calls)
        assert receipt.outcome == "ERROR" and hooks == [] and calls == []
    assert raw_state(logging.root)["_cache"] is original_cache and original_cache == contents


@pytest.mark.parametrize("target", [logging.Logger, logging.RootLogger])
@pytest.mark.parametrize("kind", ["property", "plain"])
def test_effective_dict_descriptor_in_legal_mro_is_rejected_without_getter(synthetic, target, kind):
    # type.__dict__ itself is read-only, so setattr(Logger, '__dict__', ...) is
    # not a valid test carrier. A normal same-layout Python base shim provides
    # a real inherited instance descriptor without ctypes or native mutation.
    bases = target.__bases__
    hooks, calls = [], []
    root_state = raw_state(logging.root)

    def getter(value):
        hooks.append("dict getter")
        return raw_state(value)

    shim = type("SyntheticUnsupportedDictBase", (bases[0],), {"__dict__": descriptor(kind, getter)})
    try:
        # __bases__ is a type-owned descriptor, not an entry of target.__dict__;
        # restore its saved value directly instead of MonkeyPatch's delattr
        # fallback for attributes absent from the target class namespace.
        target.__bases__ = (shim, *bases[1:])
        receipt = invoke(synthetic, calls)
        assert receipt.outcome == "ERROR" and hooks == [] and calls == []
        assert raw_state(logging.root) is root_state
    finally:
        # Also restore in the unlikely event the assertion helper itself fails.
        target.__bases__ = bases
    assert target.__bases__ == bases and raw_state(logging.root) is root_state


@pytest.mark.parametrize("placement", ["default", "instance", "class", "both"])
def test_plain_same_manager_dispatch_succeeds_and_original_configuration_is_restored(synthetic, monkeypatch, placement):
    manager = type.__getattribute__(logging.Logger, "__dict__")["manager"]
    before = dict(type.__getattribute__(logging.RootLogger, "__dict__"))
    calls = []
    with root_manager_field(manager, placement in {"instance", "both"}) as state:
        with monkeypatch.context() as patch:
            if placement in {"class", "both"}:
                patch.setattr(logging.RootLogger, "manager", manager, raising=False)
            receipt = invoke(synthetic, calls)
            assert receipt.outcome == "CHECKS_COMPLETED" and receipt.checks.requested_field_coverage == "PASS"
            assert calls == ["resolver", "fetcher"]
            assert logging.root.manager is manager and logging.Logger.manager is manager
            assert (state.get("manager") is manager) if placement in {"instance", "both"} else ("manager" not in state)
        assert dict(type.__getattribute__(logging.RootLogger, "__dict__")) == before


def test_root_getter_cannot_emit_fixture_values_before_output_isolation(synthetic, monkeypatch, capsys):
    manager = type.__getattribute__(logging.Logger, "__dict__")["manager"]
    resolution, snapshot = synthetic[3:]
    logger = logging.Logger("SYNTHETIC_PRECONFIGURED_ROOT_GETTER_LOG", logging.WARNING)
    logger.propagate = False
    # Bind the original captured stderr before API entry. A getter that executes
    # in preflight would reach this stream before the output sink is installed.
    handler = logging.StreamHandler(sys.stderr)
    logger.addHandler(handler)
    marker = f"SYNTHETIC_ONLY_SID={resolution.aliases[0].sid} CLOSE={snapshot.bars[0].close} COUNT={len(snapshot.bars)} HASH="
    marker += hashlib.sha256(snapshot.model_dump_json().encode()).hexdigest()
    hooks, calls = [], []

    def getter(_):
        hooks.append("root.manager getter")
        logger.warning(marker)
        return manager

    try:
        with root_manager_field(manager, True):
            with monkeypatch.context() as patch:
                patch.setattr(logging.RootLogger, "manager", property(getter), raising=False)
                receipt = invoke(synthetic, calls)
                captured = capsys.readouterr()
                assert receipt.outcome == "ERROR" and hooks == [] and calls == []
                assert captured.out == captured.err == ""
                assert marker not in receipt.model_dump_json()
    finally:
        handler.close()


def test_adapter_logger_descriptor_is_rejected_before_probing_legitimate_instance_field(synthetic, monkeypatch):
    logger = logging.Logger("SYNTHETIC_ADAPTER_DESCRIPTOR", logging.WARNING)
    adapter = logging.LoggerAdapter(logger, {})
    adapter_state = object.__getattribute__(adapter, "__dict__")
    assert adapter_state["logger"] is logger
    hooks, calls = [], []

    def getter(_):
        hooks.append("adapter.logger getter")
        return logger

    with monkeypatch.context() as patch:
        patch.setattr(logging.LoggerAdapter, "logger", property(getter), raising=False)
        receipt = invoke(synthetic, calls)
        assert receipt.outcome == "ERROR" and hooks == [] and calls == []
        assert adapter_state["logger"] is logger
    assert adapter.logger is logger


@pytest.mark.parametrize("file", [False, True])
def test_handler_stream_descriptor_is_rejected_before_probing_legitimate_instance_field(synthetic, monkeypatch, tmp_path, file):
    path = tmp_path / "SYNTHETIC_DESCRIPTOR_ONLY.log"
    stream = io.StringIO()
    handler = logging.FileHandler(path, delay=True) if file else logging.StreamHandler(stream)
    state = STATE_SLOT.__get__(handler, type(handler))
    original_stream = state["stream"]
    hooks, calls = [], []

    def getter(_):
        hooks.append("handler.stream getter")
        return original_stream

    target = logging.FileHandler if file else logging.StreamHandler
    try:
        with monkeypatch.context() as patch:
            patch.setattr(target, "stream", property(getter), raising=False)
            receipt = invoke(synthetic, calls)
            assert receipt.outcome == "ERROR" and hooks == [] and calls == []
            assert state["stream"] is original_stream
        assert handler.stream is original_stream and stream.getvalue() == "" and not path.exists()
    finally:
        handler.close()
