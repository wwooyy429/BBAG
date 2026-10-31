"""S1 future price labels, isolated from the causal factor service.

Only synthetic price returns are supported. These records neither model
corporate actions nor establish economic evidence or permission to trade.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math

from .s1_contracts import LabelResult, LabelRule, LabelValue, MarketPanel, Observation


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _positive_price(observation: Observation, field: str) -> float | None:
    value = observation.values.get(field)
    if value is None or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _price_return(entry: float, exit_price: float) -> float | None:
    try:
        value = exit_price / entry - 1.0
    except (OverflowError, ZeroDivisionError):
        return None
    return value if math.isfinite(value) else None


def compute_labels(
    panel: MarketPanel,
    rule: LabelRule,
    universe: list[str],
    as_of: datetime,
) -> LabelResult:
    """Compute next-open-to-open labels available at the requested cutoff.

    A signal on index i enters at i+1 and exits at i+1+horizon_sessions.
    The only supported quote field is exactly ``open``, matching both events.
    Actual quote availability can postpone maturity beyond the scheduled exit.
    Unmatured values stay None, including when future prices are already present
    in this offline synthetic input. Output order is session then asset name.
    """
    cutoff = _utc(as_of, "as_of")
    if not isinstance(panel, MarketPanel) or not isinstance(rule, LabelRule):
        raise ValueError("panel and rule must be validated S1 contracts")
    # Containers can be mutated in place after construction. Revalidate before
    # reading input; assignment validation alone does not cover those changes.
    panel = MarketPanel.model_validate(panel.model_dump(mode="python"))
    # StrictContract revalidates instances, including model_construct/model_copy
    # values that skipped validation. Never substitute another field for open.
    rule = LabelRule.model_validate(rule)
    if (type(universe) is not list or not universe
            or any(type(asset) is not str or not asset for asset in universe)
            or len(universe) != len(set(universe))):
        raise ValueError("universe must be a nonempty list of unique asset names")
    if not set(universe).issubset(panel.assets):
        raise ValueError("universe contains an unknown asset")
    if rule.price_field not in panel.fields:
        raise ValueError("label price_field is not in the data fields")
    if rule.return_type == "excess_price_return":
        if rule.benchmark_asset not in panel.assets:
            raise ValueError("label benchmark_asset is not in the data assets")
        if rule.benchmark_asset in universe:
            raise ValueError("label benchmark_asset must be outside the target universe")
    elif rule.benchmark_asset is not None:
        raise ValueError("price_return does not accept a benchmark_asset")

    observations = {(row.session, row.asset): row for row in panel.observations}
    rows: list[LabelValue] = []
    assets = sorted(universe)
    for index, session in enumerate(panel.sessions):
        signal_at = _utc(session.signal_at, "signal_at")
        if signal_at > cutoff:
            continue
        entry_index = index + 1
        exit_index = entry_index + rule.horizon_sessions
        entry = panel.sessions[entry_index] if entry_index < len(panel.sessions) else None
        exit_session = panel.sessions[exit_index] if exit_index < len(panel.sessions) else None
        entry_at = _utc(entry.open_at, "entry_at") if entry is not None else None
        exit_at = _utc(exit_session.open_at, "exit_at") if exit_session is not None else None
        for asset in assets:
            common = dict(session=session.session, asset=asset, signal_at=signal_at,
                          entry_at=entry_at, exit_at=exit_at)
            if entry is None or exit_session is None:
                rows.append(LabelValue(**common, mature_at=None, value=None,
                                       status="INSUFFICIENT_FUTURE"))
                continue
            if entry_at is None or exit_at is None or not signal_at < entry_at < exit_at:
                raise ValueError("label requires signal_at < entry_at < exit_at")
            required_assets = [asset]
            if rule.return_type == "excess_price_return":
                if rule.benchmark_asset is None:
                    raise ValueError("excess_price_return requires a benchmark_asset")
                required_assets.append(rule.benchmark_asset)
            required = [observations.get((period.session, target_asset))
                        for target_asset in required_assets for period in (entry, exit_session)]
            known_observations = [observation for observation in required if observation is not None]
            if any(observation is None for observation in required):
                earliest_assessable = max([exit_at, *(
                    _utc(observation.available_at, "available_at") for observation in known_observations
                )])
                status = "NOT_MATURED" if cutoff < earliest_assessable else "MISSING_INPUT"
                rows.append(LabelValue(**common, mature_at=None, value=None, status=status))
                continue
            available = [_utc(observation.available_at, "available_at") for observation in known_observations]
            mature_at = max(exit_at, *available)
            if cutoff < mature_at:
                rows.append(LabelValue(**common, mature_at=mature_at, value=None, status="NOT_MATURED"))
                continue
            prices = [_positive_price(observation, rule.price_field) for observation in known_observations]
            if any(price is None for price in prices):
                rows.append(LabelValue(**common, mature_at=mature_at, value=None, status="MISSING_INPUT"))
                continue
            known_prices = [price for price in prices if price is not None]
            value = _price_return(known_prices[0], known_prices[1])
            if rule.return_type == "excess_price_return":
                benchmark = _price_return(known_prices[2], known_prices[3])
                value = None if value is None or benchmark is None else value - benchmark
            if value is None or not math.isfinite(value):
                rows.append(LabelValue(**common, mature_at=mature_at, value=None, status="MISSING_INPUT"))
            else:
                rows.append(LabelValue(**common, mature_at=mature_at, value=value, status="MATURED"))
    return LabelResult(data_id=panel.data_id, as_of=cutoff, rule=rule, rows=rows)
