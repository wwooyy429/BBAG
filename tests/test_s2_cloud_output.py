"""S2-005: actual standard logging routes with synthetic data only."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import gc
import io
import logging
import threading
import warnings

import pytest

from bbag.s2_cloud_check import run_cloud_data_check
from test_s2_cloud_check import synthetic  # shared literal SYNTHETIC fixture


def call(inputs, resolver, fetcher):
    plan, _, guard, _, _ = inputs
    return run_cloud_data_check(plan, guard=guard, resolver=resolver, fetcher=fetcher)


def originals():
    import bbag.s2_cloud_check as implementation
    return [(cls, name, cls.__dict__[name]) for cls, name in implementation._LOGGING_METHODS]


@pytest.mark.parametrize("kind", ["registered", "direct"])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("callback", ["resolver", "fetcher"])
@pytest.mark.parametrize("fails", [False, True])
def test_preconfigured_standard_handler_success_failure_and_cache_restoration(synthetic, kind, warm, stream, callback, fails):
    out, err = io.StringIO(), io.StringIO()
    name = "bbag.synthetic.s2_005.matrix"
    logger = logging.getLogger(name) if kind == "registered" else logging.Logger(name)
    previous = dict(logger.__dict__)
    previous_handlers = list(logger.handlers)
    previous_cache = dict(logger._cache)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    handler = logging.StreamHandler(out if stream == "stdout" else err)
    logger.addHandler(handler)
    if warm:
        assert logger.isEnabledFor(logging.WARNING)
    cache = dict(logger._cache)
    config = (logger.level, logger.disabled, logger.propagate, list(logger.handlers), list(logger.filters), logging.root.manager.disable)
    methods = originals()
    resolution, snapshot = synthetic[3:]
    secret = f"{resolution.aliases[0].sid} close={snapshot.bars[0].close} count={len(snapshot.bars)} digest="
    secret += hashlib.sha256(snapshot.model_dump_json().encode()).hexdigest()
    visited = []

    def noisy(*_):
        visited.append(callback)
        logger.warning(secret)
        # Also exercise a nonstandard integer level with the same stock API.
        logger.log(10**30, secret)
        if fails:
            raise ValueError(secret)
        return resolution if callback == "resolver" else snapshot

    try:
        with redirect_stdout(out), redirect_stderr(err):
            receipt = call(synthetic, noisy if callback == "resolver" else lambda *_: resolution,
                noisy if callback == "fetcher" else lambda *_: snapshot)
        assert visited == [callback]
        assert receipt.outcome == ("ERROR" if fails else "CHECKS_COMPLETED")
        assert out.getvalue() == err.getvalue() == ""
        assert secret not in receipt.model_dump_json()
        assert handler.stream is (out if stream == "stdout" else err)
        assert logger._cache == cache
        assert config == (logger.level, logger.disabled, logger.propagate, list(logger.handlers), list(logger.filters), logging.root.manager.disable)
        assert all(cls.__dict__[name] is method for cls, name, method in methods)
        logger.warning("AFTER_SCOPE_CONTROL_ONLY")
        assert (out.getvalue()+err.getvalue()).strip() == "AFTER_SCOPE_CONTROL_ONLY"
    finally:
        previous["handlers"][:] = previous_handlers
        previous["_cache"].clear()
        previous["_cache"].update(previous_cache)
        logger.__dict__.clear()
        logger.__dict__.update(previous)
        handler.close()


@pytest.mark.parametrize("cancel", [SystemExit, KeyboardInterrupt, GeneratorExit])
@pytest.mark.parametrize("callback", ["resolver", "fetcher"])
def test_baseexception_message_cannot_escape_and_cleanup_always_runs(synthetic, cancel, callback, capsys):
    methods = originals()
    visited = []
    def bad(*_):
        visited.append(callback)
        logging.warning("SYNTHETIC_SECRET_BEFORE_CANCEL")
        raise cancel("SYNTHETIC_SECRET_CANCEL_MESSAGE")
    receipt = call(synthetic, bad if callback == "resolver" else lambda *_: synthetic[3],
        bad if callback == "fetcher" else lambda *_: synthetic[4])
    assert visited == [callback] and receipt.outcome == "ERROR"
    assert "SECRET" not in receipt.model_dump_json() and capsys.readouterr() == ("", "")
    assert all(cls.__dict__[name] is method for cls, name, method in methods)
    # The process gate is not left locked after any cancellation.
    assert call(synthetic, lambda *_: synthetic[3], lambda *_: synthetic[4]).outcome == "CHECKS_COMPLETED"


def test_module_root_last_resort_and_plain_adapter_do_not_configure_or_emit(synthetic, monkeypatch):
    monkeypatch.setattr(logging.root, "handlers", [])
    before = logging.lastResort
    logger = logging.Logger("SYNTHETIC_DIRECT_NO_HANDLERS", logging.DEBUG)
    logger.propagate = False
    adapter = logging.LoggerAdapter(logger, {"source": "SYNTHETIC"})
    sink = io.StringIO()
    last = logging.StreamHandler(sink)
    monkeypatch.setattr(logging, "lastResort", last)
    cache = dict(logger._cache)
    cached_warning = logging.warning
    def resolver(*_):
        for method in (logging.debug, logging.info, logging.warning, logging.warn, logging.error,
                       logging.exception, logging.critical, logging.fatal):
            method("SYNTHETIC_SECRET")
        logging.log(logging.CRITICAL, "SYNTHETIC_SECRET")
        cached_warning("SYNTHETIC_SECRET_CACHED_MODULE_FUNCTION")
        logger.warning("SYNTHETIC_SECRET")
        adapter.warning("SYNTHETIC_SECRET")
        return synthetic[3]
    receipt = call(synthetic, resolver, lambda *_: synthetic[4])
    assert receipt.outcome == "CHECKS_COMPLETED" and not sink.getvalue()
    assert logging.root.handlers == [] and logging.lastResort is last and logger._cache == cache
    assert before is not last
    last.close()


@pytest.mark.parametrize("file", [False, True])
def test_existing_stock_stream_is_retargeted_even_for_cached_emit(synthetic, tmp_path, file):
    sink = io.StringIO()
    path = tmp_path/"SYNTHETIC_ONLY.log"
    handler = logging.FileHandler(path, delay=True) if file else logging.StreamHandler(sink)
    old_stream = handler.stream
    emit = handler.emit  # Real bound standard function captured before entry.
    record = logging.LogRecord("synthetic", logging.WARNING, __file__, 1, "SYNTHETIC_SECRET", (), None)
    def resolver(*_):
        emit(record)
        return synthetic[3]
    try:
        receipt = call(synthetic, resolver, lambda *_: synthetic[4])
        assert receipt.outcome == "CHECKS_COMPLETED" and handler.stream is old_stream
        assert sink.getvalue() == "" and not path.exists()
    finally:
        handler.close()


@pytest.mark.parametrize("reason", ["logger_class", "factory", "method", "instance", "class_warning",
    "adapter_class", "adapter_instance", "adapter_proxy", "manager_factory", "root_class_warning"])
def test_unsupported_logging_configuration_rejects_before_first_callback(synthetic, monkeypatch, reason):
    logger = logging.Logger("SYNTHETIC_UNSUPPORTED")
    if reason == "logger_class":
        class Unsupported(logging.Logger):
            pass
        monkeypatch.setattr(logging, "_loggerClass", Unsupported)
    elif reason == "manager_factory":
        class Unsupported(logging.Logger):
            pass
        monkeypatch.setattr(logging.root.manager, "loggerClass", Unsupported)
    elif reason == "factory":
        monkeypatch.setattr(logging, "_logRecordFactory", lambda *_: None)
    elif reason == "method":
        monkeypatch.setattr(logging.Logger, "_log", lambda *_a, **_k: None)
    elif reason == "instance":
        monkeypatch.setattr(logger, "warning", lambda *_: None)
    elif reason == "class_warning":
        monkeypatch.setattr(logging.Logger, "warning", lambda *_: None)
    elif reason == "root_class_warning":
        monkeypatch.setattr(logging.RootLogger, "warning", lambda *_: None, raising=False)
    elif reason == "adapter_class":
        monkeypatch.setattr(logging.LoggerAdapter, "warning", lambda *_: None)
    else:
        adapter = logging.LoggerAdapter(logger if reason == "adapter_instance" else object(), {})
        if reason == "adapter_instance":
            monkeypatch.setattr(adapter, "isEnabledFor", lambda *_: True)
    calls = []
    receipt = call(synthetic, lambda *_: calls.append("resolver"), lambda *_: calls.append("fetcher"))
    assert calls == [] and receipt.outcome == "ERROR"


def test_existing_background_thread_and_worker_entry_reject_before_callbacks(synthetic):
    ready, stop = threading.Event(), threading.Event()
    def background():
        ready.set()
        stop.wait()
    thread = threading.Thread(target=background)
    thread.start()
    ready.wait()
    calls = []
    try:
        receipt = call(synthetic, lambda *_: calls.append("resolver"), lambda *_: calls.append("fetcher"))
        assert receipt.outcome == "ERROR" and calls == []
    finally:
        stop.set()
        thread.join()
    worker_results = []
    worker = threading.Thread(target=lambda: worker_results.append(call(synthetic,
        lambda *_: calls.append("resolver"), lambda *_: calls.append("fetcher")).outcome))
    worker.start()
    worker.join()
    assert worker_results == ["ERROR"] and calls == []


def test_callback_cannot_start_thread_which_logs_after_scope(synthetic):
    launched = []
    def resolver(*_):
        threading.Thread(target=lambda: launched.append("late IO")).start()
        return synthetic[3]
    receipt = call(synthetic, resolver, lambda *_: synthetic[4])
    assert receipt.outcome == "ERROR" and launched == []


def test_nested_call_fails_fast_without_restoring_outer_gate(synthetic):
    nested, invoked = [], []
    def resolver(*_):
        nested.append(call(synthetic, lambda *_: invoked.append("nested resolver"),
            lambda *_: invoked.append("nested fetcher")).outcome)
        logging.warning("SYNTHETIC_SECRET_AFTER_NESTED")
        return synthetic[3]
    receipt = call(synthetic, resolver, lambda *_: synthetic[4])
    assert receipt.outcome == "CHECKS_COMPLETED" and nested == ["ERROR"] and invoked == []


def test_deprecated_warn_does_not_mutate_capturewarnings_configuration(synthetic):
    logger = logging.Logger("SYNTHETIC_DEPRECATED_WARN", logging.WARNING)
    adapter = logging.LoggerAdapter(logger, {})
    captured = logging.getLogger("py.warnings")
    original_handlers = list(captured.handlers)
    original_show = warnings.showwarning
    original_logging_show = logging._warnings_showwarning
    try:
        captured.handlers[:] = []
        logging.captureWarnings(True)
        def resolver(*_):
            logger.warn("SYNTHETIC_SECRET")
            adapter.warn("SYNTHETIC_SECRET")
            return synthetic[3]
        receipt = call(synthetic, resolver, lambda *_: synthetic[4])
        assert receipt.outcome == "CHECKS_COMPLETED" and captured.handlers == []
    finally:
        captured.handlers[:] = original_handlers
        warnings.showwarning = original_show
        logging._warnings_showwarning = original_logging_show


def test_frozen_gc_inventory_is_rejected_before_callbacks(synthetic):
    freeze_here = gc.get_freeze_count() == 0
    calls = []
    try:
        if freeze_here:
            gc.freeze()
        assert gc.get_freeze_count() > 0
        receipt = call(synthetic, lambda *_: calls.append("resolver"), lambda *_: calls.append("fetcher"))
        assert receipt.outcome == "ERROR" and calls == []
    finally:
        if freeze_here:
            gc.unfreeze()
