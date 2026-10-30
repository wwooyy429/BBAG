"""Deterministic S1 factors from explicit synthetic point-in-time observations.

Each output row uses its own signal timestamp. Historical operands inside an
expression use data known at that decision timestamp; later arrivals never
rewrite an already formed historical output. Labels are not accepted here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
import math
import statistics
from typing import Literal

from .contracts import FactorSpec
from .expression import ExpressionAudit, Node, audit_expression
from .s1_contracts import (
    AuditLimits,
    AuditSummary,
    FactorResult,
    FactorValue,
    MarketPanel,
    Observation,
    utc,
)


Number = float | None


def _finite(value: float) -> Number:
    return value if math.isfinite(value) else None


def _average_rank(value: float, values: Sequence[float]) -> float:
    """Average ties, normalized to [0, 1]; a singleton is neutral 0.5."""
    if len(values) == 1:
        return 0.5
    lower = sum(item < value for item in values)
    tied = sum(item == value for item in values)
    return (lower + (tied + 1) / 2 - 1) / (len(values) - 1)


class _Evaluator:
    """A decision-scoped evaluator; its memo never crosses signal cutoffs."""

    def __init__(
        self,
        sessions: Sequence[date],
        observations: Mapping[tuple[date, str], Observation],
        universe: tuple[str, ...],
        cutoff: datetime,
    ) -> None:
        self.sessions = sessions
        self.observations = observations
        self.universe = universe
        self.cutoff = utc(cutoff)
        self.memo: dict[tuple[Node, int, str], Number] = {}

    def value(self, node: Node, index: int, asset: str) -> Number:
        if index < 0 or index >= len(self.sessions):
            return None
        key = (node, index, asset)
        if key not in self.memo:
            self.memo[key] = self._value(node, index, asset)
        return self.memo[key]

    def _value(self, node: Node, index: int, asset: str) -> Number:
        if node.kind == "constant":
            return _finite(float(node.value))
        if node.kind == "field":
            observation = self.observations.get((self.sessions[index], asset))
            if observation is None or utc(observation.available_at) > self.cutoff:
                return None
            value = observation.values.get(str(node.value))
            return None if value is None else _finite(value)
        if node.kind != "call":
            raise ValueError("Unsupported audited expression node")
        operation = str(node.value)
        child = node.args[0]
        if operation == "delay":
            return self.value(child, index - int(node.args[1].value), asset)
        if operation == "cs_rank":
            own = self.value(child, index, asset)
            if own is None:
                return None
            available = [self.value(child, index, member) for member in self.universe]
            return _average_rank(own, [value for value in available if value is not None])
        if operation in {"rolling_mean", "rolling_std", "ts_rank"}:
            window = int(node.args[1].value)
            if index + 1 < window:
                return None
            values = [self.value(child, position, asset) for position in range(index - window + 1, index + 1)]
            if any(value is None for value in values):
                return None
            complete = [value for value in values if value is not None]
            try:
                if operation == "rolling_mean":
                    # statistics uses exact intermediate sums for finite floats;
                    # a finite constant large-number window remains well-defined.
                    result = float(statistics.mean(complete))
                elif operation == "rolling_std":
                    result = statistics.pstdev(complete)
                else:
                    result = _average_rank(complete[-1], complete)
                return _finite(result)
            except (ArithmeticError, ValueError):
                return None
        left = self.value(child, index, asset)
        if left is None:
            return None
        if operation == "neg":
            return _finite(-left)
        if operation == "return":
            right = self.value(child, index - int(node.args[1].value), asset)
        else:
            right = self.value(node.args[1], index, asset)
        if right is None:
            return None
        try:
            if operation == "add":
                result = left + right
            elif operation == "sub":
                result = left - right
            elif operation == "mul":
                result = left * right
            elif operation in {"div", "return"}:
                if right == 0:
                    return None
                result = left / right
                if operation == "return":
                    result -= 1
            else:
                raise ValueError(f"Unsupported audited operator: {operation}")
            return _finite(result)
        except ArithmeticError:
            return None


def _checked_universe(panel: MarketPanel, universe: list[str]) -> tuple[str, ...]:
    if (type(universe) is not list or not universe
            or any(type(asset) is not str or not asset for asset in universe)
            or len(universe) != len(set(universe))):
        raise ValueError("Factor universe must be a nonempty list of unique asset identifiers")
    if not set(universe).issubset(panel.assets):
        raise ValueError("Factor universe contains assets absent from the supplied panel")
    return tuple(sorted(universe))


def _summary(audit: ExpressionAudit) -> AuditSummary:
    return AuditSummary(
        normalized_expression=audit.normalized_expression,
        fields=list(audit.fields),
        lookback_sessions=audit.lookback_sessions,
        node_count=audit.node_count,
        max_depth=audit.max_depth,
        parameter_count=audit.parameter_count,
    )


def compute_factors(
    panel: MarketPanel,
    factor: FactorSpec,
    limits: AuditLimits,
    universe: list[str],
    as_of: datetime,
    *,
    mode: Literal["batch", "incremental"] = "batch",
) -> FactorResult:
    """Compute signal-time factors without reading any future labels.

    Batch retains all supplied observations and checks availability on lookup.
    Incremental feeds arrived observations in timestamp order and supplies only
    the current session prefix. Both use exactly the same pure operator rules.
    """
    if not isinstance(panel, MarketPanel):
        raise ValueError("Factor panel must be a MarketPanel contract")
    if not isinstance(factor, FactorSpec):
        raise ValueError("Factor specification must be a FactorSpec contract")
    if not isinstance(limits, AuditLimits):
        raise ValueError("Factor limits must be an AuditLimits contract")
    panel = MarketPanel.model_validate(panel.model_dump(mode="python"))
    factor = FactorSpec.model_validate(factor.model_dump(mode="python"))
    limits = AuditLimits.model_validate(limits.model_dump(mode="python"))
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("Factor as_of must be a timezone-aware datetime")
    if type(mode) is not str or mode not in {"batch", "incremental"}:
        raise ValueError("Factor mode must be batch or incremental")
    assets = _checked_universe(panel, universe)
    audit = audit_expression(
        factor.formula,
        panel.fields,
        max_nodes=limits.max_nodes,
        max_depth=limits.max_depth,
        max_parameters=limits.max_parameters,
        max_window=limits.max_window,
        max_lookback_sessions=limits.max_lookback_sessions,
        declared_fields=factor.required_fields,
        declared_lookback_sessions=factor.lookback_sessions,
    )
    formed_sessions = sum(utc(session.signal_at) <= utc(as_of) for session in panel.sessions)
    work_units = audit.node_count * min(audit.lookback_sessions, len(panel.sessions)) * len(assets) * formed_sessions
    if work_units > limits.max_work_units:
        raise ValueError("Factor computation exceeds the predeclared engineering work-unit limit")
    sessions = [session.session for session in panel.sessions]
    all_observations = {(observation.session, observation.asset): observation for observation in panel.observations}
    arrivals = sorted(panel.observations, key=lambda item: (utc(item.available_at), item.session, item.asset))
    arrived: dict[tuple[date, str], Observation] = {}
    cursor = 0
    rows: list[FactorValue] = []
    for index, session in enumerate(panel.sessions):
        if utc(session.signal_at) > utc(as_of):
            break
        if mode == "incremental":
            while cursor < len(arrivals) and utc(arrivals[cursor].available_at) <= utc(session.signal_at):
                observation = arrivals[cursor]
                arrived[(observation.session, observation.asset)] = observation
                cursor += 1
            visible = {key: value for key, value in arrived.items() if key[0] <= session.session}
            evaluator = _Evaluator(sessions[:index + 1], visible, assets, session.signal_at)
        else:
            evaluator = _Evaluator(sessions, all_observations, assets, session.signal_at)
        for asset in assets:
            rows.append(FactorValue(
                session=session.session, asset=asset, signal_at=session.signal_at,
                value=evaluator.value(audit.root, index, asset),
            ))
    return FactorResult(
        data_id=panel.data_id, factor_id=factor.factor_id, as_of=as_of, mode=mode,
        audit=_summary(audit), rows=rows,
    )
