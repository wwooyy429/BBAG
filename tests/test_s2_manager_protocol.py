"""SYNTHETIC value-protocol dispatch oracles; no SDK, task or budget changes."""
from contextlib import contextmanager
import logging

import pytest

from bbag.s2_cloud_check import run_cloud_data_check
from test_s2_cloud_check import synthetic


PROTOCOLS = ("__get__", "__set__", "__delete__")
LOGGER_SLOT = type.__getattribute__(logging.Filterer, "__dict__")["__dict__"]
MANAGER_SLOT = type.__getattribute__(logging.Manager, "__dict__")["__dict__"]


def shared_manager():
    # Logger.manager is the descriptor VALUE under test. Never retrieve it by
    # ordinary attribute access while Manager's type has an injected protocol.
    return type.__getattribute__(logging.Logger, "__dict__")["manager"]


@contextmanager
def root_instance_manager(manager, present):
    state = LOGGER_SLOT.__get__(logging.root, type(logging.root))
    existed, old = "manager" in state, state.get("manager")
    if present:
        state["manager"] = manager
    else:
        state.pop("manager", None)
    try:
        yield state
    finally:
        if existed:
            state["manager"] = old
        else:
            state.pop("manager", None)


def invoke(inputs, events):
    plan, _, guard, resolution, snapshot = inputs

    def resolver(*_):
        events.append("resolver")
        # This standard dispatch caused the independently observed residual;
        # it is not a direct call to a saved or nonstock implementation.
        logging.getLogger("SYNTHETIC_MANAGER_PROTOCOL_CALLBACK")
        return resolution

    def fetcher(*_):
        events.append("fetcher")
        return snapshot

    return run_cloud_data_check(plan, guard=guard, resolver=resolver, fetcher=fetcher)


def protocol_function(name, events):
    def ordinary(self, *args, **kwargs):
        events.append(name)
        return self if name == "__get__" else None
    return ordinary


@pytest.mark.parametrize("name", PROTOCOLS)
@pytest.mark.parametrize("none", [False, True])
@pytest.mark.parametrize("root_field", [False, True])
def test_exact_manager_class_protocol_rejected_before_standard_dispatch(synthetic, monkeypatch, name, none, root_field):
    manager = shared_manager()
    assert type(manager) is logging.Manager
    namespace_before = dict(type.__getattribute__(logging.Manager, "__dict__"))
    protocol_events, callbacks = [], []
    value = None if none else protocol_function(name, protocol_events)
    with root_instance_manager(manager, root_field) as state:
        with monkeypatch.context() as patch:
            # Install the protocol only after all setup reads. An exact Manager
            # object can become a descriptor while retaining type and identity.
            patch.setattr(logging.Manager, name, value, raising=False)
            assert shared_manager() is manager
            receipt = invoke(synthetic, callbacks)
            assert receipt.outcome == "ERROR" and receipt.checks is None
            assert callbacks == [] and protocol_events == []
            assert (state.get("manager") is manager) if root_field else ("manager" not in state)
        assert dict(type.__getattribute__(logging.Manager, "__dict__")) == namespace_before


@pytest.mark.parametrize("root_field", [False, True])
def test_stock_same_manager_success_and_restoration(synthetic, root_field):
    manager, callbacks = shared_manager(), []
    namespace_before = dict(type.__getattribute__(logging.Manager, "__dict__"))
    with root_instance_manager(manager, root_field):
        receipt = invoke(synthetic, callbacks)
        assert receipt.outcome == "CHECKS_COMPLETED" and receipt.checks.requested_field_coverage == "PASS"
        assert callbacks == ["resolver", "fetcher"] and shared_manager() is manager
    assert dict(type.__getattribute__(logging.Manager, "__dict__")) == namespace_before


@pytest.mark.parametrize("name", PROTOCOLS)
@pytest.mark.parametrize("none", [False, True])
def test_ordinary_manager_instance_key_does_not_define_descriptor_protocol(synthetic, name, none):
    manager = shared_manager()
    state = MANAGER_SLOT.__get__(manager, type(manager))
    existed, old = name in state, state.get(name)
    protocol_events, callbacks = [], []
    value = None if none else protocol_function(name, protocol_events)
    state[name] = value
    try:
        # Special-method protocol lookup is on the type/MRO, not the instance.
        assert name not in type.__getattribute__(logging.Manager, "__dict__")
        receipt = invoke(synthetic, callbacks)
        assert receipt.outcome == "CHECKS_COMPLETED" and receipt.checks.requested_field_coverage == "PASS"
        assert callbacks == ["resolver", "fetcher"] and protocol_events == []
        assert state[name] is value
    finally:
        if existed:
            state[name] = old
        else:
            state.pop(name, None)


@pytest.mark.parametrize("target", [logging.Logger, logging.RootLogger])
@pytest.mark.parametrize("name", PROTOCOLS)
@pytest.mark.parametrize("none", [False, True])
def test_neighbor_stock_logger_type_inherited_protocol_is_rejected_statically(synthetic, target, name, none):
    # Manager directly inherits object, so inserting a heap base there is not a
    # legal same-layout mutation on supported CPython. Use the legal neighboring
    # Logger/RootLogger MRO carrier, and test its empty-base positive separately.
    bases = target.__bases__
    protocol_events, callbacks = [], []
    value = None if none else protocol_function(name, protocol_events)
    shim = type("SyntheticUnsupportedProtocolBase", (bases[0],), {name: value})
    try:
        target.__bases__ = (shim, *bases[1:])
        assert name not in type.__getattribute__(target, "__dict__")
        receipt = invoke(synthetic, callbacks)
        assert receipt.outcome == "ERROR" and receipt.checks is None
        assert callbacks == [] and protocol_events == []
    finally:
        target.__bases__ = bases
    assert target.__bases__ == bases


@pytest.mark.parametrize("target", [logging.Logger, logging.RootLogger])
def test_neighbor_stock_logger_empty_mro_shim_remains_supported(synthetic, target):
    bases = target.__bases__
    shim = type("SyntheticEmptyProtocolBase", (bases[0],), {})
    callbacks = []
    try:
        target.__bases__ = (shim, *bases[1:])
        receipt = invoke(synthetic, callbacks)
        assert receipt.outcome == "CHECKS_COMPLETED" and receipt.checks.requested_field_coverage == "PASS"
        assert callbacks == ["resolver", "fetcher"]
    finally:
        target.__bases__ = bases
    assert target.__bases__ == bases


@pytest.mark.parametrize("target", [logging.Logger, logging.RootLogger])
@pytest.mark.parametrize("attribute", ["__dict__", "__mro__"])
def test_unknown_metaclass_in_legal_mro_rejected_without_metadata_getter(synthetic, target, attribute):
    bases = target.__bases__
    protocol_events, callbacks = [], []
    # Calling type.__getattribute__ on a class does not bypass a data
    # descriptor on its metaclass. The fixture getter delegates directly to the
    # original type slot, so it remains a harmless actual dispatch witness.
    slot = type.__dict__[attribute]

    def metadata_getter(cls):
        protocol_events.append("metaclass " + attribute)
        return slot.__get__(cls, type(cls))

    meta = type("SyntheticUnknownLoggingMeta", (type,), {attribute: property(metadata_getter)})
    shim = meta("SyntheticMetaLoggingBase", (bases[0],), {})
    assert protocol_events == []
    try:
        target.__bases__ = (shim, *bases[1:])
        receipt = invoke(synthetic, callbacks)
        assert receipt.outcome == "ERROR" and receipt.checks is None
        assert callbacks == [] and protocol_events == []
    finally:
        target.__bases__ = bases
    assert target.__bases__ == bases


def test_root_name_property_rejected_before_standard_getlogger_reads_it(synthetic, monkeypatch):
    events, callbacks = [], []
    root_state = LOGGER_SLOT.__get__(logging.root, type(logging.root))
    actual_name = root_state["name"]

    def getter(_):
        events.append("root.name getter")
        return actual_name

    with monkeypatch.context() as patch:
        patch.setattr(logging.RootLogger, "name", property(getter), raising=False)
        receipt = invoke(synthetic, callbacks)
        assert receipt.outcome == "ERROR" and callbacks == [] and events == []
        assert root_state["name"] == actual_name


@contextmanager
def stock_existing_parent():
    manager = shared_manager()
    registry = MANAGER_SLOT.__get__(manager, type(manager))["loggerDict"]
    previous = dict(registry)
    placeholders = [(value, dict(value.loggerMap)) for value in registry.values()
                    if type(value) is logging.PlaceHolder]
    parent_name = "SYNTHETIC.protocol_existing_parent"
    try:
        parent = logging.getLogger(parent_name)
        child_name = parent_name + ".new_child"
        registry.pop(child_name, None)
        yield parent, child_name
    finally:
        registry.clear()
        registry.update(previous)
        for value, state in placeholders:
            value.loggerMap.clear()
            value.loggerMap.update(state)


def dispatch_with_parent(inputs, child_name, callbacks, children):
    plan, _, guard, resolution, snapshot = inputs

    def resolver(*_):
        callbacks.append("resolver")
        children.append(logging.getLogger(child_name))
        return resolution

    def fetcher(*_):
        callbacks.append("fetcher")
        return snapshot

    return run_cloud_data_check(plan, guard=guard, resolver=resolver, fetcher=fetcher)


@pytest.mark.parametrize("name", ["__bool__", "__len__"])
@pytest.mark.parametrize("none", [False, True])
def test_logger_truthiness_protocol_rejected_before_existing_parent_dispatch(synthetic, monkeypatch, name, none):
    events, callbacks, children = [], [], []

    def truthiness(_):
        events.append(name)
        return True if name == "__bool__" else 1

    with stock_existing_parent():
        with monkeypatch.context() as patch:
            patch.setattr(logging.Logger, name, None if none else truthiness, raising=False)
            receipt = dispatch_with_parent(synthetic, "SYNTHETIC.protocol_existing_parent.new_child", callbacks, children)
            assert receipt.outcome == "ERROR" and receipt.checks is None
            assert callbacks == [] and children == [] and events == []


def test_stock_existing_parent_dispatch_successfully_builds_child_and_data(synthetic):
    callbacks, children = [], []
    with stock_existing_parent() as (parent, child_name):
        receipt = dispatch_with_parent(synthetic, child_name, callbacks, children)
        assert receipt.outcome == "CHECKS_COMPLETED" and receipt.checks.requested_field_coverage == "PASS"
        assert callbacks == ["resolver", "fetcher"] and len(children) == 1
        assert children[0].parent is parent
