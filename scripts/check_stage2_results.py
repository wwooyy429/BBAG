"""Check the literal S2 synthetic reference without generating expected answers."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote

from bbag.jsonio import read_json_object
from bbag.provenance import json_bytes
from bbag.s2_contracts import S2RunManifest, S2SyntheticResult, canonical_s2_config, load_s2_config
from bbag.s2_data import DailyBar, action_entitlement
from bbag.s2_tasks import TaskResponse


def require(value: bool, message: str) -> None:
    if not value:
        raise ValueError(message)


def check_results(run: Path, root: Path | None = None) -> dict:
    root = root or Path(__file__).resolve().parents[1]
    expected = read_json_object(root / "tests/fixtures/s2_expected.json")
    raw_path = root / "tests/fixtures/s2_raw.json"
    raw_source = read_json_object(raw_path)
    config = load_s2_config(run / "config.json")
    require(config.request.provider == "SYNTHETIC", "the reference checker accepts synthetic data only")
    require(canonical_s2_config(config) == canonical_s2_config(load_s2_config(root / "configs/s2.example.json")),
            "run configuration differs from the committed synthetic example")
    record = S2RunManifest.model_validate_json(json.dumps(read_json_object(run / "run.json")))
    content = (run / "result.json").read_bytes()
    result = S2SyntheticResult.model_validate_json(json.dumps(read_json_object(run / "result.json")))
    require(record.config_sha256 == result.config_sha256 == hashlib.sha256(canonical_s2_config(config)).hexdigest(), "configuration hash mismatch")
    require(record.result_sha256 == hashlib.sha256(content).hexdigest(), "result bytes hash mismatch")
    require(config.bundle is not None and config.bundle.provenance.source_sha256 == hashlib.sha256(raw_path.read_bytes()).hexdigest(), "raw fixture source hash mismatch")
    # ISO Z and +00:00 represent the same instant; compare typed bars without
    # rewriting the separately hashed original raw bytes or any price answer.
    expected_bars = [DailyBar.model_validate_json(json.dumps(item)) for item in raw_source["bars"]]
    require(config.bundle.bars == expected_bars, "raw fields were not preserved")
    require([row.assets for row in result.universe.rows] == expected["universe_assets"], "lagged hand-calculated universe differs")
    for key in ("expected_bars", "available_bars", "complete_bars", "coverage"):
        require(getattr(result.quality, key) == expected[key], "literal quality reference differs: " + key)
    jumps = [[row.session.isoformat(), row.sid] for row in result.quality.rows if row.raw_close_jump]
    require(jumps == expected["raw_close_jumps"], "raw split jump context differs")
    for action in config.bundle.corporate_actions:
        actual = action_entitlement(action, 10.0, as_of=config.request.as_of).model_dump(mode="json")
        key = "split_entitlement_for_10_shares" if action.kind == "SPLIT" else "dividend_entitlement_for_10_shares"
        require(actual == expected[key], "independent action entitlement differs")
    require(result.task.spec.response_class == expected["local_task_class"], "fixture must not impersonate platform evidence")
    require(result.task.spec.source_commit == record.code_commit, "task source differs from run source")
    require(result.usage.attempt_count == expected["local_task_attempt_count"] and len(result.task.attempts) == 1, "fixture attempt accounting differs")
    require([a.platform_task_id for a in result.task.attempts if a.platform_task_id is not None] == expected["provider_task_ids"], "fixture invented provider IDs")
    for attempt in result.task.attempts:
        for reference in attempt.raw_metadata:
            path = run / "metadata" / reference.relative_path
            payload = path.read_bytes()
            require(len(payload) == reference.size_bytes and hashlib.sha256(payload).hexdigest() == reference.sha256, "persisted metadata bytes mismatch")
            metadata = TaskResponse.model_validate_json(json.dumps(read_json_object(path)))
            require(metadata.response_class == "SYNTHETIC_TEST" and metadata.source_commit == record.code_commit, "metadata source/class mismatch")
    database = run / "task_ledger.sqlite"
    connection = sqlite3.connect("file:" + quote(database.resolve().as_posix(), safe="/:") + "?mode=ro&immutable=1", uri=True)
    try:
        require(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity check failed")
        require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "SQLite foreign-key check failed")
    finally:
        connection.close()
    return {"reference_passed": True, "provider": "SYNTHETIC", "universe_rows": len(result.universe.rows),
            "quality_rows": len(result.quality.rows), "local_task_attempts": result.usage.attempt_count,
            "platform_execution": "NOT_RUN", "independent_review": "PENDING"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check_results(args.run), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
