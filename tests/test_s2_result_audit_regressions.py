"""S2-003 development regressions; only literal synthetic data and local Git.

The audit supplied the mutation semantics. These fixtures are freshly built;
they are not independent-audit output, provider data, or approval evidence.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from bbag.provenance import json_bytes
from bbag.s1_contracts import MarketPanel
from test_provenance_boundaries import guarded_script
from test_s2_handoff import PACKAGER, control, payload, script, write
from test_s2_lock_commit import git_lock, run_cli, s2_source


CHECKER = script("check_stage2_results")
AUDIT_FAULTS = ("universe_session", "universe_signal_at", "quality_absent", "projection_digest")
ROW_FAULTS = ("quality_session", "quality_sid", "quality_duplicate", "quality_omitted",
              "quality_order", "quality_present", "quality_available", "quality_missing",
              "quality_stale", "quality_zero_volume", "quality_action", "quality_summary",
              "quality_consistent_but_wrong", "universe_omitted", "universe_duplicate", "universe_order")


def mutate_and_rehash(evidence: Path, fault: str) -> tuple[bytes, bytes]:
    """Rehash the envelope correctly so it cannot mask the semantic fault."""
    directory = evidence / "runs/synthetic"
    before = (directory / "result.json").read_bytes()
    result = payload(directory / "result.json")
    row = result["quality"]["rows"][0]
    if fault == "universe_session":
        result["universe"]["rows"][0]["session"] = "1999-01-01"
    elif fault == "universe_signal_at":
        result["universe"]["rows"][0]["signal_at"] = "1999-01-01T00:00:00Z"
    elif fault == "quality_absent":
        row.update(present=False, available=False, missing_fields=["open", "high", "low", "close", "volume"])
    elif fault == "projection_digest":
        result["projected_s1_panel_sha256"] = "0" * 64
    elif fault == "quality_session":
        row["session"] = "1999-01-01"
    elif fault == "quality_sid":
        row["sid"] = "SYN_UNREQUESTED"
    elif fault == "quality_duplicate":
        result["quality"]["rows"][1] = deepcopy(row)
    elif fault == "quality_omitted":
        result["quality"]["rows"].pop()
    elif fault == "quality_order":
        result["quality"]["rows"][:2] = result["quality"]["rows"][:2][::-1]
    elif fault == "quality_present":
        row["present"] = False
    elif fault == "quality_available":
        row["available"] = False
    elif fault == "quality_missing":
        row["missing_fields"] = ["close"]
    elif fault == "quality_stale":
        row["stale"] = True
    elif fault == "quality_zero_volume":
        row["zero_volume"] = True
    elif fault == "quality_action":
        row["known_action_on_session"] = True
    elif fault == "quality_summary":
        result["quality"]["available_bars"] = 7
    elif fault == "quality_consistent_but_wrong":
        row.update(present=False, available=False, missing_fields=["open", "high", "low", "close", "volume"])
        result["quality"].update(available_bars=7, complete_bars=7, coverage=7 / 8)
    elif fault == "universe_omitted":
        result["universe"]["rows"].pop()
    elif fault == "universe_duplicate":
        result["universe"]["rows"][1] = deepcopy(result["universe"]["rows"][0])
    elif fault == "universe_order":
        result["universe"]["rows"][:2] = result["universe"]["rows"][:2][::-1]
    else:
        raise AssertionError("Unknown test mutation")
    write(directory / "result.json", result)
    record = payload(directory / "run.json")
    record["result_sha256"] = hashlib.sha256((directory / "result.json").read_bytes()).hexdigest()
    write(directory / "run.json", record)
    assert record["result_sha256"] == hashlib.sha256((directory / "result.json").read_bytes()).hexdigest()
    return before, (directory / "result.json").read_bytes()


@pytest.mark.parametrize("fault", AUDIT_FAULTS + ROW_FAULTS)
def test_reference_rejects_semantic_corruption_with_valid_outer_hash(control, fault):
    evidence, source = control
    mutate_and_rehash(evidence, fault)
    with pytest.raises(ValueError, match="universe|quality|projection|reference"):
        CHECKER.check_results(evidence / "runs/synthetic", root=source)


@pytest.mark.parametrize("fault", AUDIT_FAULTS)
def test_full_cli_rejects_audit_counterexample_after_result_hash_is_recomputed(tmp_path, s2_source, fault):
    repository, commit, evidence = s2_source
    mutate_and_rehash(evidence, fault)
    original = {p.relative_to(evidence): p.read_bytes() for p in evidence.rglob("*") if p.is_file()}
    output = tmp_path / "must-not-be-created"
    result = run_cli(repository, commit, evidence, output)
    witness = {"fault": fault, "actual_exit_code": result.returncode,
               "sha_directory_created": (output / commit).exists(),
               "source_zip_created": (output / commit / "source.zip").exists()}
    if result.returncode == 0:
        witness["incorrectly_packaged_result"] = payload(output / commit / "evidence/runs/synthetic/result.json")
    assert result.returncode == 2, (witness, result.stdout, result.stderr)
    error = json.loads(result.stderr)
    assert error["stage"] == "S2" and isinstance(error["error"], str)
    assert any(word in error["error"].lower() for word in ("universe", "quality", "projection", "reference"))
    assert not output.exists()
    assert {p.relative_to(evidence): p.read_bytes() for p in evidence.rglob("*") if p.is_file()} == original


@pytest.mark.parametrize("fault", AUDIT_FAULTS)
@pytest.mark.parametrize("existing_output", [False, True])
def test_semantic_rejection_precedes_archive_and_preserves_sentinel(tmp_path, s2_source, fault, existing_output):
    repository, commit, evidence = s2_source
    mutate_and_rehash(evidence, fault)
    output = tmp_path / "deliveries"
    if existing_output:
        (output / "historic").mkdir(parents=True)
        (output / "historic/report.txt").write_bytes(b"SYNTHETIC HISTORICAL SENTINEL; PRESERVE EXACTLY\n")
        (output / "old.zip").write_bytes(b"SYNTHETIC EXISTING ARCHIVE; NOT A REAL PACKAGE\n")
    before = {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()}
    result, marker = guarded_script(repository, "build_stage2_package.py", [
        "--commit", commit, "--evidence-dir", str(evidence), "--output-dir", str(output),
    ], tmp_path / "archive-guard")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert json.loads(result.stderr)["stage"] == "S2"
    assert not marker.exists() and not (output / commit).exists()
    assert not (output / f"BBAG_S2_GPT_Work_{commit[:12]}.zip").exists()
    assert output.exists() is existing_output
    assert {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()} == before


def literal_s1_panel() -> MarketPanel:
    """Hand-entered fixed four-day/two-asset answer, no projection function."""
    days = ("2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05")
    fields = ["open", "high", "low", "close", "volume"]
    a = ((20., 21., 19., 20., 100.), (22., 23., 21., 22., 100.),
         (11., 12., 10., 11., 100.), (12., 13., 11., 12., 100.))
    b = ((30., 31., 29., 30., 100.), (30., 31., 29., 30., 100.),
         (31., 32., 30., 31., 100.), (32., 33., 31., 32., 100.))
    return MarketPanel.model_validate_json(json.dumps({
        "data_id": "s2-synthetic-raw-v1", "source": "synthetic_fixture", "timezone": "America/New_York",
        "sessions": [{"session": d, "open_at": d + "T14:30:00Z", "close_at": d + "T21:00:00Z",
                      "signal_at": d + "T21:05:00Z"} for d in days],
        "assets": ["SYN_A", "SYN_B"], "fields": fields,
        "observations": [{"session": day, "asset": sid, "available_at": day + "T21:01:00Z",
                          "values": dict(zip(fields, values))}
                         for index, day in enumerate(days) for sid, values in (("SYN_A", a[index]), ("SYN_B", b[index]))],
    }))


def test_correct_projection_and_quality_match_separate_literal_answer(control):
    evidence, source = control
    result = payload(evidence / "runs/synthetic/result.json")
    expected_hash = hashlib.sha256(json_bytes(literal_s1_panel().model_dump(mode="json"))).hexdigest()
    assert result["projected_s1_panel_sha256"] == expected_hash
    assert [(r["session"], r["sid"], r["known_action_on_session"], r["raw_close_jump"])
            for r in result["quality"]["rows"]] == [
        ("2024-01-02", "SYN_A", False, False), ("2024-01-02", "SYN_B", False, False),
        ("2024-01-03", "SYN_A", False, False), ("2024-01-03", "SYN_B", False, False),
        ("2024-01-04", "SYN_A", True, True), ("2024-01-04", "SYN_B", False, False),
        ("2024-01-05", "SYN_A", False, False), ("2024-01-05", "SYN_B", True, False)]
    assert CHECKER.check_results(evidence / "runs/synthetic", root=source)["reference_passed"] is True


def test_equivalent_signal_timezone_offset_preserves_the_fixed_instant(control):
    evidence, source = control
    directory = evidence / "runs/synthetic"
    result = payload(directory / "result.json")
    result["universe"]["rows"][0]["signal_at"] = "2024-01-02T16:05:00-05:00"
    write(directory / "result.json", result)
    record = payload(directory / "run.json")
    record["result_sha256"] = hashlib.sha256((directory / "result.json").read_bytes()).hexdigest()
    write(directory / "run.json", record)
    assert CHECKER.check_results(directory, root=source)["reference_passed"] is True
    assert PACKAGER.validate_evidence(evidence, record["code_commit"], source)[0]["developer_checks_passed"] is True


def test_valid_fixed_result_and_committed_lock_package_from_unrelated_cwd(tmp_path, s2_source):
    repository, commit, evidence = s2_source
    launcher = tmp_path / "unrelated-launcher"
    launcher.mkdir()
    (launcher / "requirements.lock").write_bytes(b"DECOY; NEVER THE SOURCE LOCK")
    output = tmp_path / "SYNTHETIC_VALID_PACKAGE"
    result = run_cli(repository, commit, evidence, output, cwd=launcher)
    assert result.returncode == 0, (result.stdout, result.stderr)
    committed = git_lock(repository, commit)
    with zipfile.ZipFile(output / commit / "source.zip") as archive:
        assert archive.read("requirements.lock") == committed == (repository / "requirements.lock").read_bytes()
    packaged = output / commit / "evidence"
    assert payload(packaged / "verification.json")["dependency_lock_sha256"] == hashlib.sha256(committed).hexdigest()
    assert (packaged / "runs/synthetic/result.json").read_bytes() == (evidence / "runs/synthetic/result.json").read_bytes()
    assert PACKAGER.reference_check(repository, packaged)["reference_passed"] is True
