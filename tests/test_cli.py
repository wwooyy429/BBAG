"""Real subprocess tests of S0 JSON interfaces and artifact boundaries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

import pytest

from bbag.contracts import CONTRACT_MODELS, ExperimentRecord, canonical_protocol_bytes, load_protocol
from bbag.provenance import git_provenance


def test_validate_example_never_claims_research_readiness(
    example_path: Path, run_cli: Any, cli_json: Any
) -> None:
    result = run_cli("validate", "--config", example_path)
    assert result.returncode == 0, result.stderr
    output = cli_json(result)
    assert output["valid"] is True
    assert output["stage"] == "S0"
    assert output["research_ready"] is False
    assert output["pending_decisions"]
    assert output["protocol_sha256"] == hashlib.sha256(
        canonical_protocol_bytes(load_protocol(example_path))
    ).hexdigest()


@pytest.mark.parametrize("kind", ["missing", "malformed", "unknown", "wrong_type", "nan"])
def test_bad_config_returns_structured_error(
    tmp_path: Path, protocol_payload: dict[str, Any], write_json: Any,
    run_cli: Any, cli_json: Any, kind: str
) -> None:
    path = tmp_path / "absent.json"
    if kind == "malformed":
        path.write_text("{", encoding="utf-8")
    elif kind != "missing":
        if kind == "unknown":
            protocol_payload["not_a_contract_field"] = True
        elif kind == "wrong_type":
            protocol_payload["factor"]["lookback_sessions"] = True
        else:
            protocol_payload["execution"]["commission_bps"] = float("nan")
        path = write_json(protocol_payload)
    result = run_cli("validate", "--config", path)
    assert result.returncode == 2
    output = cli_json(result)
    assert output["valid"] is False
    assert output["command"] == "validate"
    assert output.get("errors") or output.get("error")


def test_init_experiment_writes_canonical_snapshot_and_honest_provenance(
    tmp_path: Path, example_path: Path, run_cli: Any, cli_json: Any
) -> None:
    directory = tmp_path / "runs"
    result = run_cli("init-experiment", "--config", example_path,
                     "--output-dir", directory, "--run-id", "s0-contract-test")
    assert result.returncode == 0, result.stderr
    output = cli_json(result)
    target = directory / "s0-contract-test"
    assert Path(output["protocol_path"]).resolve() == (target / "protocol.json").resolve()
    assert Path(output["experiment_path"]).resolve() == (target / "experiment.json").resolve()
    protocol_bytes = (target / "protocol.json").read_bytes()
    record_json = json.loads((target / "experiment.json").read_text(encoding="utf-8"))
    assert record_json == output["record"]
    record = ExperimentRecord.model_validate_json(json.dumps(record_json))
    assert protocol_bytes == canonical_protocol_bytes(load_protocol(example_path))
    assert record.protocol_sha256 == hashlib.sha256(protocol_bytes).hexdigest()
    assert record.run_id == "s0-contract-test"
    assert record.stage == "S0"
    assert record.purpose == "ENGINEERING_TEST"
    assert record.review_status == "PENDING"
    assert record.platform_status == "NOT_RUN"
    assert record.economic_status == "NOT_EVALUATED"
    assert type(record.working_tree_dirty) is bool
    assert record.created_at.utcoffset() is not None
    assert record.code_commit is None or re.fullmatch(r"[0-9a-f]{40}", record.code_commit)
    if record.code_commit is None:
        assert record.working_tree_dirty is True


def test_init_refuses_to_overwrite_existing_run(
    tmp_path: Path, example_path: Path, run_cli: Any, cli_json: Any
) -> None:
    arguments = ("init-experiment", "--config", example_path,
                 "--output-dir", tmp_path / "runs", "--run-id", "immutable-run")
    first = run_cli(*arguments)
    assert first.returncode == 0, first.stderr
    paths = [Path(cli_json(first)[key]) for key in ("experiment_path", "protocol_path")]
    previous = {path: path.read_bytes() for path in paths}
    second = run_cli(*arguments)
    assert second.returncode == 2
    assert cli_json(second)["valid"] is False
    assert {path: path.read_bytes() for path in paths} == previous


@pytest.mark.parametrize("run_id", ["..", "../escape", r"..\escape", "/absolute", "C:/escape", "nested/run", "."])
def test_init_rejects_path_escape_without_writing_manifests(
    tmp_path: Path, example_path: Path, run_cli: Any, cli_json: Any, run_id: str
) -> None:
    result = run_cli("init-experiment", "--config", example_path,
                     "--output-dir", tmp_path / "runs", "--run-id", run_id)
    assert result.returncode == 2
    assert cli_json(result)["valid"] is False
    assert list(tmp_path.rglob("experiment.json")) == []
    assert list(tmp_path.rglob("protocol.json")) == []


def test_invalid_protocol_cannot_create_experiment(
    tmp_path: Path, protocol_payload: dict[str, Any],
    write_json: Any, run_cli: Any, cli_json: Any
) -> None:
    protocol_payload["stage"] = "S1"
    path = write_json(protocol_payload)
    result = run_cli("init-experiment", "--config", path,
                     "--output-dir", tmp_path / "runs", "--run-id", "invalid")
    assert result.returncode == 2
    assert cli_json(result)["valid"] is False
    assert not (tmp_path / "runs" / "invalid").exists()


def test_exported_schemas_match_all_registered_contracts(tmp_path: Path, run_cli: Any, cli_json: Any) -> None:
    directory = tmp_path / "schemas"
    result = run_cli("schemas", "--output-dir", directory)
    assert result.returncode == 0, result.stderr
    assert cli_json(result)["schemas"] == len(CONTRACT_MODELS)
    expected = {f"{name}.schema.json" for name in CONTRACT_MODELS}
    assert {path.name for path in directory.glob("*.schema.json")} == expected
    for name, model in CONTRACT_MODELS.items():
        schema = json.loads((directory / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert schema == model.model_json_schema()
        assert schema["additionalProperties"] is False


def test_schema_check_detects_drift_and_missing_files(tmp_path: Path, run_cli: Any, cli_json: Any) -> None:
    directory = tmp_path / "schemas"
    assert run_cli("schemas", "--output-dir", directory).returncode == 0
    checked = run_cli("schemas", "--output-dir", directory, "--check")
    assert checked.returncode == 0, checked.stderr
    assert cli_json(checked)["checked"] is True
    schema_file = next(directory.glob("*.schema.json"))
    schema_file.write_text("{}", encoding="utf-8")
    drifted = run_cli("schemas", "--output-dir", directory, "--check")
    assert drifted.returncode == 2
    assert cli_json(drifted)["valid"] is False
    schema_file.unlink()
    missing = run_cli("schemas", "--output-dir", directory, "--check")
    assert missing.returncode == 2
    assert cli_json(missing)["valid"] is False


def test_unavailable_git_is_explicit_unknown_and_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("simulated missing Git executable")

    monkeypatch.setattr(subprocess, "run", unavailable)
    assert git_provenance() == {"commit": None, "branch": None, "dirty": True}
