"""Package the exact S1 implementation and validated engineering evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
import zipfile

import bbag.s1_contracts as s1_contracts
from bbag.evidence_paths import validate_evidence_tree, validate_package_paths
from bbag.jsonio import read_json_object
from bbag.junit import validate_junit
from bbag.provenance import json_bytes
from bbag.s1_cli import audit_config
from bbag.s1_contracts import (
    FactorResult, LabelResult, S1Config, S1ReviewReceipt, S1RunManifest, S1Verification,
    canonical_s1_bytes, load_s1_config, utc,
)

S0_REVIEWED_SHA = "f46fad2ff4b3ec9659001659fa44201e10951fd5"
S0_MERGE_SHA = "b2555efe7201c473bdb66c7dd1c14552ad130ac5"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=root, text=True,
                                   encoding="utf-8", stderr=subprocess.PIPE).strip()


def write_json(path: Path, value: object) -> None:
    path.write_bytes(json_bytes(value))


def checked_json(path: Path, model: type) -> Any:
    return model.model_validate_json(json.dumps(read_json_object(path), allow_nan=False))


def reference_check(root: Path, evidence: Path) -> dict:
    source = root / "scripts/check_stage1_results.py"
    specification = importlib.util.spec_from_file_location("bbag_s1_package_reference", source)
    if specification is None or specification.loader is None:
        raise ValueError("The committed S1 hand/rational reference checker is missing")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    runs = evidence / "runs"
    return module.check_results(root / "tests/fixtures/s1_expected.json",
                                runs / "batch", runs / "incremental", runs / "labels")


def validate_schedule(result: FactorResult | LabelResult, config: S1Config) -> None:
    """Bind timing metadata to the supplied calendar without recomputing values."""
    formed = [session for session in config.panel.sessions if utc(session.signal_at) <= utc(config.as_of)]
    expected_keys = [(session.session, asset) for session in formed for asset in sorted(config.universe)]
    if [(row.session, row.asset) for row in result.rows] != expected_keys:
        raise ValueError("S1 result rows do not cover the declared formed sessions and universe")
    sessions = {session.session: (index, session) for index, session in enumerate(config.panel.sessions)}
    observations = {(row.session, row.asset): row for row in config.panel.observations}
    for row in result.rows:
        index, session = sessions[row.session]
        if utc(row.signal_at) != utc(session.signal_at):
            raise ValueError("S1 result signal timestamp differs from its source session")
        if not isinstance(result, LabelResult):
            continue
        entry_index = index + 1
        exit_index = entry_index + config.label.horizon_sessions
        entry = config.panel.sessions[entry_index] if entry_index < len(config.panel.sessions) else None
        exit_session = config.panel.sessions[exit_index] if exit_index < len(config.panel.sessions) else None
        for actual, expected in ((row.entry_at, None if entry is None else entry.open_at),
                                 (row.exit_at, None if exit_session is None else exit_session.open_at)):
            if (actual is None) != (expected is None) or (actual is not None and utc(actual) != utc(expected)):
                raise ValueError("S1 label entry/exit timestamp differs from the source session schedule")
        maturity = None
        if entry is not None and exit_session is not None:
            assets = [row.asset]
            if config.label.benchmark_asset is not None:
                assets.append(config.label.benchmark_asset)
            required = [observations.get((period.session, asset)) for asset in assets for period in (entry, exit_session)]
            if all(observation is not None for observation in required):
                maturity = max(utc(exit_session.open_at), *(utc(observation.available_at) for observation in required))
        if (row.mature_at is None) != (maturity is None) or (row.mature_at is not None and utc(row.mature_at) != maturity):
            raise ValueError("S1 label maturity timestamp differs from source data availability")


def validate_evidence(evidence: Path, commit: str, root: Path | None = None) -> tuple[dict, dict, dict[str, int]]:
    """Validate structure, hashes and source-bound references, not reviewer identity."""
    root = root or Path(__file__).resolve().parents[1]
    validate_evidence_tree(evidence)
    verification = checked_json(evidence / "verification.json", S1Verification)
    if (verification.implementation_commit != commit or verification.code_commit_end != commit
            or verification.working_tree_dirty or not verification.developer_checks_passed):
        raise ValueError("S1 evidence must pass all checks on the same unchanged clean implementation SHA")
    if verification.dependency_lock_sha256 != digest(root / "requirements.lock"):
        raise ValueError("S1 verification dependency lock hash does not match the committed source")
    receipt = checked_json(evidence / "review_request.json", S1ReviewReceipt)
    if (receipt.audited_commit != commit or receipt.decision != "PENDING"
            or receipt.engineering_status != "INCOMPLETE" or receipt.blocking_findings != 0
            or receipt.evidence or receipt.reviewed_at is not None):
        raise ValueError("S1 audit request must remain unfilled PENDING for the exact S1 SHA")
    for record in verification.checks:
        for stream in ("stdout", "stderr"):
            if not (evidence / f"{record.name}.{stream}.txt").is_file():
                raise ValueError(f"Missing raw S1 check output: {record.name}.{stream}")
    counts = validate_junit(evidence / "junit.xml")
    expected_config = load_s1_config(root / "configs/s1.example.json")
    expected_bytes = canonical_s1_bytes(expected_config)
    expected_hash = hashlib.sha256(expected_bytes).hexdigest()
    results: dict[str, FactorResult | LabelResult] = {}
    for run_id, kind, model in (
        ("batch", "factors_batch", FactorResult),
        ("incremental", "factors_incremental", FactorResult),
        ("labels", "labels", LabelResult),
    ):
        directory = evidence / "runs" / run_id
        config_path, result_path = directory / "config.json", directory / "result.json"
        config = checked_json(config_path, S1Config)
        result = checked_json(result_path, model)
        record = checked_json(directory / "run.json", S1RunManifest)
        if (config_path.read_bytes() != canonical_s1_bytes(config)
                or config_path.read_bytes() != expected_bytes):
            raise ValueError("S1 run config must use canonical bytes of the committed fixed example input")
        if (record.run_id != run_id or record.kind != kind or record.code_commit != commit
                or record.working_tree_dirty or record.config_sha256 != expected_hash):
            raise ValueError("S1 run identity, kind, source SHA, clean state or config hash is inconsistent")
        if (record.result_sha256 != digest(result_path)
                or result_path.read_bytes() != json_bytes(result.model_dump(mode="json"))):
            raise ValueError("S1 result bytes or hash are inconsistent")
        if result.data_id != config.panel.data_id or utc(result.as_of) != utc(config.as_of):
            raise ValueError("S1 result data/as_of do not match the committed config")
        if isinstance(result, FactorResult):
            if (result.mode != run_id or result.factor_id != config.factor.factor_id
                    or result.audit != audit_config(config)):
                raise ValueError("S1 factor mode, identity or expression audit is inconsistent")
        elif result.rule != config.label:
            raise ValueError("S1 label rule differs from the committed config")
        validate_schedule(result, config)
        results[run_id] = result
    if results["batch"].rows != results["incremental"].rows:
        raise ValueError("S1 batch and incremental factor rows differ")
    reference = reference_check(root, evidence)
    if reference.get("reference_passed") is not True:
        raise ValueError("S1 hand/rational reference comparison did not pass")
    return verification.model_dump(mode="json"), receipt.model_dump(mode="json"), counts


def development_report(root: Path, evidence: Path, commit: str, verification: dict, counts: dict) -> str:
    histories = [name for name in ("prior_review", "prior_audit") if (evidence / name).is_dir()]
    history = ("本包实际包含历史输入目录：" + "、".join(f"`evidence/{name}/`" for name in histories)
               + "。其结论只覆盖各自准确版本，不构成 S1 批准。"
               if histories else "本包未包含可选历史审核目录；前置 S0 版本与合并结果按下列准确 SHA 记录，不声称附有原审核文件。")
    additions = evidence / "DELIVERY_NOTES.md"
    extra = additions.read_text(encoding="utf-8") if additions.is_file() else "未提供补充执行记录，不宣称执行额外检查。"
    table = "\n".join(f"| {item['name']} | {item['exit_code']} | {item['expected_exit_code']} | {item['duration_seconds']:.6f} |"
                      for item in verification["checks"])
    return f"""# BBAG S1 开发报告

准确实现 SHA：`{commit}`。本报告在实现提交后生成，source.zip 导出该提交；独立审核保持 PENDING，阶段状态 READY_FOR_REVIEW。

## 授权与基线

历史上负责人验收 S0 后明确授权合并并开始 S1。S0 被审实现：`{S0_REVIEWED_SHA}`；首次 S0 合并结果：`{S0_MERGE_SHA}`。S1 后续 c56db883cd7841c94517ee9817d7ad551e811dc2 经负责人接受独立复审并授权合并为 41789706f710e131d4a654e349798ddb15ea70f5，后者是本轮 S0-004 修复基线。最新联合复审因 S0-004 更新为 CHANGES_REQUIRED；S1 功能通过且 S1-001 保持关闭，历史批准不能覆盖本次新 SHA。当前源码分支：`{git(root, 'branch', '--show-current')}`。具体范围见 docs/S0_LOCK_HASH_REMEDIATION.md。

## 实际交付范围

受限公式与不可变语法树、字段/窗口/复杂度/参数/回看审计，最小确定性算子集；批量和逐期因子接口；分离的未来标签服务及入场/退出/成熟记录；严格 S1 类型契约与 Schema；ENGINEERING_TEST/DRAFT 合成样例、手算/有理数标准答案；S1 专用 CLI、十项验证采集、来源/哈希和固定版本交审工具。

因子可用时点、缺失、窗口不足、除零、非有限值、标准差及排名规则由 S1 规范和样例固定。未来数据扰动、截断与完整输入、批量与逐期一致性及手算参考见真实测试，不要求未来收益标签对未来数据变化保持不变。

## 实际开发验证

Python：`{verification['python']}`；平台：`{verification['platform']}`。JUnit：{json.dumps(counts, ensure_ascii=False)}。跳过项以原始 JUnit 和 pytest 输出为准，不计为执行通过。依赖锁 SHA-256：`{verification['dependency_lock_sha256']}`。代码开始/结束均为本 SHA，工作区均干净。

| 检查 | 实际退出码 | 预期退出码 | 秒 |
|---|---|---|---|
{table}

`pending_review_gate` 预期退出 3，表示 S1 尚无独立批准。所有 stdout/stderr 原字节、命令、时刻、耗时、JUnit、三个 run 的规范配置/结果/manifest 均在 evidence。打包时另实际校验固定提交样例、完整契约、原始哈希、真实 JUnit 明细、两个因子结果及独立手算/有理数参考。

{history}

## 补充事实与未验证项

{extra}

S1 无 GPT API、QuantConnect、真实数据、Research 经济评估、LEAN 回测、优化或部署；外部模型/量化平台调用及费用为零。平台集成和经济结论均为 NOT_APPLICABLE，经济评估执行有效性不适用。合成价格收益标签不冒充含公司行动的真实总收益。正式市场、数据权限、研究参数、资源门槛及统计准入仍待负责人确认；工程容差不构成经济批准。

F01—F08 完整业务路径 NOT_RUN（依赖后续阶段）；本轮仅验证适用的 S1 局部计算、时间因果、错误与交付边界。S6.4 最终配置复验、最终测试及派生隔离、固定与滚动模型规则保留，不能用最终测试补做开发复验。

本地 hook 和草稿 PR 不等于服务器保护；远端状态仅依据实际附带快照，不从本地配置推断。历史合并事实保留，本轮交付不执行新合并或回退、不启用自动合并、不开始 S2 新任务；既有 S2 草稿与证据独立保留。后续必须取得准确新 SHA 的独立复审结论与负责人新授权。代码改变需要新提交、新证据及受影响项目复验。
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--output-dir", default=Path("deliverables/stages/S1"), type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("Source directory must be the real Git checkout root, not an ancestor repository")
    if Path(s1_contracts.__file__).resolve() != root / "src/bbag/s1_contracts.py":
        raise ValueError("S1 contracts must be imported from this exact source checkout")
    commit = git(root, "rev-parse", "--verify", f"{args.commit}^{{commit}}")
    if git(root, "rev-parse", "HEAD") != commit or git(root, "status", "--porcelain"):
        raise ValueError("Check out the exact S1 implementation with an unchanged clean working tree")
    evidence = args.evidence_dir.resolve()
    base = args.output_dir.resolve()
    output = base / commit
    archive_path = base / f"BBAG_S1_GPT_Work_{commit[:12]}.zip"
    validate_package_paths(evidence, output, archive_path)
    verification, receipt, counts = validate_evidence(evidence, commit, root)
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source.zip"
    subprocess.run(["git", "archive", "--format=zip", f"--output={source}", commit], cwd=root, check=True)
    shutil.copytree(evidence, output / "evidence")
    # Recheck the copied payload; a changed input must not become a ready archive.
    validate_evidence(output / "evidence", commit, root)
    (output / "SOURCE_COMMIT.txt").write_text(commit + "\n", encoding="ascii")
    write_json(output / "review_request.json", receipt)
    (output / "DEVELOPMENT_REPORT.md").write_text(development_report(root, evidence, commit, verification, counts), encoding="utf-8")
    audit = f"""# BBAG S1 独立审核请求

准确 S1 SHA：`{commit}`。范围仅 S1，当前独立审核 PENDING。S0 被审 `{S0_REVIEWED_SHA}`、合并 `{S0_MERGE_SHA}` 仅作为前置，不是本次批准。

核对 manifest、source.zip 与 Git 提交、锁依赖、样例及原始证据，按 REPRODUCE.md 在独立副本复验。自行构造语法/字段/类型/窗口/复杂度/参数/回看、缺失/常数/不足窗口/除零/非有限、重复 JSON、日期/可用时点和来源/输出边界反例；使用独立手算或参考值，不只重复生产函数。

验证未来数据扰动不改变过去因子、截断和完整输入在相同可用时点一致；批量与逐期在登记容差内一致。分开验证未来标签：入场严格在信号后、退出按会话规则、仅成熟后披露值；未来价格可合法改变未来收益标签。检查价格收益与真实含公司行动总收益的边界。

核查 S1 专用回执及验证契约、十项检查、真实 JUnit、规范配置/结果字节、哈希、准确 SHA 与 clean 快照、固定提交的手算/有理数标准答案。检查 evidence 链接、越界/重叠、已有输出防覆盖及可选历史目录的说明准确。

逐项记录 EXECUTED/STATIC_ONLY/NOT_RUN、输入/预期/实际/退出码和原始证据。工程结论由你实测判定，不预设通过；平台和经济 NOT_APPLICABLE。保留 S6.4、最终派生隔离与固定/滚动模型规则；不调用模型或量化平台，不运行完整 F01—F08，不合并、不开始 S2。

在审核副本新增测试和报告，生产修复交回 Codex。生成绑定 `{commit}` 的 S1 review_receipt.json；一致性检查不认证身份，也不自动授予 Git 或阶段权限。
"""
    (output / "GPT_WORK_AUDIT_REQUEST.md").write_text(audit, encoding="utf-8")
    template = f"""# BBAG S1 独立审核结果模板

被审 SHA：`{commit}`。本模板未执行，全部结论由独立审核者填写。

环境／工具／输入版本：待填写。

| 验收项 | 输入与方法 | EXECUTED / STATIC_ONLY / NOT_RUN | 预期 | 实际／退出码 | 原始证据 |
|---|---|---|---|---|---|
| 待填写 | 待填写 | NOT_RUN | 待填写 | 未验证 | 待填写 |

| 缺陷编号 | 严重度 | 复现 | 影响 | 修复／复测版本 |
|---|---|---|---|---|
| 待填写 | 待填写 | 待填写 | 待填写 | 未复测 |

工程：INCOMPLETE（待审核）；平台：NOT_APPLICABLE；经济：NOT_APPLICABLE；经济评估执行有效性：不适用。
审核决定、阻断项、未验证项、来源确认、允许动作及依据：全部待填写。

S1 通过也不自动合并或开始 S2；S6.4 复验、最终测试隔离与固定/滚动模型规则仍适用。
"""
    (output / "AUDIT_RESULT_TEMPLATE.md").write_text(template, encoding="utf-8")
    reproduce = f"""# 复现准确 S1 提交

```powershell
git clone https://github.com/wwooyy429/BBAG.git BBAG-S1-review
Set-Location BBAG-S1-review
git checkout --detach {commit}
py -3.13 -m venv .venv
.\\.venv\\Scripts\\python.exe -m pip install -r requirements.lock
.\\.venv\\Scripts\\python.exe -m pip install --no-deps --no-build-isolation -e .
.\\.venv\\Scripts\\python.exe scripts/verify_stage1.py --output-dir artifacts/independent-s1
.\\.venv\\Scripts\\python.exe scripts/build_stage1_package.py --commit {commit} --evidence-dir artifacts/independent-s1 --output-dir deliverables/independent-s1
```

Linux/macOS 使用 python3、.venv/bin/python。输出必须全新，不覆盖任何证据。所有数据均为合成 ENGINEERING_TEST/DRAFT，手算/有理数参考在 tests/fixtures/s1_expected.json。

source.zip 可用于无 Git 的源码阅读与普通 pytest/CLI 检查，但验证采集器和打包器要求真实、准确且干净的 Git checkout；不得在外层仓库内的解压目录冒充来源。开发日志和重放不代替独立验收，不访问封存最终测试数据。
"""
    (output / "REPRODUCE.md").write_text(reproduce, encoding="utf-8")
    if git(root, "rev-parse", "HEAD") != commit or git(root, "status", "--porcelain"):
        raise ValueError("Source changed during S1 packaging; retain evidence and use a fresh verified SHA")
    entries = [{"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size, "sha256": digest(path)}
               for path in sorted(output.rglob("*")) if path.is_file()]
    write_json(output / "manifest.json", {
        "stage": "S1", "implementation_commit": commit, "s0_reviewed_commit": S0_REVIEWED_SHA,
        "s0_merge_commit": S0_MERGE_SHA, "repository": "https://github.com/wwooyy429/BBAG",
        "generated_after_commit": True, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "developer_checks_passed": True, "independent_review": "PENDING",
        "merged_into_main": False, "next_stage_authorized": False,
        "source_archive_sha256": digest(source), "files": entries,
        "validation_scope": "S1 synthetic engineering and source-bound reference checks; no independent acceptance, platform or economics",
    })
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output).as_posix())
    print(json.dumps({"implementation_commit": commit, "report_directory": str(output),
                      "package": str(archive_path), "package_sha256": digest(archive_path),
                      "independent_review": "PENDING"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, subprocess.SubprocessError) as exc:
        print(json.dumps({"stage": "S1", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
