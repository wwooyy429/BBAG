"""Guarded data-plane explanations and static holding action arithmetic.

Cloud identifiers, event times and entitlements remain in Cloud. These objects
are not verdict-only transport metadata and do not authenticate provider truth.
The action trace has no trades, prices, portfolio returns or economic verdict.
"""
from __future__ import annotations

import math
from typing import Literal

from pydantic import AwareDatetime, Field

from .contracts import NonNegativeFloat, ProtocolDate, StrictContract, Text
from .s1_contracts import utc
from .s2_access import AccessGuard, DataRequest
from .s2_data import (
    QualityReport, StandardDataBundle, action_entitlement,
    validate_snapshot_selection,
)


IdentityReason = Literal[
    "ELIGIBLE", "IDENTITY_NOT_KNOWN", "NOT_YET_LISTED", "DELISTED",
    "NO_KNOWN_MEMBERSHIP", "NOT_MEMBER", "NO_KNOWN_TICKER",
]


class IdentityAuditRow(StrictContract):
    session: ProtocolDate
    signal_at: AwareDatetime
    sid: Text
    eligible: bool
    reason: IdentityReason
    ticker: Text | None = None
    membership_effective_on: ProtocolDate | None = None
    membership_known_at: AwareDatetime | None = None


class IdentityAudit(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    data_id: Text
    as_of: AwareDatetime
    selection: Literal["membership_before_optional_liquidity"] = "membership_before_optional_liquidity"
    rows: list[IdentityAuditRow] = Field(max_length=8192)


def identity_audit(bundle: StandardDataBundle, request: DataRequest, guard: AccessGuard) -> IdentityAudit:
    """Explain historical eligibility using only each signal's known metadata."""
    bundle, request = validate_snapshot_selection(bundle, request, guard)
    identities = {item.sid: item for item in bundle.identities}
    rows = []
    for session in bundle.sessions:
        if not request.start <= session.session <= request.end or utc(session.signal_at) > utc(request.as_of):
            continue
        for sid in sorted(request.assets):
            identity = identities[sid]
            ticker, effective, known_at = None, None, None
            if utc(identity.known_at) > utc(session.signal_at):
                reason = "IDENTITY_NOT_KNOWN"
            elif identity.listed_from > session.session:
                reason = "NOT_YET_LISTED"
            elif (identity.delisted_on is not None and identity.delisted_on <= session.session
                    and utc(identity.delisting_known_at) <= utc(session.signal_at)):
                reason = "DELISTED"
            else:
                events = [item for item in bundle.membership if item.sid == sid
                          and item.effective_on <= session.session
                          and utc(item.known_at) <= utc(session.signal_at)]
                latest = max(events, key=lambda item: item.effective_on, default=None)
                if latest is None:
                    reason = "NO_KNOWN_MEMBERSHIP"
                else:
                    effective, known_at = latest.effective_on, latest.known_at
                    if not latest.member:
                        reason = "NOT_MEMBER"
                    else:
                        mappings = [item for item in bundle.tickers if item.sid == sid
                                    and item.valid_from <= session.session
                                    and utc(item.known_at) <= utc(session.signal_at)
                                    and (item.valid_to is None or session.session <= item.valid_to
                                         or utc(item.valid_to_known_at) > utc(session.signal_at))]
                        mapping = max(mappings, key=lambda item: item.valid_from, default=None)
                        ticker = mapping.ticker if mapping else None
                        reason = "ELIGIBLE" if ticker is not None else "NO_KNOWN_TICKER"
            rows.append(IdentityAuditRow(session=session.session, signal_at=session.signal_at,
                sid=sid, eligible=reason == "ELIGIBLE", reason=reason, ticker=ticker,
                membership_effective_on=effective, membership_known_at=known_at))
    return IdentityAudit(data_id=bundle.data_id, as_of=request.as_of, rows=rows)


def validate_quality_consistency(report: QualityReport) -> QualityReport:
    """Check generic report self-consistency, without claiming provider truth.

    This does not replace fixed-fixture reference comparisons or independently
    reconcile a report with its source bundle. It adds no new acceptance gate.
    """
    if not isinstance(report, QualityReport):
        raise ValueError("a typed quality report is required")
    checked = QualityReport.model_validate(report.model_dump(mode="python"))
    keys = [(row.session, row.sid) for row in checked.rows]
    if len(keys) != len(set(keys)):
        raise ValueError("quality rows must have unique session/SID keys")
    for row in checked.rows:
        if len(row.missing_fields) != len(set(row.missing_fields)):
            raise ValueError("quality missing fields must be unique")
        if row.available and not row.present:
            raise ValueError("available quality data must be present")
        if not row.available and (not row.missing_fields or row.zero_volume or row.raw_close_jump):
            raise ValueError("unavailable quality data cannot claim complete or observed fields")
        if row.raw_close_jump and "close" in row.missing_fields:
            raise ValueError("a raw close jump requires an observed close")
        if row.zero_volume and "volume" in row.missing_fields:
            raise ValueError("zero volume requires an observed volume")
    available = sum(row.available for row in checked.rows)
    complete = sum(row.available and not row.missing_fields for row in checked.rows)
    expected = len(checked.rows)
    if (checked.expected_bars != expected or checked.available_bars != available
            or checked.complete_bars != complete
            or checked.coverage != (complete / expected if expected else 0.0)):
        raise ValueError("quality summaries must equal their actual row verdicts")
    return checked


class ActionTraceRow(StrictContract):
    effective_on: ProtocolDate
    known_at: AwareDatetime
    kind: Literal["SPLIT", "DIVIDEND"]
    shares_before: NonNegativeFloat
    shares_after: NonNegativeFloat
    cash_distribution: NonNegativeFloat
    cumulative_cash: NonNegativeFloat


class ActionChainResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    purpose: Literal["ENGINEERING_TEST"] = "ENGINEERING_TEST"
    status: Literal["DRAFT"] = "DRAFT"
    data_id: Text
    sid: Text
    start: ProtocolDate
    end: ProtocolDate
    as_of: AwareDatetime
    shares_before: NonNegativeFloat
    shares_after: NonNegativeFloat
    cash_distribution: NonNegativeFloat
    semantics: Literal["static_holding_entitlements_only"] = "static_holding_entitlements_only"
    same_day_order: Literal["reject_without_intraday_sequence"] = "reject_without_intraday_sequence"
    steps: list[ActionTraceRow] = Field(max_length=1024)


def action_entitlement_chain(bundle: StandardDataBundle, request: DataRequest, guard: AccessGuard,
                             *, sid: str, shares_before: float) -> ActionChainResult:
    """Trace one unchanged holding over selected dates, using known events.

    The starting shares refer to the start of the selected date interval.
    Unobserved events are not inferred. Same-day split/dividend ordering needs
    information absent from this daily schema, so the ambiguous case rejects.
    """
    bundle, request = validate_snapshot_selection(bundle, request, guard)
    if type(sid) is not str or sid not in request.assets:
        raise ValueError("action-chain SID must belong to the authorized selection")
    if type(shares_before) is not float or not math.isfinite(shares_before) or shares_before < 0:
        raise ValueError("a finite nonnegative float holding is required")
    actions = sorted((item for item in bundle.corporate_actions if item.sid == sid
                      and request.start <= item.effective_on <= request.end
                      and utc(item.known_at) <= utc(request.as_of)), key=lambda item: item.effective_on)
    days = [item.effective_on for item in actions]
    if len(days) != len(set(days)):
        raise ValueError("same-day corporate actions require an explicit intraday ordering and cash basis")
    shares, distributions, rows = shares_before, [], []
    for action in actions:
        entitlement = action_entitlement(action, shares, as_of=request.as_of, timezone=bundle.timezone)
        distributions.append(entitlement.cash_distribution)
        try:
            cumulative_cash = math.fsum(distributions)
        except OverflowError as exc:
            raise ValueError("corporate-action cash accumulation overflow") from exc
        if not math.isfinite(cumulative_cash):
            raise ValueError("corporate-action cash accumulation overflow")
        rows.append(ActionTraceRow(effective_on=action.effective_on, known_at=action.known_at,
            kind=action.kind, shares_before=shares, shares_after=entitlement.shares_after,
            cash_distribution=entitlement.cash_distribution, cumulative_cash=cumulative_cash))
        shares = entitlement.shares_after
    return ActionChainResult(data_id=bundle.data_id, sid=sid, start=request.start, end=request.end,
        as_of=request.as_of, shares_before=shares_before, shares_after=shares,
        cash_distribution=math.fsum(distributions), steps=rows)


S2_DIAGNOSTIC_CONTRACTS = {model.__name__: model for model in
    (IdentityAuditRow, IdentityAudit, ActionTraceRow, ActionChainResult)}
