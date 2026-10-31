"""Compare saved S1 runs with hand/rational reference data; compute no factors."""
from __future__ import annotations

import argparse
from fractions import Fraction
import json
import math
from pathlib import Path
import sys

from bbag.jsonio import read_json_object
from bbag.s1_contracts import FactorResult, LabelResult


def check_results(expected_path: Path, batch_dir: Path, incremental_dir: Path, labels_dir: Path) -> dict:
    expected = read_json_object(expected_path)
    if (expected.get("purpose") != "ENGINEERING_TEST" or expected.get("status") != "DRAFT"):
        raise ValueError("Reference data must be an ENGINEERING_TEST DRAFT")
    abs_tol, rel_tol = expected["absolute_tolerance"], expected["relative_tolerance"]
    if (type(abs_tol) not in (int, float) or type(rel_tol) not in (int, float)
            or not 0 < abs_tol <= 1e-6 or not 0 < rel_tol <= 1e-6):
        raise ValueError("Reference tolerances must be finite registered small positive numbers")
    batch = FactorResult.model_validate_json(json.dumps(read_json_object(batch_dir / "result.json"), allow_nan=False))
    incremental = FactorResult.model_validate_json(json.dumps(read_json_object(incremental_dir / "result.json"), allow_nan=False))
    labels = LabelResult.model_validate_json(json.dumps(read_json_object(labels_dir / "result.json"), allow_nan=False))
    if batch.mode != "batch" or incremental.mode != "incremental":
        raise ValueError("Factor run modes do not match the reference comparison")
    if batch.rows != incremental.rows:
        raise ValueError("Batch and incremental outputs differ")
    factor_expected = expected["factor_rows"]
    label_expected = expected["label_rows"]
    if len(batch.rows) != len(factor_expected) or len(labels.rows) != len(label_expected):
        raise ValueError("Reference output row counts differ")
    for actual, reference in zip(batch.rows, factor_expected):
        if (actual.session.isoformat(), actual.asset) != (reference["session"], reference["asset"]):
            raise ValueError("Factor reference row identity/order differs")
        if actual.signal_at.isoformat() != reference["signal_at"]:
            raise ValueError("Factor signal timestamp differs from the reference schedule")
        value = reference["value"]
        if value is None:
            if actual.value is not None:
                raise ValueError("An expected missing factor is populated")
        else:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("A reference factor must be a finite number or null")
            if actual.value is None or not math.isclose(actual.value, value, abs_tol=abs_tol, rel_tol=rel_tol):
                raise ValueError(f"Factor differs from hand answer: {reference}")
    for actual, reference in zip(labels.rows, label_expected):
        if ((actual.session.isoformat(), actual.asset, actual.status)
                != (reference["session"], reference["asset"], reference["status"])):
            raise ValueError("Label identity/order/status differs from reference")
        for name in ("signal_at", "entry_at", "exit_at", "mature_at"):
            value = getattr(actual, name)
            stamp = None if value is None else value.isoformat()
            if stamp != reference[name]:
                raise ValueError(f"Label {name} differs from the independent session schedule")
        if reference["status"] == "MATURED":
            if (type(reference["numerator"]) is not int or type(reference["denominator"]) is not int
                    or reference["denominator"] <= 0):
                raise ValueError("A rational reference requires an integer numerator and positive integer denominator")
            answer = float(Fraction(reference["numerator"], reference["denominator"]))
            if actual.value is None or not math.isclose(actual.value, answer, abs_tol=abs_tol, rel_tol=rel_tol):
                raise ValueError(f"Label differs from integer rational answer: {reference}")
        elif actual.value is not None:
            raise ValueError("An unavailable label disclosed a value")
    return {"reference_passed": True, "factor_rows": len(batch.rows), "label_rows": len(labels.rows),
            "absolute_tolerance": abs_tol, "relative_tolerance": rel_tol,
            "scope": "Synthetic hand/rational engineering references; no economic evaluation"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--incremental", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = check_results(args.expected, args.batch, args.incremental, args.labels)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
