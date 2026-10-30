"""S0-001 regressions; receipt fixtures never represent independent approval."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bbag.jsonio import read_json_object


@pytest.mark.parametrize(
    ("key", "earlier"),
    [
        ("decision", "CHANGES_REQUIRED"),
        ("blocking_findings", 3),
        ("audited_commit", "b" * 40),
    ],
)
def test_conflicting_receipt_keys_are_rejected_before_contract_validation(
    tmp_path: Path, approved_receipt_payload: dict[str, Any],
    run_cli: Any, cli_json: Any, key: str, earlier: Any,
) -> None:
    body = json.dumps(approved_receipt_payload)
    raw = "{" + json.dumps(key) + ":" + json.dumps(earlier) + "," + body[1:]
    path = tmp_path / "ambiguous-receipt.json"
    path.write_text(raw, encoding="utf-8")
    result = run_cli("check-review", "--receipt", path, "--commit", "a" * 40)
    assert result.returncode == 2
    assert "duplicate JSON key" in cli_json(result)["error"]
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize(
    "raw",
    [
        '{"decision":"PENDING","decision":"PENDING"}',
        '{"decision":"PENDING","d\\u0065cision":"APPROVED"}',
        '{"checks":{"passed":false,"passed":true}}',
        '{"checks":[{"passed":false,"passed":true}]}',
    ],
)
def test_duplicate_keys_are_rejected_at_every_level(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        read_json_object(path)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_shared_reader_rejects_all_nonfinite_numbers(tmp_path: Path, token: str) -> None:
    path = tmp_path / "nonfinite.json"
    path.write_text('{"nested":[{"value":' + token + "}]}", encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite JSON"):
        read_json_object(path)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_receipt_nonfinite_tokens_are_rejected_during_parsing(
    tmp_path: Path, approved_receipt_payload: dict[str, Any],
    run_cli: Any, cli_json: Any, token: str,
) -> None:
    body = json.dumps(approved_receipt_payload)
    raw = body[:-1] + ',"unrecognized":' + token + "}"
    path = tmp_path / "nonfinite-receipt.json"
    path.write_text(raw, encoding="utf-8")
    result = run_cli("check-review", "--receipt", path, "--commit", "a" * 40)
    assert result.returncode == 2
    assert "non-finite JSON" in cli_json(result)["error"]


@pytest.mark.parametrize("raw", ["[]", "null", '"object required"', "42"])
def test_shared_reader_requires_an_object(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "scalar.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="expected a JSON object"):
        read_json_object(path)


def test_shared_reader_preserves_valid_utf8_and_finite_values(tmp_path: Path) -> None:
    path = tmp_path / "valid.json"
    expected = {"说明": "合成工程夹具", "values": [0, 1.25, 1e100], "passed": True}
    path.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")
    assert read_json_object(path) == expected
