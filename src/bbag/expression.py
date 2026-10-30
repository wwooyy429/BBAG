"""Parse and audit a small, deterministic S1 expression language.

The grammar has numeric literals, approved field names, arithmetic and the
operators listed below. It never compiles or evaluates Python source. Windows
are bare positive integer literals; in particular ``5.0`` and ``1 + 4`` are not
window declarations. A session lookback includes the current observation.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
import keyword
import math
import re
from typing import Literal


@dataclass(frozen=True)
class Node:
    kind: Literal["field", "constant", "call"]
    value: str | float
    args: tuple[Node, ...] = ()


@dataclass(frozen=True)
class ExpressionAudit:
    root: Node
    fields: tuple[str, ...]
    node_count: int
    max_depth: int
    parameter_count: int
    lookback_sessions: int
    normalized_expression: str


_ARITIES = {
    "neg": 1, "add": 2, "sub": 2, "mul": 2, "div": 2,
    "delay": 2, "return": 2, "rolling_mean": 2, "rolling_std": 2,
    "ts_rank": 2, "cs_rank": 1,
}
_WINDOW_OPERATORS = {"delay", "return", "rolling_mean", "rolling_std", "ts_rank"}
_SERIES_OPERATORS = _WINDOW_OPERATORS | {"cs_rank"}
_INFIX = {"+": (1, "add"), "-": (1, "sub"), "*": (2, "mul"), "/": (2, "div")}
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<number>(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
    r"|(?P<identifier>[A-Za-z_][A-Za-z0-9_]*)|(?P<punctuation>[(),+*/-])"
)
_MAX_LENGTH = 4096
_MAX_SYNTAX_NESTING = 64
_MAX_ABS_LITERAL = 1e12


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    offset: int


def _fields(value: Collection[str], label: str) -> frozenset[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Collection):
        raise ValueError(f"{label} must be a collection of field names")
    names = list(value)
    if any(type(name) is not str or not _IDENTIFIER.fullmatch(name) or keyword.iskeyword(name)
           for name in names):
        raise ValueError(f"{label} must contain safe ASCII field identifiers, not Python keywords")
    if len(names) != len(set(names)):
        raise ValueError(f"{label} must not contain duplicate field names")
    return frozenset(names)


def _integer_limit(value: int, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")


class _Parser:
    def __init__(self, expression: str, allowed_fields: frozenset[str], max_nodes: int, max_window: int) -> None:
        self.allowed_fields = allowed_fields
        self.max_nodes = max_nodes
        self.max_window = max_window
        self.index = 0
        self.created_nodes = 0
        self.nesting = 0
        self.tokens: list[_Token] = []
        offset = 0
        while offset < len(expression):
            match = _TOKEN.match(expression, offset)
            if match is None:
                raise ValueError(f"unsupported expression token at offset {offset}")
            kind = match.lastgroup
            assert kind is not None
            if kind != "space":
                self.tokens.append(_Token(kind, match.group(), offset))
            offset = match.end()
        self.tokens.append(_Token("end", "", len(expression)))

    def _node(self, kind: Literal["field", "constant", "call"], value: str | float,
              args: tuple[Node, ...] = ()) -> Node:
        self.created_nodes += 1
        if self.created_nodes > self.max_nodes:
            raise ValueError("expression exceeds max_nodes")
        return Node(kind, value, args)

    def _take(self, text: str) -> None:
        if self.tokens[self.index].text != text:
            raise ValueError(f"expected {text!r} at offset {self.tokens[self.index].offset}")
        self.index += 1

    def _nested(self) -> None:
        self.nesting += 1
        if self.nesting > _MAX_SYNTAX_NESTING:
            raise ValueError("expression exceeds the syntax nesting limit of 64")

    def parse(self) -> Node:
        root = self._expression()
        if self.tokens[self.index].kind != "end":
            raise ValueError(f"unexpected expression token at offset {self.tokens[self.index].offset}")
        return root

    def _expression(self, minimum_precedence: int = 0) -> Node:
        left = self._primary()
        while self.tokens[self.index].text in _INFIX:
            precedence, operator = _INFIX[self.tokens[self.index].text]
            if precedence < minimum_precedence:
                break
            self.index += 1
            right = self._expression(precedence + 1)
            left = self._node("call", operator, (left, right))
        return left

    def _primary(self) -> Node:
        token = self.tokens[self.index]
        if token.text in {"-", "+"}:
            self.index += 1
            self._nested()
            value = self._primary()
            self.nesting -= 1
            return self._node("call", "neg", (value,)) if token.text == "-" else value
        if token.text == "(":
            self.index += 1
            self._nested()
            result = self._expression()
            self._take(")")
            self.nesting -= 1
            return result
        if token.kind == "number":
            self.index += 1
            number = float(token.text)
            if not math.isfinite(number) or abs(number) > _MAX_ABS_LITERAL:
                raise ValueError("numeric literals must be finite and have absolute value at most 1e12")
            return self._node("constant", number)
        if token.kind != "identifier":
            raise ValueError(f"expected a field, literal or call at offset {token.offset}")
        self.index += 1
        name = token.text
        if self.tokens[self.index].text != "(":
            if keyword.iskeyword(name) or name not in self.allowed_fields:
                raise ValueError(f"field is not allowed: {name}")
            return self._node("field", name)
        if name not in _ARITIES:
            raise ValueError(f"operator is not allowed: {name}")
        self.index += 1
        self._nested()
        arguments: list[Node] = []
        spans: list[list[_Token]] = []
        if self.tokens[self.index].text != ")":
            while True:
                start = self.index
                arguments.append(self._expression())
                spans.append(self.tokens[start:self.index])
                if self.tokens[self.index].text != ",":
                    break
                self.index += 1
        self._take(")")
        self.nesting -= 1
        if len(arguments) != _ARITIES[name]:
            raise ValueError(f"{name} requires exactly {_ARITIES[name]} arguments")
        if name in _WINDOW_OPERATORS:
            window = spans[1]
            if len(window) != 1 or window[0].kind != "number" or not re.fullmatch(r"[0-9]+", window[0].text):
                raise ValueError(f"{name} window must be a bare positive integer literal")
            sessions = int(window[0].text)
            if not 1 <= sessions <= self.max_window:
                raise ValueError(f"{name} window must be in [1, {self.max_window}]")
        return self._node("call", name, tuple(arguments))


def _number(value: float) -> str:
    return str(int(value)) if value.is_integer() else repr(value)


def audit_expression(
    expression: str,
    allowed_fields: Collection[str],
    *,
    max_nodes: int = 128,
    max_depth: int = 16,
    max_parameters: int = 16,
    max_window: int = 64,
    max_lookback_sessions: int = 256,
    declared_fields: Collection[str] | None = None,
    declared_lookback_sessions: int | None = None,
) -> ExpressionAudit:
    """Audit one formula, returning a typed immutable AST and exact requirements.

    Every numeric literal occurrence, including windows, counts as one free
    parameter. Repeated literals are counted repeatedly. Arithmetic broadcasts
    constants, but time-series and rank operators require a field-dependent
    first argument. Declared fields must match exactly; a declared lookback may
    be conservative, but cannot exceed the configured lookback budget.
    """
    if type(expression) is not str or not expression.strip() or len(expression) > _MAX_LENGTH:
        raise ValueError("expression must be nonempty text of at most 4096 characters")
    _integer_limit(max_nodes, "max_nodes", 1, 4096)
    _integer_limit(max_depth, "max_depth", 1, 64)
    _integer_limit(max_parameters, "max_parameters", 0, 4096)
    _integer_limit(max_window, "max_window", 1, 1_000_000)
    _integer_limit(max_lookback_sessions, "max_lookback_sessions", 1, 1_000_000)
    allowed = _fields(allowed_fields, "allowed_fields")
    declared = None if declared_fields is None else _fields(declared_fields, "declared_fields")
    if declared_lookback_sessions is not None:
        _integer_limit(declared_lookback_sessions, "declared_lookback_sessions", 1, max_lookback_sessions)
    root = _Parser(expression, allowed, max_nodes, max_window).parse()
    # Explicit postorder traversal also handles long flat infix expressions
    # without relying on Python's recursion limit.
    pending = [(root, 1, False)]
    computed: dict[int, tuple[frozenset[str], int, str]] = {}
    node_count = depth = parameters = 0
    while pending:
        node, level, visited = pending.pop()
        if not visited:
            node_count += 1
            depth = max(depth, level)
            if level > max_depth:
                raise ValueError("expression exceeds max_depth")
            if node.kind == "constant":
                parameters += 1
                if parameters > max_parameters:
                    raise ValueError("expression exceeds max_parameters")
            pending.append((node, level, True))
            pending.extend((child, level + 1, False) for child in reversed(node.args))
            continue
        if node.kind == "field":
            computed[id(node)] = (frozenset({str(node.value)}), 1, str(node.value))
            continue
        if node.kind == "constant":
            computed[id(node)] = (frozenset(), 1, _number(float(node.value)))
            continue
        children = [computed[id(child)] for child in node.args]
        references = frozenset().union(*(child[0] for child in children))
        lookback = max(child[1] for child in children)
        operator = str(node.value)
        if operator in _SERIES_OPERATORS and not children[0][0]:
            raise ValueError(f"{operator} requires a field-dependent series argument")
        if operator in _WINDOW_OPERATORS:
            window = int(float(node.args[1].value))
            lookback = children[0][1] + window - (0 if operator in {"delay", "return"} else 1)
        if lookback > max_lookback_sessions:
            raise ValueError("expression exceeds max_lookback_sessions")
        computed[id(node)] = (references, lookback, f"{operator}({','.join(child[2] for child in children)})")
    fields, lookback, normalized = computed[id(root)]
    if not fields:
        raise ValueError("factor expression must reference at least one allowed field")
    if declared is not None and declared != fields:
        raise ValueError("declared_fields must match expression field references exactly")
    if declared_lookback_sessions is not None and declared_lookback_sessions < lookback:
        raise ValueError("declared_lookback_sessions is smaller than the inferred lookback")
    return ExpressionAudit(root, tuple(sorted(fields)), node_count, depth, parameters, lookback, normalized)
