"""Package the exact committed S0 source and matching developer evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False), encoding="utf-8")


def git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=root, text=True, encoding="utf-8").strip()


def read_object(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"Required evidence file is missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Evidence must be a JSON object: {path}")
    return value


def validate_evidence(evidence: Path, commit: str) -> tuple[dict, dict, dict[str, int]]:
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
    pending_status = {
        "stage": "S0", "audited_commit": commit, "decision": "PENDING",
        "engineering_status": "INCOMPLETE", "platform_status": "NOT_APPLICABLE",
        "economic_status": "NOT_APPLICABLE",
    }
    if (any(receipt.get(key) != value for key, value in pending_status.items())
            or receipt.get("reviewed_at") is not None):
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
    try:
        suites = list(ET.parse(junit).getroot().iter("testsuite"))
    except ET.ParseError as exc:
        raise ValueError("JUnit evidence is not valid XML") from exc
    counts = {key: sum(int(suite.get(key, "0")) for suite in suites)
              for key in ("tests", "failures", "errors", "skipped")}
    if (any(value < 0 for value in counts.values()) or counts["tests"] <= counts["skipped"]
            or counts["failures"] or counts["errors"]):
        raise ValueError("JUnit evidence does not show a successful nonempty executed test suite")
    experiment_dir = evidence / "experiments" / "s0-demo"
    protocol_path = experiment_dir / "protocol.json"
    protocol = read_object(protocol_path)
    experiment = read_object(experiment_dir / "experiment.json")
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
    verification, receipt, counts = validate_evidence(evidence, commit)
    output_base = args.output_dir.resolve()
    output = output_base / commit
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
    development = f"""# BBAG S0 开发报告

实现 commit SHA：`{commit}`

本报告在提交后生成，审核对象为 source.zip 中该 SHA 的完整源码。开发分支为 `{git(root, 'branch', '--show-current')}`，独立 GPT Work 审核保持 PENDING；未合并 main，未进入 S1。

## 本次交付

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

## 尚未实现或验证

S1 因子算子、时序运行审计及因子有效性评估尚未实现。没有 GPT API、QuantConnect 研究/回测/优化、模拟或真实资金部署；平台集成和经济证据在 S0 不适用。配置的日期边界校验不替代后续平台访问控制。2026-10-31 仓库初始化核实时服务器分支保护未启用；打包器不重新查询远端，当前保护状态须另行核实。当前采用开发分支、草稿 PR、本地 hook 和人工治理，hook 不构成服务器硬门禁。

当前状态为 READY_FOR_REVIEW。开发自测与 CI 不能代替独立 GPT Work 对此 SHA 的审核。任何受影响源码、契约或配置变化都需要新提交、新证据和新审核包。
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
    destination = output_base / f"BBAG_S0_GPT_Work_{commit[:12]}.zip"
    if destination.exists():
        raise ValueError("Audit archive already exists")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
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
