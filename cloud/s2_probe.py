"""Bounded in-Cloud S2 smoke check. Never print or return market observations.

This probe covers only the separately approved January 2020 SPY/AAPL scope.
Its point_in_time flag checks bar/calendar causality and the declared cutoff;
it does not establish historical revision, survivorship, or corporate-action
correctness. Licensed bars and internal security identifiers stay in Cloud.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, time, timedelta, timezone
import json
import math
from zoneinfo import ZoneInfo

from bbag.s2_access import AccessGuard, AccessPolicy, DataRequest, STANDARD_FIELDS, canonical_policy_sha256
from bbag.s2_quantconnect import (
    ALL_CHECKS, QuantConnectCheckRequest, QuantConnectCheckResponse, qc_request_sha256,
)


class CloudProbeRejected(ValueError):
    """An unbound request cannot receive invented project or source identifiers."""

    def __init__(self) -> None:
        super().__init__("INVALID_REQUEST")


class _DiscardOutput:
    """SDK diagnostics remain suppressed rather than becoming notebook data."""

    encoding = "utf-8"

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def _json_model(model, payload: dict):
    if type(payload) is not dict:
        raise ValueError("invalid input")
    return model.model_validate_json(json.dumps(payload, allow_nan=False, separators=(",", ":")))


def _response(request: QuantConnectCheckRequest, *, error: str | None = None, checks: dict | None = None) -> dict:
    return QuantConnectCheckResponse(
        project_id=request.project_id, request_sha256=qc_request_sha256(request),
        source_commit=request.source_commit, config_sha256=request.config_sha256,
        status="ERROR" if error else "COMPLETED", error_category=error or "NONE",
        check_results=checks or {},
    ).model_dump(mode="json")


def _preflight(request: QuantConnectCheckRequest, policy_payload: dict, trusted_digest: str) -> AccessPolicy:
    policy = _json_model(AccessPolicy, policy_payload)
    if (type(trusted_digest) is not str or len(trusted_digest) != 64
            or any(char not in "0123456789abcdef" for char in trusted_digest)
            or canonical_policy_sha256(policy) != trusted_digest):
        raise ValueError("access denied")
    if (policy.authorization != "OWNER_AUTHORIZED" or policy.provider != "QUANTCONNECT_CLOUD"
            or policy.timezone != "America/New_York"
            or policy.development.start != date(2020, 1, 2) or policy.development.end != date(2020, 1, 10)
            or set(policy.allowed_assets) != {"SPY", "AAPL"}
            or set(policy.allowed_fields) != set(STANDARD_FIELDS)
            or policy.allowed_purposes != ["ENGINEERING_TEST"]
            or request.data.provider != "QUANTCONNECT_CLOUD"
            or set(request.data.fields) != set(STANDARD_FIELDS)
            or set(request.checks) != set(ALL_CHECKS)):
        raise ValueError("access denied")
    guard = AccessGuard(policy, trusted_policy_sha256s=frozenset([trusted_digest]),
                        execution_location="QUANTCONNECT_CLOUD")
    guard.authorize(request.data, operation="cloud_dispatch")
    # This is the sole CONTROL_ONLY -> IN_CLOUD_DATA conversion. It creates a
    # fresh validated request under the same trusted scope, never a local bundle.
    cloud_payload = request.data.model_dump(mode="python")
    cloud_payload["output_mode"] = "IN_CLOUD_DATA"
    guard.authorize(DataRequest.model_validate(cloud_payload), operation="query")
    return policy


def _sdk():
    # Lazy imports and factory use occur only after complete runtime preflight.
    from QuantConnect import DataNormalizationMode, Market, Resolution
    from QuantConnect.Data.Market import TradeBar
    from QuantConnect.Research import QuantBook
    return QuantBook, Resolution, DataNormalizationMode, Market, TradeBar


def _local(value: datetime, zone: ZoneInfo) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("invalid provider event")
    return value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)


def _bars(history):
    # The official typed multi-Symbol overload yields DataDictionary[TradeBar].
    for packet in history:
        values = getattr(packet, "values", None)
        if values is None:
            values = getattr(packet, "Values", None)
        if values is not None:
            yield from values() if callable(values) else values
        else:
            # Python/.NET bridges can instead expose IEnumerable<KeyValuePair>.
            for pair in packet:
                value = getattr(pair, "value", None)
                yield pair.Value if value is None else value


def _run(request: QuantConnectCheckRequest, policy: AccessPolicy) -> dict:
    QuantBook, Resolution, Normalization, Market, TradeBar = _sdk()
    qb = QuantBook()
    qb.set_time_zone(policy.timezone)
    qb.settings.daily_precise_end_time = True
    first, last = request.data.start, request.data.end
    start = datetime.combine(first, time.min)
    end = datetime.combine(last + timedelta(days=1), time.min)
    # In Research, QuantBook's start date is the latest available history date.
    # Put that clock at the query's exclusive end, before subscriptions. The
    # later QuantBook end date is context only; it does not widen the query.
    context_end = end + timedelta(days=1)
    qb.set_start_date(end.year, end.month, end.day)
    qb.set_end_date(context_end.year, context_end.month, context_end.day)
    securities = [qb.add_equity(asset, Resolution.DAILY, market=Market.USA,
                    fill_forward=False, extended_market_hours=False,
                    data_normalization_mode=Normalization.RAW) for asset in request.data.assets]
    symbols = [security.symbol for security in securities]
    # These identifiers are only compared in Cloud; never hashed or returned.
    by_sid = {str(security.symbol.id): security for security in securities}
    checks = {name: True for name in ALL_CHECKS}
    checks["identity_consistency"] = len(by_sid) == len(securities)
    for security in securities:
        configs = tuple(security.subscriptions)
        checks["normalization_raw"] &= bool(configs) and all(
            config.data_normalization_mode == Normalization.RAW for config in configs)
    # QC's date-range end is exclusive. Jan 11 midnight includes Jan 10 without
    # granting data for Jan 11 or using a span/bar-count overload across bounds.
    history = qb.history[TradeBar](symbols, start, end, Resolution.DAILY,
        fill_forward=False, extended_market_hours=False, data_normalization_mode=Normalization.RAW)
    seen = set()
    previous = {}
    zone = ZoneInfo(policy.timezone)
    total = 0
    for bar in _bars(history):
        total += 1
        if total > request.max_history_rows:
            return _response(request, error="PROVIDER_ERROR")
        sid = str(bar.symbol.id)
        if sid not in by_sid:
            checks["identity_consistency"] = checks["requested_bounds"] = False
            continue
        security = by_sid[sid]
        event_start, event_end = _local(bar.time, zone), _local(bar.end_time, zone)
        session = event_end.date()
        checks["requested_bounds"] &= first <= session <= last and event_start.date() == session
        opening = _local(security.exchange.hours.get_next_market_open(
            datetime.combine(session, time.min), False), zone)
        closing = _local(security.exchange.hours.get_next_market_close(
            opening.replace(tzinfo=None), False), zone)
        checks["point_in_time"] &= (
            opening.date() == closing.date() == session and event_start <= opening < closing == event_end
            and event_end.astimezone(timezone.utc) <= request.data.as_of.astimezone(timezone.utc)
            and not bar.is_fill_forward)
        checks["point_in_time"] &= sid not in previous or previous[sid] < event_end
        previous[sid] = event_end
        prices = [float(getattr(bar, field)) for field in ("open", "high", "low", "close")]
        volume = float(bar.volume)
        op, hi, lo, cl = prices
        checks["field_availability"] &= (all(math.isfinite(x) and x > 0 for x in prices)
            and math.isfinite(volume) and volume >= 0 and lo <= min(op, cl) <= max(op, cl) <= hi)
        seen.add(sid)
    checks["data_presence"] = bool(total) and seen == set(by_sid)
    # No raw data, SID, event time, count, graph, or raw-data hash is released.
    return _response(request, checks={name: bool(checks[name]) for name in ALL_CHECKS})


def run_cloud_check(plan: dict, policy: dict, trusted_policy_sha256: str) -> dict:
    """Return only validated check metadata; reject before any provider I/O.

    An invalid unbound plan raises CloudProbeRejected with the sole closed
    category INVALID_REQUEST. The calling cell must catch that class and emit
    only this constant category, never a traceback or exception representation.
    """
    try:
        request = _json_model(QuantConnectCheckRequest, plan)
        if plan.get("source_commit") != request.source_commit:
            raise ValueError("invalid source binding")
    except Exception:
        raise CloudProbeRejected() from None
    try:
        approved_policy = _preflight(request, policy, trusted_policy_sha256)
    except Exception:
        return _response(request, error="ACCESS_DENIED")
    try:
        # The callback/tool caller receives only the returned control object,
        # including when a dependency writes unsolicited Python diagnostics.
        with redirect_stdout(_DiscardOutput()), redirect_stderr(_DiscardOutput()):
            return _run(request, approved_policy)
    except (ImportError, ModuleNotFoundError):
        return _response(request, error="CAPABILITY_UNAVAILABLE")
    except TimeoutError:
        return _response(request, error="TIMEOUT")
    except Exception:
        return _response(request, error="PROVIDER_ERROR")
