"""Freeze the exact readable Cloud source closure; never upload or compile."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from s2_plan_source import check_output, check_source

# Deliberate module closure; adding a local import requires this list to change
# and be reviewed. The planner independently verifies every declared AST edge.
INIT = "src/bbag/__init__.py"
DEPENDENCIES = {
    "cloud/main.py": [],
    INIT: [],
    "src/bbag/jsonio.py": [INIT],
    "src/bbag/contracts.py": [INIT, "src/bbag/jsonio.py"],
    "src/bbag/s1_contracts.py": [INIT, "src/bbag/contracts.py", "src/bbag/jsonio.py"],
    "src/bbag/s2_access.py": [INIT, "src/bbag/contracts.py", "src/bbag/s1_contracts.py"],
    "src/bbag/s2_data.py": [INIT, "src/bbag/contracts.py", "src/bbag/s1_contracts.py", "src/bbag/s2_access.py"],
    "src/bbag/s2_cloud_data.py": [INIT, "src/bbag/contracts.py", "src/bbag/jsonio.py", "src/bbag/s1_contracts.py", "src/bbag/s2_access.py", "src/bbag/s2_data.py"],
    "src/bbag/s2_cloud_check.py": [INIT, "src/bbag/contracts.py", "src/bbag/s2_access.py", "src/bbag/s2_cloud_data.py", "src/bbag/s2_data.py"],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-file-bytes", required=True, type=int)
    parser.add_argument("--max-total-bytes", required=True, type=int)
    parser.add_argument("--extra-compile-calls", default=0, type=int)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    check_source(root, args.commit)
    from bbag.s2_upload import UploadSourceSpec, build_upload_manifest, canonical_upload_manifest_bytes, read_upload_sources
    specs = [UploadSourceSpec(source_path=path,
        remote_path="main.py" if path == "cloud/main.py" else path.removeprefix("src/"),
        dependencies=dependencies) for path, dependencies in DEPENDENCIES.items()]
    manifest = build_upload_manifest(root, args.commit, specs,
        ["cloud/main.py", "src/bbag/s2_cloud_check.py"], max_file_bytes=args.max_file_bytes,
        max_total_bytes=args.max_total_bytes, extra_compile_calls=args.extra_compile_calls)
    payloads = read_upload_sources(root, manifest)
    check_source(root, args.commit)
    # All validation precedes output creation; an existing delivery is immutable.
    output = check_output(root, args.output_dir, protected=(root / "artifacts/s2-bootstrap",))
    output.mkdir(parents=True, exist_ok=False)
    (output / "UPLOAD_MANIFEST.json").write_bytes(canonical_upload_manifest_bytes(manifest) + b"\n")
    for remote, payload in payloads.items():
        target = output / "payload" / remote
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    table = "\n".join(f"| {file.order} | `{file.source_path}` | `{file.remote_path}` | {file.size_bytes} | `{file.source_sha256}` | 1 |" for file in manifest.files)
    (output / "UPLOAD_PLAN.md").write_text(f"""# S2 准确上传清单（离线草稿）

SHA：`{manifest.source_commit}`。远端未查询；FULL_FRESH 包含入口的完整可读源码闭包。上传前须另行核实目标项目、现有同名文件、资源和外部依赖，不能据旧观察跳过文件。

| 顺序 | 仓库文件 | 云端文件 | 字节 | SHA-256 | 写入编译 |
|---|---|---|---:|---|---:|
{table}

逐文件 create_file/edit_file 连接器调用均自动编译一次，失败或 UNKNOWN 也计数。m={manifest.write_call_count}，额外明确编译 e={manifest.extra_compile_calls}，本清单所需编译增量为 **{manifest.required_compile_increment}**（m+e）。如未来授权该增量，累计编译上限提案为 {manifest.proposed_compile_total_cap}；这只是提案。

当前累计已用/上限仍为 **2/2**，可用编译 **0**，本轮授权增量 **0**。未上传、未编译、未登记 grant 或新真实任务；Research 仍已用 2/3，最后一次保留，可用新增 Research 为 0。输入的文件大小限制是工程防护，不证明平台配额或可用节点。依赖 pydantic/tzdata 及 SDK 的实际兼容性尚未平台验证；不把 requirements.lock 上传当作安装依赖。

payload 只包含自有可读源码，无行情、SID 表、缓存、Notebook 搬运代码或预算账本。既有 cloud/s2_probe.py、bbag/s2_quantconnect.py 和历史研究文件不在此闭包内，不擅自删除或替换；未来调用须显式使用 bbag.s2_cloud_check 入口并核对源码绑定。每项许可数据及其内部摘要留云内，只返回固定控制回执。实际 SDK 查询工厂、身份知识证据、缓存持久化、恢复与资源/费用证明仍须补齐，不能据本清单宣称整 S2 平台通过。
""", encoding="utf-8")
    print(json.dumps({"source_commit": manifest.source_commit, "files": manifest.write_call_count,
        "required_compile_increment": manifest.required_compile_increment,
        "authorized_compile_increment": 0, "platform_execution": "NOT_RUN",
        "output_dir": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "platform_execution": "NOT_RUN"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
