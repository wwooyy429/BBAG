"""Package exact S2 source and source-bound developer evidence, pending review."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path, PureWindowsPath
import shutil
import sqlite3
import subprocess
import sys
from typing import Any
import zipfile

import bbag.s2_contracts as s2_contracts
from bbag.evidence_paths import validate_evidence_tree, validate_package_paths
from bbag.jsonio import read_json_object
from bbag.junit import validate_junit
from bbag.provenance import json_bytes
from bbag.s2_contracts import (S2Config, S2ReviewReceipt, S2RunManifest, S2SyntheticResult,
                               S2Verification, canonical_s2_config, load_s2_config)
from bbag.s2_data import canonical_request_sha256
from bbag.s2_quantconnect import S2_QC_CONTRACTS
from bbag.s2_tasks import RawMetadataRef, S2_TASK_CONTRACTS, TaskAttempt, TaskResponse, TaskSpec, canonical_task_bytes

S1_REVIEWED_SHA = "259962860d2301ead894c5d649a29eab1f8db1ac"
S1_MERGE_SHA = "7aac4886b50493c09235c49b5de90643b7090e07"


def git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=root, text=True, encoding="utf-8", stderr=subprocess.PIPE).strip()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_bytes(json_bytes(value))


def validate_source_archive(root: Path, commit: str, source: Path) -> None:
    tree = subprocess.check_output(["git", "ls-tree", "-rz", "--full-tree", commit], cwd=root)
    expected = {}
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, blob = metadata.decode("ascii").split(" ")
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("S2 source archive accepts regular committed files only")
        expected[raw_path.decode("utf-8")] = blob
    with zipfile.ZipFile(source) as archive:
        names = [entry.filename for entry in archive.infolist() if not entry.is_dir()]
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise ValueError("Source ZIP inventory differs from the exact Git tree")
        for name, blob in expected.items():
            raw = archive.read(name)
            actual = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
            if actual != blob:
                raise ValueError("Source ZIP bytes differ from a committed Git blob")


def checked_json(path: Path, model: type) -> Any:
    return model.model_validate_json(json.dumps(read_json_object(path), allow_nan=False))


def checked_stored_json(raw: str, model: type) -> Any:
    checked = model.model_validate_json(raw)
    if raw.encode("utf-8") != canonical_task_bytes(checked):
        raise ValueError("S2 stored task JSON must be canonical, without duplicate keys or unknown data")
    return checked


def validate_protocol(verification: S2Verification) -> None:
    # The recorded output root survives evidence relocation. All ten command
    # arguments must still be the fixed protocol, including that common root.
    pytest_record = next(record for record in verification.checks if record.name == "pytest")
    if len(pytest_record.command) != 5:
        raise ValueError("S2 pytest command must identify its actual absolute JUnit output")
    recorded_path = Path(pytest_record.command[-1])
    if not recorded_path.is_absolute():
        recorded_path = PureWindowsPath(pytest_record.command[-1])
    if not recorded_path.is_absolute() or recorded_path.name != "junit.xml":
        raise ValueError("S2 pytest command must identify its actual absolute JUnit output")
    recorded_output = recorded_path.parent
    specification = importlib.util.spec_from_file_location("bbag_s2_fixed_protocol", Path(__file__).with_name("verify_stage2.py"))
    if specification is None or specification.loader is None:
        raise ValueError("S2 fixed verification protocol is missing")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    expected = {name: [verification.implementation_commit if value == "{commit}" else value for value in args]
                for name, args, _ in module.commands(recorded_output)}
    executable = pytest_record.command[0]
    for record in verification.checks:
        if record.command != [executable, *expected[record.name]]:
            raise ValueError("S2 verification command differs from the fixed ten-check protocol")


def validate_platform_evidence(evidence: Path, commit: str) -> None:
    platform = evidence / "platform"
    if not platform.exists():
        return
    if not platform.is_dir():
        raise ValueError("Optional platform evidence must be a control-only directory")
    files = [path for path in platform.rglob("*") if path.is_file()]
    if not files or len(files) > 128:
        raise ValueError("Optional platform evidence must contain bounded typed control JSON")
    for path in files:
        if path.suffix != ".json" or path.stat().st_size > 1_000_000:
            raise ValueError("Platform attachments accept typed control JSON only, never raw logs/data")
        payload = read_json_object(path)
        accepted = False
        for model in (*S2_QC_CONTRACTS.values(), *S2_TASK_CONTRACTS.values()):
            try:
                model.model_validate_json(json.dumps(payload, allow_nan=False))
                accepted = True
                break
            except ValueError:
                continue
        if not accepted:
            raise ValueError("Platform attachment is not an allowlisted control contract")
        pending = [payload]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                if "source_commit" in item and item["source_commit"] != commit:
                    raise ValueError("Platform control metadata belongs to a different source SHA")
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)


def reference_check(root: Path, evidence: Path) -> dict:
    specification = importlib.util.spec_from_file_location("bbag_s2_package_reference", root / "scripts/check_stage2_results.py")
    if specification is None or specification.loader is None:
        raise ValueError("The committed S2 fixed reference checker is missing")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.check_results(evidence / "runs/synthetic", root=root)


def validate_ledger(directory: Path, config: S2Config, result: S2SyntheticResult, commit: str) -> None:
    """Read SQLite without changing bytes or rebinding its historical raw path."""
    db = directory / "task_ledger.sqlite"
    if not db.is_file():
        raise ValueError("S2 synthetic run is missing its actual persistent task ledger")
    task = result.task
    spec = task.spec
    if (spec.source_commit != commit or spec.config_sha256 != result.config_sha256
            or spec.request_sha256 != result.request_sha256 or spec.operation != "project_read"
            or spec.response_class != "SYNTHETIC_TEST" or spec.project_id is not None
            or task.status != "completed" or len(task.attempts) != 1):
        raise ValueError("S2 synthetic task identity, execution class or completion is inconsistent")
    attempt = task.attempts[0]
    if (attempt.platform_task_id is not None or attempt.invocation_id is not None or attempt.cell_index is not None
            or getattr(attempt, "sync_confirmation", None) is not None
            or attempt.actual_cost_microusd != 0 or attempt.success is not True or attempt.check_passed is not True):
        raise ValueError("Synthetic local task must not invent platform identifiers or costs")
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",) or connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("S2 task SQLite integrity check failed")
            metadata = dict(connection.execute("SELECT key,value FROM ledger_meta"))
            if metadata.get("schema_version") != "1.0" or metadata.get("budget") != canonical_task_bytes(config.task_budget).decode("utf-8") or not metadata.get("raw_dir"):
                raise ValueError("S2 persisted task budget/schema metadata differs from config")
            if connection.execute("SELECT id FROM configurations").fetchall() != [(task.configuration_id,)]:
                raise ValueError("S2 task configuration inventory differs from result")
            rows = connection.execute("SELECT id,configuration_id,spec_json FROM tasks").fetchall()
            if len(rows) != 1 or rows[0][:2] != (task.task_id, task.configuration_id) or checked_stored_json(rows[0][2], TaskSpec) != spec:
                raise ValueError("S2 persisted task specification differs from source-bound result")
            aliases = connection.execute("SELECT correlation_id,task_id FROM correlations ORDER BY correlation_id").fetchall()
            if aliases != [(alias, task.task_id) for alias in sorted(task.correlation_ids)]:
                raise ValueError("S2 persisted task aliases differ from result")
            rows = connection.execute("SELECT id,task_id,attempt_number,operation,attempt_json FROM attempts").fetchall()
            if len(rows) != 1 or rows[0][:4] != (attempt.local_attempt_id, task.task_id, 1, spec.operation) or checked_stored_json(rows[0][4], TaskAttempt) != attempt:
                raise ValueError("S2 persisted task attempt differs from result")
            events = connection.execute("SELECT attempt_id,event_id,payload_sha256,payload_kind,raw_json FROM events").fetchall()
            if len(events) != 1 or events[0][:2] != (attempt.local_attempt_id, "dispatch_response"):
                raise ValueError("S2 synthetic task must retain its one actual callback event")
            reference = checked_stored_json(events[0][4], RawMetadataRef)
            if attempt.raw_metadata != [reference] or events[0][2:4] != (reference.sha256, "TaskResponse"):
                raise ValueError("S2 task metadata references differ from immutable event records")
    except sqlite3.Error as exc:
        raise ValueError("S2 task ledger is not a valid readable task database") from exc
    path = directory / "metadata" / reference.relative_path
    if not path.is_file() or len(path.read_bytes()) != reference.size_bytes or digest(path) != reference.sha256:
        raise ValueError("S2 immutable task metadata bytes/hash are inconsistent")
    response = checked_json(path, TaskResponse)
    if (response.operation != spec.operation or response.source_commit != commit or response.config_sha256 != spec.config_sha256
            or response.request_sha256 != spec.request_sha256 or response.response_class != "SYNTHETIC_TEST"
            or response.status != "completed" or response.success is not True or response.check_passed is not True
            or response.actual_cost_microusd != 0 or response.project_id is not None or response.platform_task_id is not None
            or response.invocation_id is not None or response.cell_index is not None):
        raise ValueError("S2 raw local task metadata does not match the actual synthetic task")
    if {item.name for item in (directory / "metadata").iterdir()} != {reference.relative_path}:
        raise ValueError("S2 synthetic metadata directory contains unreferenced output")
    usage = result.usage
    if (usage.configuration_count, usage.task_count, usage.attempt_count, usage.confirmed_execution_count,
            usage.unknown_attempt_count, usage.active_attempt_count, usage.known_cost_microusd,
            usage.reserved_unknown_cost_microusd, usage.unknown_cost_attempt_count, usage.budget_cost_microusd) != (1, 1, 1, 0, 0, 0, 0, 0, 0, 0) or usage.operation_attempts != {"project_read": 1} or getattr(usage, "synchronous_result_count", 0) != 0:
        raise ValueError("S2 synthetic usage must count one local task without a platform execution")


def validate_evidence(evidence: Path, commit: str, root: Path | None = None) -> tuple[dict, dict, dict[str, int]]:
    root = root or Path(__file__).resolve().parents[1]
    validate_evidence_tree(evidence)
    allowed_files = {"verification.json", "review_request.json", "junit.xml", "DELIVERY_NOTES.md"}
    allowed_files.update(f"{name}.{stream}.txt" for name in s2_contracts.S2_REQUIRED_CHECKS for stream in ("stdout", "stderr"))
    if any(path.name not in (allowed_files if path.is_file() else {"runs", "platform"}) for path in evidence.iterdir()):
        raise ValueError("S2 evidence contains unrecognized files; platform attachments must use typed control JSON")
    if {path.name for path in (evidence / "runs").iterdir()} != {"synthetic"}:
        raise ValueError("S2 evidence must contain only the fixed synthetic local run")
    directory = evidence / "runs/synthetic"
    if {path.name for path in directory.iterdir()} != {"config.json", "result.json", "run.json", "task_ledger.sqlite", "metadata"}:
        raise ValueError("S2 synthetic run has unexpected, incomplete or uncheckpointed files")
    verification = checked_json(evidence / "verification.json", S2Verification)
    if (verification.implementation_commit != commit or verification.code_commit_end != commit
            or verification.working_tree_dirty_start or verification.working_tree_dirty_end or not verification.developer_checks_passed):
        raise ValueError("S2 evidence must pass all checks on the same unchanged clean implementation SHA")
    if verification.dependency_lock_sha256 != digest(root / "requirements.lock"):
        raise ValueError("S2 verification dependency lock hash differs from source")
    validate_protocol(verification)
    receipt = checked_json(evidence / "review_request.json", S2ReviewReceipt)
    if (receipt.audited_commit != commit or receipt.decision != "PENDING" or receipt.engineering_status != "INCOMPLETE"
            or receipt.platform_status != "NOT_RUN" or receipt.blocking_findings != 0 or receipt.evidence or receipt.reviewed_at is not None):
        raise ValueError("S2 audit request must remain unfilled PENDING for the exact source SHA")
    for record in verification.checks:
        for stream in ("stdout", "stderr"):
            if not (evidence / f"{record.name}.{stream}.txt").is_file():
                raise ValueError(f"Missing raw S2 check output: {record.name}.{stream}")
    counts = validate_junit(evidence / "junit.xml")
    validate_platform_evidence(evidence, commit)
    config = checked_json(directory / "config.json", S2Config)
    fixed_bytes = canonical_s2_config(load_s2_config(root / "configs/s2.example.json"))
    if (directory / "config.json").read_bytes() != canonical_s2_config(config) or (directory / "config.json").read_bytes() != fixed_bytes:
        raise ValueError("S2 run must use canonical bytes of the committed fixed synthetic example")
    # A cloud plan remains configuration only; merely validating it grants nothing.
    load_s2_config(root / "configs/s2.cloud-scope.json")
    result = checked_json(directory / "result.json", S2SyntheticResult)
    record = checked_json(directory / "run.json", S2RunManifest)
    config_hash = hashlib.sha256(fixed_bytes).hexdigest()
    if (record.run_id != "synthetic" or record.code_commit != commit or record.working_tree_dirty
            or record.config_sha256 != config_hash or result.config_sha256 != config_hash
            or record.result_sha256 != digest(directory / "result.json")
            or (directory / "result.json").read_bytes() != json_bytes(result.model_dump(mode="json"))):
        raise ValueError("S2 run source identity, config/result hashes or canonical bytes differ")
    if (result.request_sha256 != canonical_request_sha256(config.request) or config.bundle is None
            or result.universe.data_id != config.bundle.data_id or result.quality.data_id != config.bundle.data_id
            or result.universe.as_of != config.request.as_of or result.quality.as_of != config.request.as_of):
        raise ValueError("S2 result request/data/as_of differs from the fixed source config")
    validate_ledger(directory, config, result, commit)
    reference = reference_check(root, evidence)
    if (reference.get("reference_passed") is not True or reference.get("provider") != "SYNTHETIC"
            or reference.get("universe_rows") != len(result.universe.rows)
            or reference.get("quality_rows") != len(result.quality.rows)
            or reference.get("projected_s1_panel_sha256") != result.projected_s1_panel_sha256):
        raise ValueError("S2 fixed independent reference comparison failed")
    return verification.model_dump(mode="json"), receipt.model_dump(mode="json"), counts


def development_report(root: Path, evidence: Path, commit: str, verification: dict, counts: dict) -> str:
    table = "\n".join(f"| {item['name']} | {item['exit_code']} | {item['expected_exit_code']} | {item['duration_seconds']:.6f} |" for item in verification["checks"])
    extra = (evidence / "DELIVERY_NOTES.md").read_text(encoding="utf-8") if (evidence / "DELIVERY_NOTES.md").is_file() else "未提供补充执行记录，不宣称额外平台检查已执行。"
    platform = "包含 evidence/platform 控制元数据附件；其来源与实际执行范围以附件为准，导入本身不认证平台结果、不改变独立回执。" if (evidence / "platform").is_dir() else "未附平台控制证据；真实平台执行 NOT_RUN。"
    return f"""# BBAG S2 开发报告

准确实现 SHA：`{commit}`。源码提交后生成；独立审核保持 PENDING，S3 未获授权，不合并本次 S2。

前置 S1 被审实现：`{S1_REVIEWED_SHA}`；负责人授权的 S1 合并结果：`{S1_MERGE_SHA}`。本轮将这项已审基础修复集成回 S2；其批准不能覆盖本次 S2，新版本仍须独立复审。

S2 首轮提交 93ca41169420da474f139bdcf89ae73e0eb73350 的独立结论为 CHANGES_REQUIRED / 工程 FAIL，三个 P2 为 S2-001 UNKNOWN 恢复、S2-002 已知检查结论保持、S2-003 固定结果完整复核。本包交审的是这些修复的新准确 SHA，开发自测不产生独立关闭。原失败、平台 NOT_RUN、经济 NOT_APPLICABLE 和基础四项关闭记录保留，范围见 docs/S2_AUDIT_REMEDIATION.md。

本包包含原始美股日频标准契约、显式交易日历、SID/更名/退市及 known_at 成员事件、原始公司行动元数据与单事件持仓权益、滞后流动性、缺失/陈旧/异常覆盖检查、synthetic-only S1 投影、运行时访问/派生隔离、云端控制回执桥接与持久任务/预算/未知状态控制。真实云端原始及可重建数据必须留在云内；本地 gateway 仅接受严格控制元数据。配置文件不授予运行时访问权，外部回执导入不认证来源。

Python：`{verification['python']}`；平台：`{verification['platform']}`。JUnit：{json.dumps(counts, ensure_ascii=False)}；跳过项以原始明细为准。依赖锁：`{verification['dependency_lock_sha256']}`。开始/结束源码均为本 SHA 且干净。

| 检查 | 实际退出码 | 预期退出码 | 秒 |
|---|---|---|---|
{table}

pending_review_gate 预期退出 3。全部 stdout/stderr 原始字节、命令/时刻/耗时、JUnit、合成 run 规范配置/结果/manifest、SQLite 与严格控制 metadata 均在 evidence。打包再次核对固定提交样例和手工参考、原始哈希与源版本；开发验证不是独立验收。

{platform}

{extra}

合成任务是 SYNTHETIC_TEST，本地 bookkeeping ID 不冒充平台任务 ID；平台默认 NOT_RUN，经济 NOT_APPLICABLE。真实任务仍须实际平台证据与独立复验，不能凭 mock、导入、编译成功或任务接受宣称完整 S2 平台 PASS。S6.4 最终复验、封存测试及派生隔离、固定/滚动模型规则继续保留。未运行完整 F01—F08；不做 Research 经济评价、优化或部署，不开始 S3。
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--output-dir", default=Path("deliverables/stages/S2"), type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("Source directory must be the real Git checkout root, not an ancestor repository")
    if Path(s2_contracts.__file__).resolve() != root / "src/bbag/s2_contracts.py":
        raise ValueError("S2 contracts must be imported from this exact source checkout")
    commit = git(root, "rev-parse", "--verify", f"{args.commit}^{{commit}}")
    if git(root, "rev-parse", "HEAD") != commit or git(root, "status", "--porcelain"):
        raise ValueError("Check out the exact S2 implementation with an unchanged clean working tree")
    committed_lock = subprocess.check_output(["git", "show", f"{commit}:requirements.lock"], cwd=root, stderr=subprocess.PIPE)
    if digest(root / "requirements.lock") != hashlib.sha256(committed_lock).hexdigest():
        raise ValueError("S2 working dependency lock does not match the exact committed source")
    evidence, base = args.evidence_dir.resolve(), args.output_dir.resolve()
    output, archive_path = base / commit, base / f"BBAG_S2_GPT_Work_{commit[:12]}.zip"
    validate_package_paths(evidence, output, archive_path)
    required = ["requirements.lock", "configs/s2.example.json", "configs/s2.cloud-scope.json",
                "tests/fixtures/s2_raw.json", "tests/fixtures/s2_expected.json", "scripts/check_stage2_results.py",
                "scripts/verify_stage2.py", "scripts/build_stage2_package.py"]
    required.extend(f"src/bbag/{name}.py" for name in ("s2_contracts", "s2_access", "s2_data", "s2_quantconnect", "s2_tasks", "s2_cli"))
    git(root, "ls-files", "--error-unmatch", "--", *required)
    verification, receipt, counts = validate_evidence(evidence, commit, root)
    paths = git(root, "ls-tree", "-r", "--name-only", commit).splitlines()
    if any(set(Path(path).parts) & {".git", ".venv", "venv", "artifacts", "deliverables"} for path in paths):
        raise ValueError("Committed source must exclude generated artifacts, environments and .git")
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source.zip"
    subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "archive", "--format=zip", f"--output={source}", commit], cwd=root, check=True)
    validate_source_archive(root, commit, source)
    shutil.copytree(evidence, output / "evidence")
    validate_evidence(output / "evidence", commit, root)
    (output / "SOURCE_COMMIT.txt").write_text(commit + "\n", encoding="ascii")
    write_json(output / "review_request.json", receipt)
    (output / "DEVELOPMENT_REPORT.md").write_text(development_report(root, evidence, commit, verification, counts), encoding="utf-8")
    (output / "GPT_WORK_AUDIT_REQUEST.md").write_text(f"""# BBAG S2 独立审核请求

准确 SHA：`{commit}`；独立审核 PENDING。前置 S1 被审 `{S1_REVIEWED_SHA}`、合并 `{S1_MERGE_SHA}` 单独记录。

核对清单、Git/source.zip、锁、配置/原始夹具/手工标准答案与十项实际检查；独立构造日期、类型、字段、extra/duplicate JSON、可用时点、未来扰动、SID 更名/退市、公司行动原始口径、历史成员、滞后流动性、质量、sealed/派生/缓存/batch/export 防绕过、策略伪造、云内数据边界、预算/未知任务/重复完成与恢复反例。检查 raw控制 metadata/SQLite/源 SHA/config/request 哈希，不能凭未认证导入确认平台通过。

重点独立复测 docs/S2_AUDIT_REMEDIATION.md 的三个 P2：无 ID 超时后普通 error 导入不解除 UNKNOWN，重试和并发仍阻止；合法 ID/同步观察/未提交核查保持；已确认 result_available/completed 的 False/True 不被后事件反转，None 省略不擦除，未知首次补齐/补账单/省略 ID/幂等保持，矛盾拒绝时 DB 与 raw 目录不变。对完整 CLI 仅污染固定 result 并正确重算 run 摘要：session、signal_at、逐行质量/汇总和投影 hash 的错误均须 JSON 退出 2、在新交付 mkdir/source archive/ZIP 前停止，旧哨兵不变；正确输入跨 cwd 继续成功。原审核字面数学答案、原失败与旧 SHA 不改写。

本轮修复和复审不增加平台调用，不消耗最后一次 Research、不上传触发编译；累计 compile 2/2、Research 2/3、账单未知保持。S2-N01 快照时钟观察及 S0-N02 历史 P3 继续非阻断待办，不假称已修复。S0-004、S0-N01、S1-001、S1-002 的历史关闭保持；当前三个 S2 P2 是否关闭由本 SHA 独立实测决定。

逐项记录 EXECUTED/STATIC_ONLY/NOT_RUN、预期/实际、退出码及原始证据。真实平台验收仅依据实际授权范围内的真实任务，许可数据留云内，不能从本包合成验证推定 PASS。经济 NOT_APPLICABLE；保留 S6.4 与最终派生隔离。填写绑定本 SHA 的 S2 回执；不得合并、启动 S3 或自动改变权限。
""", encoding="utf-8")
    (output / "AUDIT_RESULT_TEMPLATE.md").write_text(f"""# BBAG S2 独立审核模板

被审 SHA：`{commit}`。模板未执行；输入/环境/工具版本、方法、EXECUTED/STATIC_ONLY/NOT_RUN、预期、实际/退出码、原始证据、缺陷与限制均待独立审核者填写。

工程 INCOMPLETE；平台 NOT_RUN；经济 NOT_APPLICABLE；决定 PENDING。审核来源、阻断数、时间与证据待填写。通过不自动合并或开始 S3；保留 S6.4、最终隔离与固定/滚动模型规则。
""", encoding="utf-8")
    (output / "REPRODUCE.md").write_text(f"""# 复现准确 S2 提交

在真实 Git checkout 中检出 `{commit}`，用 Python 3.11+ 创建 venv，安装 requirements.lock 后执行 pip install --no-deps --no-build-isolation -e .。

```powershell
python scripts/verify_stage2.py --output-dir artifacts/independent-s2
python scripts/build_stage2_package.py --commit {commit} --evidence-dir artifacts/independent-s2 --output-dir deliverables/independent-s2
```

输出必须全新；Linux/macOS 使用相应 venv Python。验证器只执行固定本地十项，不调用真实平台。真实平台复验需单独核实当前 owner grant、成本边界、可信策略哈希及项目，只允许授权控制回执离开云端。source.zip 无 .git，可供阅读/普通 pytest，不能冒充真实 Git 来源用于正式验证采集。合成夹具不产生经济结论，模板不构成审核。
""", encoding="utf-8")
    if git(root, "rev-parse", "HEAD") != commit or git(root, "status", "--porcelain"):
        raise ValueError("Source changed during S2 packaging; preserve evidence and verify a fresh SHA")
    entries = [{"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size, "sha256": digest(path)} for path in sorted(output.rglob("*")) if path.is_file()]
    write_json(output / "manifest.json", dict(stage="S2", implementation_commit=commit, s1_reviewed_commit=S1_REVIEWED_SHA,
        s1_merge_commit=S1_MERGE_SHA, repository="https://github.com/wwooyy429/BBAG", generated_after_commit=True,
        generated_at_utc=datetime.now(timezone.utc).isoformat(), developer_checks_passed=True, independent_review="PENDING",
        platform_integration="NOT_RUN", economic_evidence="NOT_APPLICABLE", merged_into_main=False, next_stage_authorized=False,
        source_archive_sha256=digest(source), files=entries))
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output).as_posix())
    print(json.dumps(dict(implementation_commit=commit, report_directory=str(output), package=str(archive_path), package_sha256=digest(archive_path), independent_review="PENDING"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"stage": "S2", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
