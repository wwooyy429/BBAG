"""Independent S1 syntax, type, complexity and causality audit examples."""

from dataclasses import FrozenInstanceError

import pytest

from bbag.expression import Node, audit_expression


FIELDS = {"open", "close", "volume"}


def test_existing_placeholder_return_keyword_has_exact_audit() -> None:
    audit = audit_expression("neg(return(close, 5))", FIELDS,
                             declared_fields=["close"], declared_lookback_sessions=6)
    assert audit.root == Node("call", "neg", (Node("call", "return", (Node("field", "close"), Node("constant", 5.0))),))
    assert (audit.fields, audit.node_count, audit.max_depth, audit.parameter_count,
            audit.lookback_sessions, audit.normalized_expression) == (("close",), 4, 3, 1, 6, "neg(return(close,5))")
    with pytest.raises(FrozenInstanceError):
        audit.lookback_sessions = 1


@pytest.mark.parametrize("expression,normalized,lookback,nodes,depth,parameters", [
    ("close", "close", 1, 1, 1, 0),
    ("close + open * 2", "add(close,mul(open,2))", 1, 5, 3, 1),
    ("(close + open) * 2", "mul(add(close,open),2)", 1, 5, 3, 1),
    ("close - open - 2", "sub(sub(close,open),2)", 1, 5, 3, 1),
    ("close / open / 2", "div(div(close,open),2)", 1, 5, 3, 1),
    ("-close + +open", "add(neg(close),open)", 1, 4, 3, 0),
    ("rolling_mean(delay(close,2),3)", "rolling_mean(delay(close,2),3)", 5, 5, 3, 2),
    ("return(rolling_std(close,3),2)", "return(rolling_std(close,3),2)", 5, 5, 3, 2),
    ("cs_rank(ts_rank(close,4))", "cs_rank(ts_rank(close,4))", 4, 4, 3, 1),
    ("add(close,mul(2,3))", "add(close,mul(2,3))", 1, 5, 3, 2),
    ("div(close,1e2)", "div(close,100)", 1, 3, 2, 1),
    ("close + .5", "add(close,0.5)", 1, 3, 2, 1),
])
def test_hand_derived_tree_metrics_and_normalization(
    expression: str, normalized: str, lookback: int, nodes: int, depth: int, parameters: int,
) -> None:
    audit = audit_expression(expression, FIELDS)
    assert (audit.normalized_expression, audit.lookback_sessions, audit.node_count,
            audit.max_depth, audit.parameter_count) == (normalized, lookback, nodes, depth, parameters)


def test_fields_are_sorted_and_explicit_declarations_are_checked() -> None:
    audit = audit_expression("open + close", FIELDS, declared_fields={"open", "close"}, declared_lookback_sessions=4)
    assert audit.fields == ("close", "open")
    assert audit.lookback_sessions == 1
    for declared in (["close"], ["open", "close", "volume"], ["open", "close", "close"]):
        with pytest.raises(ValueError):
            audit_expression("open+close", FIELDS, declared_fields=declared)
    with pytest.raises(ValueError, match="smaller"):
        audit_expression("return(close,5)", FIELDS, declared_lookback_sessions=5)


@pytest.mark.parametrize("expression", [
    "", " ", "close.value", "close[0]", "close[-1]", "__import__('os')", "open('secret')",
    "lambda x: x", "[close for x in volume]", "(x for x in close)", "close if True else open",
    "True", "False", "None", "close and open", "close > open", "close == open", "close=1",
    "close ** 2", "close // 2", "close % 2", "close;open", "close#comment", "close,open",
    "close()", "neg()", "neg(close,open)", "add(close)", "add(close,open,volume)", "neg(close,)",
    "lead(close,1)", "future_return(close,1)", "delay(close,-1)", "return(close,0)",
    "delay(close,1.0)", "delay(close,1e0)", "delay(close,+1)", "delay(close,1+1)",
    "delay(close,(1))", "rolling_mean(close,'5')", "rolling_std(close,True)",
    "ts_rank(close,65)", "cs_rank(close,1)", "neg(label)", "close + future_close",
    "nan", "inf", "close + NaN", "close + 1e999", "close + 1000000000001",
    "close + 0x10", "close + 1_000", "close + \\", "收盘", "close @ open",
])
def test_reject_unsafe_future_ill_typed_or_unsupported_expressions(expression: str) -> None:
    with pytest.raises(ValueError):
        audit_expression(expression, FIELDS)


@pytest.mark.parametrize("expression", ["1", "neg(1)", "add(1,2)", "rolling_mean(1,3)",
                                         "add(close,delay(1,2))", "mul(close,cs_rank(1))"])
def test_factor_and_series_operators_need_field_dependence(expression: str) -> None:
    with pytest.raises(ValueError, match="field"):
        audit_expression(expression, FIELDS)


@pytest.mark.parametrize("option,value", [
    ("max_nodes", False), ("max_nodes", 0), ("max_nodes", 4097),
    ("max_depth", 0), ("max_depth", 65), ("max_parameters", -1),
    ("max_window", 0), ("max_window", 1.0), ("max_lookback_sessions", True),
    ("declared_lookback_sessions", False), ("declared_lookback_sessions", 257),
])
def test_limits_have_strict_integer_types_and_bounded_ranges(option: str, value: object) -> None:
    with pytest.raises(ValueError):
        audit_expression("close", FIELDS, **{option: value})


def test_each_literal_occurrence_counts_and_exact_budgets_accept() -> None:
    audit = audit_expression("close + 2 + 2", FIELDS, max_nodes=5, max_depth=3, max_parameters=2)
    assert audit.parameter_count == 2
    for options in ({"max_nodes": 4}, {"max_depth": 2}, {"max_parameters": 1}):
        with pytest.raises(ValueError, match="exceeds"):
            audit_expression("close + 2 + 2", FIELDS, **options)
    assert audit_expression("close", FIELDS, max_nodes=1, max_depth=1, max_parameters=0).node_count == 1


def test_nested_lookback_accumulates_and_budget_is_not_a_maximum_window_only() -> None:
    expression = "rolling_mean(return(close,5),4)"
    assert audit_expression(expression, FIELDS, max_lookback_sessions=9).lookback_sessions == 9
    with pytest.raises(ValueError, match="max_lookback_sessions"):
        audit_expression(expression, FIELDS, max_lookback_sessions=8)
    assert audit_expression("delay(close,64)", FIELDS).lookback_sessions == 65
    assert audit_expression("rolling_std(close,1)", FIELDS).lookback_sessions == 1


@pytest.mark.parametrize("fields", ["close", ["close", "close"], ["close", 1], ["return"], ["close.price"], ["收盘"]])
def test_field_dictionary_requires_safe_unique_identifiers(fields: object) -> None:
    with pytest.raises(ValueError):
        audit_expression("close", fields)


def test_large_flat_and_nested_inputs_fail_with_controlled_errors() -> None:
    with pytest.raises(ValueError, match="max_nodes"):
        audit_expression("+".join(["close"] * 300), FIELDS)
    with pytest.raises(ValueError, match="max_depth"):
        audit_expression("+".join(["close"] * 100), FIELDS, max_nodes=1000)
    with pytest.raises(ValueError, match="syntax nesting"):
        audit_expression("(" * 70 + "close" + ")" * 70, FIELDS)
    with pytest.raises(ValueError, match="syntax nesting"):
        audit_expression("-" * 70 + "close", FIELDS)
    with pytest.raises(ValueError, match="4096"):
        audit_expression(" " * 4096 + "close", FIELDS)


def test_equivalent_surface_syntax_has_identical_normalized_audit() -> None:
    infix = audit_expression(" (close + open) / 2.0 ", FIELDS)
    calls = audit_expression("div(add(close,open),2)", FIELDS)
    assert infix == calls
