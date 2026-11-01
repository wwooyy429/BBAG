"""Synthetic S2-006/N02 host-state oracles; no SDK, grant or real history I/O."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import logging
import warnings

import pytest

from bbag.s2_cloud_check import run_cloud_data_check
from test_s2_cloud_check import synthetic


# Stdlib Manager operations are tested through effective instance/class lookup,
# independently of any implementation-owned list of inspected methods.
MANAGER_METHODS = (
    "getLogger", "setLoggerClass", "setLogRecordFactory", "_fixupParents",
    "_fixupChildren", "_clear_cache", "__init__",
)


def call(inputs, events):
    plan, _, guard, resolution, snapshot = inputs

    def resolver(*_):
        events.append("resolver")
        return resolution

    def fetcher(*_):
        events.append("fetcher")
        return snapshot

    return run_cloud_data_check(plan, guard=guard, resolver=resolver, fetcher=fetcher)


@pytest.mark.parametrize("method", MANAGER_METHODS)
@pytest.mark.parametrize("where", ["instance", "class"])
def test_nonstock_effective_manager_methods_reject_before_first_data_callback(synthetic, monkeypatch, method, where):
    manager = logging.root.manager
    methods_called, callbacks = [], []
    if where == "instance":
        original = getattr(manager, method)

        def delegate(*args, **kwargs):
            methods_called.append(method)
            return original(*args, **kwargs)

        monkeypatch.setattr(manager, method, delegate)
    else:
        original = getattr(logging.Manager, method)

        def delegate(self, *args, **kwargs):
            methods_called.append(method)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(logging.Manager, method, delegate)
    receipt = call(synthetic, callbacks)
    # No pytest.fail/assert sentinel is placed inside a swallowed callback.
    assert callbacks == [] and methods_called == []
    assert receipt.outcome == "ERROR" and receipt.checks is None
    assert receipt.error_category == "STANDARDIZATION_REJECTED"
    assert getattr(manager if where == "instance" else logging.Manager, method) is delegate


@pytest.mark.parametrize("factory", [None, logging.Logger])
def test_stock_manager_and_factory_remain_usable_not_blanket_rejected(synthetic, monkeypatch, factory):
    manager = logging.root.manager
    monkeypatch.setattr(manager, "loggerClass", factory)
    before = {name: getattr(logging.Manager, name) for name in MANAGER_METHODS}
    callbacks = []
    receipt = call(synthetic, callbacks)
    assert callbacks == ["resolver", "fetcher"] and receipt.outcome == "CHECKS_COMPLETED"
    assert receipt.checks.requested_field_coverage == "PASS"
    assert logging.root.manager is manager and manager.loggerClass is factory
    assert all(getattr(logging.Manager, name) is value for name, value in before.items())


@pytest.mark.parametrize("shadow", [False, True])
def test_unregistered_logger_effective_foreign_manager_is_rejected_before_data_io(synthetic, monkeypatch, shadow):
    logger = logging.Logger("SYNTHETIC_UNREGISTERED_FOREIGN_MANAGER", logging.WARNING)
    assert logger.name not in logging.root.manager.loggerDict
    foreign = logging.Manager(logging.root)
    monkeypatch.setattr(logger, "manager", foreign)
    invoked, callbacks = [], []
    if shadow:
        original = foreign.getLogger

        def delegate(*args, **kwargs):
            invoked.append("foreign.getLogger")
            return original(*args, **kwargs)

        monkeypatch.setattr(foreign, "getLogger", delegate)
    receipt = call(synthetic, callbacks)
    assert callbacks == [] and invoked == [] and receipt.outcome == "ERROR"
    assert logger.manager is foreign  # Rejection does not rewrite caller objects.


@contextmanager
def warning_host(initial):
    """Own and completely restore only a synthetic logging/warnings context."""
    manager = logging.root.manager
    registry = manager.loggerDict
    registry_before = dict(registry)
    placeholder_maps = [(value, dict(value.loggerMap)) for value in registry.values()
                        if type(value) is logging.PlaceHolder]
    original_show, original_delegate = warnings.showwarning, logging._warnings_showwarning
    original_last = logging.lastResort
    previous = registry.get("py.warnings")
    previous_state = dict(vars(previous)) if isinstance(previous, logging.Logger) else None
    previous_lists = ({key: list(vars(previous)[key]) for key in ("handlers", "filters")}
                      if previous_state is not None else None)
    previous_cache = dict(previous._cache) if previous_state is not None else None
    registry.pop("py.warnings", None)
    ordinary, fallback = io.StringIO(), io.StringIO()
    handler = logging.StreamHandler(ordinary)
    last = logging.StreamHandler(fallback)
    logger = None
    try:
        if initial != "absent":
            logger = logging.getLogger("py.warnings")
            logger.handlers = [handler] if initial == "handler" else []
            logger.filters = []
            logger._cache = {}
            logger.propagate = False
            logger.disabled = False
            logger.setLevel(logging.WARNING)
        logging.lastResort = last
        logging.captureWarnings(True)
        yield manager, logger, ordinary, fallback, handler
    finally:
        warnings.showwarning, logging._warnings_showwarning = original_show, original_delegate
        logging.lastResort = original_last
        manager.loggerDict = registry
        registry.clear()
        registry.update(registry_before)
        for placeholder, contents in placeholder_maps:
            placeholder.loggerMap.clear()
            placeholder.loggerMap.update(contents)
        if previous_state is not None:
            previous.__dict__.clear()
            previous.__dict__.update(previous_state)
            for key, value in previous_lists.items():
                vars(previous)[key][:] = value
            previous._cache.clear()
            previous._cache.update(previous_cache)
        handler.close()
        last.close()


def capture_logger_state(logger):
    if logger is None:
        return None
    return {"keys": set(vars(logger)), "handlers": logger.handlers, "handler_contents": list(logger.handlers),
        "filters": logger.filters, "filter_contents": list(logger.filters), "cache": logger._cache,
        "cache_contents": dict(logger._cache), "level": logger.level, "disabled": logger.disabled,
        "propagate": logger.propagate, "manager": logger.manager, "parent": logger.parent}


def assert_logger_state(logger, state):
    assert set(vars(logger)) == state["keys"]
    assert logger.handlers is state["handlers"] and logger.handlers == state["handler_contents"]
    assert logger.filters is state["filters"] and logger.filters == state["filter_contents"]
    assert logger._cache is state["cache"] and logger._cache == state["cache_contents"]
    assert (logger.level, logger.disabled, logger.propagate) == (state["level"], state["disabled"], state["propagate"])
    assert logger.manager is state["manager"] and logger.parent is state["parent"]


@pytest.mark.parametrize("initial", ["absent", "empty", "handler"])
@pytest.mark.parametrize("callback", ["resolver", "fetcher"])
@pytest.mark.parametrize("fails", [False, True])
def test_capturewarnings_ordinary_warning_restores_registry_handlers_and_post_scope_logging(synthetic, initial, callback, fails):
    plan, _, guard, resolution, snapshot = synthetic
    with warning_host(initial) as (manager, logger, ordinary, fallback, handler):
        # The absent state exercises py.warnings lazily created by stdlib warning
        # capture, rather than a developer-created warning logger.
        registry, members = manager.loggerDict, dict(manager.loggerDict)
        prior = capture_logger_state(logger)
        show, delegate = warnings.showwarning, logging._warnings_showwarning
        out, err, visited = io.StringIO(), io.StringIO(), []

        def resolver(*_):
            visited.append("resolver")
            if callback == "resolver":
                warnings.warn("SYNTHETIC_ONLY_CAPTURE_WARN_SID_PRICE_HASH", UserWarning)
                if fails:
                    raise ValueError("SYNTHETIC_ONLY_RESOLVER_FAILURE")
            return resolution

        def fetcher(*_):
            visited.append("fetcher")
            if callback == "fetcher":
                warnings.warn("SYNTHETIC_ONLY_CAPTURE_WARN_SID_PRICE_HASH", UserWarning)
                if fails:
                    raise ValueError("SYNTHETIC_ONLY_FETCHER_FAILURE")
            return snapshot

        with warnings.catch_warnings():
            warnings.simplefilter("always", UserWarning)
            filter_object, filters = warnings.filters, list(warnings.filters)
            with redirect_stdout(out), redirect_stderr(err):
                receipt = run_cloud_data_check(plan, guard=guard, resolver=resolver, fetcher=fetcher)
            assert warnings.filters is filter_object and warnings.filters == filters
        expected_calls = ["resolver"] if fails and callback == "resolver" else ["resolver", "fetcher"]
        assert visited == expected_calls
        assert receipt.outcome == ("ERROR" if fails else "CHECKS_COMPLETED")
        if not fails:
            assert receipt.checks.requested_field_coverage == "PASS"
        assert out.getvalue() == err.getvalue() == ordinary.getvalue() == fallback.getvalue() == ""
        assert "SYNTHETIC_ONLY" not in receipt.model_dump_json()
        assert warnings.showwarning is show and logging._warnings_showwarning is delegate
        assert logging.lastResort.stream is fallback and handler.stream is ordinary
        assert manager.loggerDict is registry and manager.loggerDict == members
        if initial == "absent":
            assert "py.warnings" not in registry
            post_logger = logging.getLogger("py.warnings")
            post_logger.setLevel(logging.WARNING)
            post_logger.propagate = False
        else:
            assert_logger_state(logger, prior)
            post_logger = logger
        post_logger.warning("POST_SCOPE_CONTROL_WARNING")
        if initial == "handler":
            assert ordinary.getvalue().strip() == "POST_SCOPE_CONTROL_WARNING" and fallback.getvalue() == ""
        else:
            assert fallback.getvalue().strip() == "POST_SCOPE_CONTROL_WARNING" and ordinary.getvalue() == ""
@pytest.mark.parametrize("owner,field", [(logging.Manager, name) for name in (
    "root", "loggerDict", "_disable", "emittedNoHandlerWarning", "loggerClass", "logRecordFactory")]
    + [(logging.PlaceHolder, "loggerMap")])
def test_stock_instance_field_class_descriptor_is_rejected_before_data(synthetic, monkeypatch, owner, field):
    plan, _, guard, _, _ = synthetic
    visits, dispatched = [], []
    def descriptor(instance):
        dispatched.append(field)
        return object.__getattribute__(instance, "__dict__").get(field)
    with monkeypatch.context() as patch:
        patch.setattr(owner, field, property(descriptor), raising=False)
        receipt = run_cloud_data_check(plan, guard=guard,
            resolver=lambda *_: visits.append("resolver"), fetcher=lambda *_: visits.append("fetcher"))
    assert receipt.outcome == "ERROR" and visits == [] and dispatched == []

