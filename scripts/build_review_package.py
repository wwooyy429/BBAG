"""Package the exact committed S0 source and matching developer evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bbag.contracts import ExperimentRecord, ResearchProtocol, ReviewReceipt, canonical_protocol_bytes
from bbag.evidence_paths import validate_evidence_tree, validate_package_paths
from bbag.jsonio import read_json_object
from bbag.junit import validate_junit


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False), encoding="utf-8")


def git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=root, text=True, encoding="utf-8").strip()


def read_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"Required evidence file is missing: {path}")
    return read_json_object(path)


def validate_evidence(evidence: Path, commit: str) -> tuple[dict, dict, dict[str, int]]:
    validate_evidence_tree(evidence)
    verification = read_object(evidence / "verification.json")
    if verification.get("implementation_commit") != commit or verification.get("working_tree_dirty") is not False:
        raise ValueError("Verification evidence is not from this clean implementation commit")
    verification_status = {
        "stage": "S0", "independent_review": "PENDING",
        "platform_integration": "NOT_APPLICABLE", "economic_evidence": "NOT_APPLICABLE",
    }
    if (any(verification.get(key) != value for key, value in verification_status.items())
            or verification.get("developer_checks_passed") is not True
            or verification.get("next_stage_authorized") is not False):
        raise ValueError("Developer evidence must pass S0 checks without claiming independent approval or advancement")
    if not isinstance(verification.get("python"), str) or not verification["python"].strip():
        raise ValueError("Verification must record its actual Python version")
    receipt = read_object(evidence / "review_request.json")
    # JSON mode preserves strict ISO datetime validation after ambiguity checks.
    ReviewReceipt.model_validate_json(json.dumps(receipt, allow_nan=False))
    pending_status = {
        "stage": "S0", "audited_commit": commit, "decision": "PENDING",
        "engineering_status": "INCOMPLETE", "platform_status": "NOT_APPLICABLE",
        "economic_status": "NOT_APPLICABLE",
    }
    if (any(receipt.get(key) != value for key, value in pending_status.items())
            or receipt.get("reviewed_at") is not None
            or receipt.get("blocking_findings") != 0 or receipt.get("evidence") != []):
        raise ValueError("Audit request must remain pending and bind the S0 implementation commit")
    required_checks = {
        "pip_check": 0, "pytest": 0, "validate": 0, "schemas": 0,
        "experiment": 0, "pending_review_gate": 3,
    }
    checks = verification.get("checks")
    if not isinstance(checks, list) or len(checks) != len(required_checks):
        raise ValueError("Verification must contain exactly the six required S0 checks")
    seen = set()
    for record in checks:
        if not isinstance(record, dict) or not isinstance(record.get("name"), str):
            raise ValueError("Invalid developer check record")
        name = record["name"]
        if name not in required_checks or name in seen:
            raise ValueError("Required S0 check names must be complete and unique")
        seen.add(name)
        expected = required_checks[name]
        if (record.get("passed") is not True
                or type(record.get("exit_code")) is not int or record["exit_code"] != expected
                or type(record.get("expected_exit_code")) is not int or record["expected_exit_code"] != expected):
            raise ValueError(f"Unexpected developer verification result: {name}")
        command = record.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError(f"Raw reproduction command is missing for check: {name}")
        for stream in ("stdout", "stderr"):
            path = evidence / f"{name}.{stream}.txt"
            if not path.is_file():
                raise ValueError(f"Required raw verification output is missing: {path}")
    junit = evidence / "junit.xml"
    if not junit.is_file():
        raise ValueError("Required JUnit evidence is missing")
    counts = validate_junit(junit)
    experiment_dir = evidence / "experiments" / "s0-demo"
    protocol_path = experiment_dir / "protocol.json"
    protocol = read_object(protocol_path)
    experiment = read_object(experiment_dir / "experiment.json")
    checked_protocol = ResearchProtocol.model_validate(protocol)
    ExperimentRecord.model_validate_json(json.dumps(experiment, allow_nan=False))
    # The collector writes canonical bytes. Both semantic validity and exact raw
    # bytes are required; recomputing a hash cannot legitimize invalid content.
    if protocol_path.read_bytes() != canonical_protocol_bytes(checked_protocol):
        raise ValueError("Experiment protocol evidence must use canonical protocol bytes")
    if (protocol.get("stage") != "S0" or protocol.get("purpose") != "ENGINEERING_TEST"
            or protocol.get("status") != "DRAFT"):
        raise ValueError("Experiment protocol must remain an S0 ENGINEERING_TEST DRAFT")
    experiment_status = {
        "schema_version": "1.0", "run_id": "s0-demo", "stage": "S0", "purpose": "ENGINEERING_TEST",
        "code_commit": commit, "validation_status": "VALIDATED", "platform_status": "NOT_RUN",
        "economic_status": "NOT_EVALUATED", "review_status": "PENDING",
        "protocol_sha256": digest(protocol_path),
    }
    if (any(experiment.get(key) != value for key, value in experiment_status.items())
            or experiment.get("working_tree_dirty") is not False):
        raise ValueError("Experiment evidence must bind the clean S0 implementation and exact draft protocol")
    return verification, receipt, counts


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--output-dir", default=Path("deliverables/stages/S0"), type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("Source directory must be the Git checkout root; ancestor repositories are not valid provenance")
    commit = git(root, "rev-parse", "--verify", f"{args.commit}^{{commit}}")
    if commit != git(root, "rev-parse", "HEAD"):
        raise ValueError("Check out the reviewed commit before packaging it")
    if git(root, "status", "--porcelain"):
        raise ValueError("Committed source must be clean before packaging")
    evidence = args.evidence_dir.resolve()
    output_base = args.output_dir.resolve()
    output = output_base / commit
    destination = output_base / f"BBAG_S0_GPT_Work_{commit[:12]}.zip"
    validate_package_paths(evidence, output, destination)
    verification, receipt, counts = validate_evidence(evidence, commit)
    output.mkdir(parents=True, exist_ok=False)
    source_zip = output / "source.zip"
    subprocess.run(["git", "archive", "--format=zip", f"--output={source_zip}", commit], cwd=root, check=True)
    shutil.copytree(evidence, output / "evidence")
    (output / "SOURCE_COMMIT.txt").write_text(commit + "\n", encoding="ascii")
    write_json(output / "review_request.json", receipt)
    test_summary = ", ".join(f"{key}={value}" for key, value in counts.items())
    checks_table = "\n".join(
        f"| {item['name']} | {item['exit_code']} | {item['expected_exit_code']} | {'PASS' if item['passed'] else 'FAIL'} |"
        for item in verification["checks"]
    )
    remediation = root / "docs/S0_SUPPLEMENT_CHECK.md"
    remediation_details = remediation.read_text(encoding="utf-8") if remediation.is_file() else ""
    remediation_report = evidence / "remediation/S0_FIX_REPORT.md"
    remediation_results = remediation_report.read_text(encoding="utf-8") if remediation_report.is_file() else "补充复验报告未提供，不能宣称已执行额外检查。"
    development = f"""# BBAG S0 开发报告

实现 commit SHA：`{commit}`

本报告在提交后生成，审核对象为 source.zip 中该 SHA 的完整源码。开发分支为 `{git(root, 'branch', '--show-current')}`，独立 GPT Work 审核保持 PENDING；未合并 main，未进入 S1。

## 本轮 S0 修复

本轮基线：`2eccc6418674314c2463a34783b8dd9fa1ad2971`。按负责人补充材料核查 S0，修复四项状态与交付边界遗漏。此前 `b2d502048dfed1f8765514a018bc91560d87ec22` 的 CHANGES_REQUIRED 及原始失败记录继续保留；两版旧材料均不构成本实现的独立验收。

- S0-SUP-001：S0 结构批准要求平台 NOT_APPLICABLE，拒绝矛盾的批准状态。
- S0-SUP-002：打包前拒绝证据子链接、junction、根外路径及特殊文件，普通云占位符不因 reparse 标记被误拒。
- S0-SUP-003：在创建输出前拒绝证据与实际交付路径重叠，防止复制回输入。
- S0-SUP-004：前置检查已有 SHA 目录和 ZIP，ZIP 使用排他创建，避免覆盖或在已知冲突后留下就绪形态的部分交付。
- 前轮严格 JSON、JUnit 明细、完整契约与规范字节/哈希/SHA 校验保留；DRAFT 参数继续待负责人确认。

## 保留的 S0 基础能力

- Python 工程骨架、依赖锁定和离线命令入口。
- 数据、因子、标签、执行、评估、实验记录和发布清单等标准契约及 JSON Schema。
- 合成 ENGINEERING_TEST 和 DRAFT 研究协议，显式记录待确定参数。
- 严格类型、未知字段、日期与数据分区、预算和跨契约一致性校验。
- 实验清单及协议哈希、Git SHA 和工作区状态记录。
- 审核回执与 SHA 的一致性检查；检查器仅提供 advisory 结果，不认证审核者、不合并、不自动推进。
- Git 开发分支、直接推送 main 的本地阻止钩子、CI 和阶段交接规范。

## 实际开发验证

执行 Python：`{verification['python']}`。JUnit：{test_summary}。

| 检查 | 实际退出码 | 预期退出码 | 开发自测 |
|---|---|---|---|
{checks_table}

pending_review_gate 的预期退出码为 3，表示未取得独立批准时保持阻止。原始命令、输出、JUnit、实验清单和待审核请求均在 evidence 中。

`evidence/prior_audit/` 保留负责人提供的旧审核及失败记录；原报告注明独立 Codex 会话、未认证 GPT Work 身份。`evidence/remediation/` 保存本轮缺陷对照、原测试适配差异、开发者复跑及 CI 状态（存在时以实际文件为准）。开发者重跑原审核测试不产生独立验收结论。PR 为 https://github.com/wwooyy429/BBAG/pull/1 ，保持草稿。

配置/契约版本 1.0，样例保持 ENGINEERING_TEST/DRAFT；依赖锁随 source.zip 交付，数据为合成样例，模型与平台版本不适用。S0 外部模型、平台任务和费用消耗为零；GitHub CI 仅属于开发验证。测试耗时及实际环境见 verification.json，内存、存储、并发、延迟门槛尚待项目负责人确认，未根据本轮观测倒填。

F01—F08 完整业务路径均为 NOT_RUN（依赖 S1—S7）；本轮仅实际执行适用的 S0 契约、CLI、来源与证据接纳回归。平台与经济结论均为 NOT_APPLICABLE，经济评估执行有效性不适用。

## 缺陷对照与补充实测

{remediation_results}

## 尚未实现或验证

S1 因子算子、时序运行审计及因子有效性评估尚未实现。没有 GPT API、QuantConnect 研究/回测/优化、模拟或真实资金部署；平台集成和经济证据在 S0 不适用。配置的日期边界校验不替代后续平台访问控制。2026-10-31 仓库初始化核实时服务器分支保护未启用；打包器不重新查询远端，当前保护状态须另行核实。当前采用开发分支、草稿 PR、本地 hook 和人工治理，hook 不构成服务器硬门禁。

当前状态为 READY_FOR_REVIEW。开发自测与 CI 不能代替独立 GPT Work 对此 SHA 的审核。任何受影响源码、契约或配置变化都需要新提交、新证据和新审核包。

## 修复约束与旧审核适用范围

{remediation_details}
"""
    (output / "DEVELOPMENT_REPORT.md").write_text(development, encoding="utf-8")
    audit = f"""# BBAG S0 GPT Work 独立审核任务

审核 commit SHA：`{commit}`

请独立审核 source.zip 中这一准确版本。此文件为待执行任务，不是审核通过报告。生产源码修复交回 Codex，在独立副本中新增测试与报告。

## 复现与检查

1. 核验 SOURCE_COMMIT.txt、manifest.json、源码压缩包和 evidence 的版本与哈希。
2. 解压源码，按 REPRODUCE.md 创建环境、安装锁定依赖并重跑测试。
3. 独立构造缺字段、未知字段、负数、布尔冒充数字、非有限值、重叠日期、越界查询、时间规则冲突及非法阶段/用途反例。
4. 核对生成的 JSON Schema、协议哈希、实验清单和 Git 源码状态；尝试覆盖或逃逸实验输出目录。
5. 检查审核缺失、PENDING、陈旧 SHA、需要修改时保持阻止；结构合格回执仍不认证审核者或执行合并。
6. 确认 S0 未实现 S1 逻辑、未调用平台或模型服务，DRAFT 配置未被当作研究批准。
7. 检查分支和报告治理：main 只含初始化标记，当前实现等待此 SHA 的独立审核。
8. 对照 evidence/prior_audit 的 S0-001—S0-003 原反例逐项独立复测；检查完整 JSON 入口、嵌套 JUnit 明细及协议/实验/回执契约，失败不得生成包。自行增加反例，不以开发者原测试复跑代替独立判断。
9. 核对 evidence/remediation（如有）的原件哈希、适配差异、原始失败与新结果，以及新源码与 PR 的准确 SHA；旧版工程 FAIL 不自动覆盖修复效果，新版也不得自填 ACCEPTED。
10. 对照 docs/S0_SUPPLEMENT_CHECK.md 复测 S0 回执平台状态、证据 symlink/junction 与越界、输入输出重叠、已有 ZIP 冲突；所有拒绝均须发生在新交付目录和源码 archive 创建前。普通目录、合法兄弟路径及云占位符正例须保留。

## 结论要求

当前独立审核状态 PENDING，工程验收待你执行后判定；平台集成和经济证据在 S0 为 NOT_APPLICABLE。每项标记 EXECUTED、STATIC_ONLY 或 NOT_RUN，保留命令、输入、实际结果和缺陷复现。

审核完成后生成独立报告及绑定 `{commit}` 的 review_receipt.json。请勿直接改写此包的原始开发证据。未通过时交回缺陷清单，继续禁止合并 main 和进入 S1。通过也不在本任务中自动合并或开展阶段 1。
"""
    (output / "GPT_WORK_AUDIT_REQUEST.md").write_text(audit, encoding="utf-8")
    reproduce = f"""# 复现 S0

准确源码版本：`{commit}`。推荐先核对本包哈希，再使用独立目录。

```powershell
git clone https://github.com/wwooyy429/BBAG.git BBAG-review
Set-Location BBAG-review
git checkout --detach {commit}
py -3.13 -m venv .venv
.\\.venv\\Scripts\\python.exe -m pip install -r requirements.lock
.\\.venv\\Scripts\\python.exe -m pip install --no-deps --no-build-isolation -e .
.\\.venv\\Scripts\\python.exe -m pytest
.\\.venv\\Scripts\\python.exe -m bbag validate --config configs/s0.example.json
.\\.venv\\Scripts\\python.exe -m bbag schemas --output-dir contracts --check
.\\.venv\\Scripts\\python.exe scripts/verify_stage0.py --output-dir artifacts/independent-review
```

无 Git 访问时可解压 source.zip，在该目录创建环境并运行安装、pytest、validate、schemas 和 init-experiment 命令；verify_stage0.py 和打包脚本需要真实 Git checkout，不适用于无 .git 的目录。没有 .git 时 CLI 实验来源会显式记为未知且非干净状态，不伪造 commit；请另核对 SOURCE_COMMIT.txt 和压缩包清单。Linux/macOS 使用 python3 和 .venv/bin/python。开发验证包中的原始日志是开发证据，独立报告必须记录你实际重跑的结果。
"""
    (output / "REPRODUCE.md").write_text(reproduce, encoding="utf-8")
    template = root / "docs/handoff/templates/AUDIT_RESULT_TEMPLATE.md"
    if template.is_file():
        body = template.read_text(encoding="utf-8")
        body = body.replace("# GPT 独立审核结果模板", "# BBAG S0 GPT Work 独立审核结果模板", 1)
        body = body.replace("## 审核对象与环境", f"本次被审实现 SHA：`{commit}`。范围仅 S0，独立审核尚未执行；平台及经济研究在本阶段不适用。\n\n## 审核对象与环境", 1)
        body = body.replace("| 阶段与审核范围 | 待填写 |", "| 阶段与审核范围 | S0 工程骨架、研究规范、契约、CLI 与来源记录 |")
        body = body.replace("| 被审源码提交或内容哈希 | 待填写 |", f"| 被审源码提交或内容哈希 | `{commit}` |")
        body = body.replace("| 平台集成 | NOT_RUN | 待审核 |", "| 平台集成 | NOT_APPLICABLE | S0 不调用平台；不得据此宣称 S2 集成通过 |")
        body = body.replace("| 因子经济证据 | NOT_EVALUATED 或阶段不适用 | 待审核 |", "| 因子经济证据 | NOT_APPLICABLE | S0 不计算因子经济有效性 |")
        (output / "AUDIT_RESULT_TEMPLATE.md").write_text(body, encoding="utf-8")
    files = [{"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size, "sha256": digest(path)}
             for path in sorted(output.rglob("*")) if path.is_file()]
    manifest = {
        "stage": "S0", "implementation_commit": commit, "repository": "https://github.com/wwooyy429/BBAG",
        "generated_after_commit": True, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "developer_checks_passed": True, "independent_review": "PENDING", "merged_into_main": False,
        "next_stage_authorized": False, "source_archive_sha256": digest(source_zip), "files": files,
        "validation_scope": "S0 developer tests and package integrity; no independent acceptance or platform tests",
    }
    write_json(output / "manifest.json", manifest)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output).as_posix())
    print(json.dumps({"implementation_commit": commit, "package": str(destination),
                      "package_sha256": digest(destination), "report_directory": str(output),
                      "independent_review": "PENDING"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
