"""Unambiguous JSON input shared by S0 contracts, receipts and evidence."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, NoReturn


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"non-finite JSON number is not allowed: {value}")
    return result


def read_json_object(path: str | Path) -> dict[str, Any]:
    """Read one UTF-8 JSON object, rejecting ambiguous or non-finite values.

    Object hooks run at every nesting level, including objects inside arrays.
    This is syntax validation; callers still apply their typed contracts.
    """
    payload = json.loads(
        Path(path).read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
        parse_float=_finite_float,
    )
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload
