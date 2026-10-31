"""Runtime S2 scope and output-boundary checks, independent of transport.

Policy JSON is not authority. Real cloud dispatch additionally needs an exact
policy digest in a registry supplied by trusted application code. No such
registry is populated by loading the synthetic example configuration.
"""
from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator

from .contracts import ProtocolDate, StrictContract, Text, TimeRange
from .s1_contracts import utc

Provider = Literal["SYNTHETIC", "QUANTCONNECT_CLOUD"]
MarketField = Literal["open", "high", "low", "close", "volume"]
DataClassification = Literal["DEVELOPMENT", "SEALED_FINAL", "SEALED_DERIVATIVE", "UNKNOWN"]
DataOperation = Literal["query", "batch", "cache_read", "cache_write", "quality", "export", "cloud_dispatch"]
OutputMode = Literal["LOCAL_DATA", "CONTROL_ONLY", "IN_CLOUD_DATA"]
STANDARD_FIELDS: tuple[MarketField, ...] = ("open", "high", "low", "close", "volume")


def _unique(values: list[str], name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must not contain duplicates")


class DataRequest(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    provider: Provider
    start: ProtocolDate
    end: ProtocolDate
    assets: list[Text] = Field(min_length=1, max_length=32)
    fields: list[MarketField] = Field(min_length=1, max_length=5)
    as_of: AwareDatetime
    purpose: Literal["ENGINEERING_TEST", "DEVELOPMENT"] = "ENGINEERING_TEST"
    classification: DataClassification = "UNKNOWN"
    derived_from: list[DataClassification] = Field(default_factory=list, max_length=32)
    output_mode: OutputMode = "LOCAL_DATA"

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.start > self.end:
            raise ValueError("request start must not follow end")
        _unique(self.assets, "request assets")
        _unique(self.fields, "request fields")
        return self


class AccessPolicy(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    policy_id: Text
    authorization: Literal["ENGINEERING_TEST", "OWNER_AUTHORIZED"] = "ENGINEERING_TEST"
    provider: Provider
    timezone: Text = "America/New_York"
    development: TimeRange
    sealed: list[TimeRange] = Field(default_factory=list, max_length=32)
    allowed_assets: list[Text] = Field(min_length=1, max_length=32)
    allowed_fields: list[MarketField] = Field(min_length=1, max_length=5)
    allowed_purposes: list[Literal["ENGINEERING_TEST", "DEVELOPMENT"]] = Field(default_factory=lambda: ["ENGINEERING_TEST"], min_length=1, max_length=2)
    max_query_days: Annotated[int, Field(strict=True, ge=1, le=3660)] = 366
    approval_reference: Text | None = None
    approved_by: Text | None = None
    approved_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("policy timezone must be an available IANA zone") from exc
        _unique(self.allowed_assets, "policy assets")
        _unique(self.allowed_fields, "policy fields")
        _unique(self.allowed_purposes, "policy purposes")
        ordered = sorted(self.sealed, key=lambda item: (item.start, item.end))
        for left, right in zip(ordered, ordered[1:]):
            if left.end >= right.start:
                raise ValueError("sealed intervals must be disjoint")
        if any(not (item.end < self.development.start or item.start > self.development.end) for item in ordered):
            raise ValueError("development and sealed intervals must be disjoint")
        approval = (self.approval_reference, self.approved_by, self.approved_at)
        if self.authorization == "ENGINEERING_TEST":
            if self.provider != "SYNTHETIC" or self.allowed_purposes != ["ENGINEERING_TEST"] or any(value is not None for value in approval):
                raise ValueError("an engineering policy grants only synthetic tests and carries no owner approval")
        elif any(value is None for value in approval):
            raise ValueError("OWNER_AUTHORIZED policy requires reference, owner and aware approval time")
        return self


def canonical_policy_sha256(policy: AccessPolicy) -> str:
    if not isinstance(policy, AccessPolicy):
        raise ValueError("policy must be an AccessPolicy")
    checked = AccessPolicy.model_validate(policy.model_dump(mode="python"))
    raw = json.dumps(checked.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def derive_classification(sources: Sequence[DataClassification]) -> DataClassification:
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence) or not sources:
        return "UNKNOWN"
    if any(type(value) is not str or value not in {"DEVELOPMENT", "SEALED_FINAL", "SEALED_DERIVATIVE", "UNKNOWN"} for value in sources):
        return "UNKNOWN"
    if "UNKNOWN" in sources:
        return "UNKNOWN"
    if any(value in {"SEALED_FINAL", "SEALED_DERIVATIVE"} for value in sources):
        return "SEALED_DERIVATIVE"
    return "DEVELOPMENT"


class AccessGuard:
    """A mandatory pre-I/O gate. This class never invokes a transport itself."""

    def __init__(self, policy: AccessPolicy, *, trusted_policy_sha256s: frozenset[str] = frozenset(),
                 execution_location: Literal["LOCAL", "QUANTCONNECT_CLOUD"] = "LOCAL") -> None:
        if not isinstance(policy, AccessPolicy):
            raise ValueError("policy must be an AccessPolicy")
        if type(trusted_policy_sha256s) is not frozenset or any(type(value) is not str or len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in trusted_policy_sha256s):
            raise ValueError("trusted policy digests must be a frozenset of exact lowercase SHA-256 values")
        if type(execution_location) is not str or execution_location not in {"LOCAL", "QUANTCONNECT_CLOUD"}:
            raise ValueError("unknown execution location")
        self._policy = AccessPolicy.model_validate(policy.model_dump(mode="python"))
        self._trusted = trusted_policy_sha256s
        self._location = execution_location

    @property
    def policy(self) -> AccessPolicy:
        return AccessPolicy.model_validate(self._policy.model_dump(mode="python"))

    @property
    def policy_sha256(self) -> str:
        return canonical_policy_sha256(self._policy)

    @property
    def execution_location(self) -> str:
        return self._location

    def authorize(self, request: DataRequest, operation: DataOperation = "query") -> DataRequest:
        if not isinstance(request, DataRequest):
            raise ValueError("request must be a DataRequest")
        if type(operation) is not str or operation not in {"query", "batch", "cache_read", "cache_write", "quality", "export", "cloud_dispatch"}:
            raise ValueError("unknown data operation")
        checked = DataRequest.model_validate(request.model_dump(mode="python"))
        policy = self.policy
        if checked.provider != policy.provider or checked.purpose not in policy.allowed_purposes:
            raise ValueError("request provider/purpose is outside the policy")
        if (checked.classification != "DEVELOPMENT"
                or checked.derived_from and derive_classification(checked.derived_from) != "DEVELOPMENT"):
            raise ValueError("sealed, derivative or unknown-classification material is not development input")
        if checked.start < policy.development.start or checked.end > policy.development.end:
            raise ValueError("request is outside the development interval")
        if any(checked.start <= item.end and checked.end >= item.start for item in policy.sealed):
            raise ValueError("request intersects a sealed interval")
        if (checked.end - checked.start).days + 1 > policy.max_query_days:
            raise ValueError("request exceeds the bounded engineering date workload")
        if checked.end > utc(checked.as_of).astimezone(ZoneInfo(policy.timezone)).date():
            raise ValueError("request includes a date later than as_of")
        if not set(checked.assets).issubset(policy.allowed_assets) or not set(checked.fields).issubset(policy.allowed_fields):
            raise ValueError("request assets or fields are outside the policy")
        if policy.authorization == "OWNER_AUTHORIZED" and self.policy_sha256 not in self._trusted:
            raise ValueError("owner-authorized configuration is not a trusted runtime grant")
        if checked.output_mode == "CONTROL_ONLY" and operation not in {"cloud_dispatch", "cache_read", "cache_write"}:
            raise ValueError("the control channel cannot return data or quality series")
        if checked.provider == "QUANTCONNECT_CLOUD":
            if policy.authorization != "OWNER_AUTHORIZED":
                raise ValueError("real cloud access requires owner authorization")
            if operation == "export" or checked.output_mode == "LOCAL_DATA":
                raise ValueError("licensed QuantConnect cloud data cannot be imported/exported locally")
            if checked.output_mode != "CONTROL_ONLY" and self._location != "QUANTCONNECT_CLOUD":
                raise ValueError("cloud data processing must remain inside QuantConnect cloud")
        elif checked.output_mode == "IN_CLOUD_DATA":
            raise ValueError("synthetic local requests must not claim licensed cloud execution")
        return checked

    def authorize_batch(self, requests: list[DataRequest], operation: DataOperation = "batch") -> list[DataRequest]:
        if type(requests) is not list or not 1 <= len(requests) <= 32:
            raise ValueError("batch must contain between 1 and 32 typed requests")
        # Validate the whole batch before the caller may perform the first I/O.
        return [self.authorize(request, operation) for request in requests]

    def authorize_artifact(self, request: DataRequest, *, classification: DataClassification,
                           derived_from: Sequence[DataClassification] = (), operation: DataOperation = "cache_read") -> DataRequest:
        if not isinstance(request, DataRequest):
            raise ValueError("request must be a DataRequest")
        if isinstance(derived_from, (str, bytes)) or not isinstance(derived_from, Sequence):
            raise ValueError("artifact ancestry must be an explicit sequence of classifications")
        payload = request.model_dump(mode="python")
        payload.update(classification=classification, derived_from=list(derived_from))
        return self.authorize(DataRequest.model_validate(payload), operation)


S2_ACCESS_CONTRACTS = {model.__name__: model for model in (DataRequest, AccessPolicy)}
