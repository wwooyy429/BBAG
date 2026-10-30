# BBAG 量化因子研究系统

BBAG 的完整目标是由 GPT 提出经济假设，经 QuantConnect Research 和 LEAN 验证，再由独立评估程序及 GPT Work 审核。当前开发范围为 **S0 工程骨架、研究规范与标准接口**；不包含因子计算、平台接入、模型调用、回测或部署。

本分支的交付状态以绑定 commit SHA 的开发报告和真实验证日志为准。独立 GPT Work 审核在取得实际结果前保持 **PENDING**；开发自测和协作代理检查不能替代该审核。审核未通过前不合并 main，不进入 S1。

## 本地运行

要求 Python 3.11 或更高版本。依赖使用仓库中的 requirements.lock；下列 Windows 示例在项目根目录执行。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe -m bbag validate --config configs/s0.example.json
.\.venv\Scripts\python.exe -m bbag init-experiment --config configs/s0.example.json --output-dir artifacts/experiments --run-id s0-demo
.\.venv\Scripts\python.exe -m bbag schemas --output-dir contracts
.\.venv\Scripts\python.exe -m bbag schemas --output-dir contracts --check
.\.venv\Scripts\python.exe -m pytest --junitxml artifacts/verification/junit.xml
```

Linux 或 macOS 可用 `python3 -m venv .venv` 建立环境，并将后续解释器路径替换为 `.venv/bin/python`。重复创建同一实验编号会被拒绝，应使用新的编号或保留原实验后另建输出目录。

样例协议是 `ENGINEERING_TEST` 与 `DRAFT`，仅用于校验接口和实验记录。配置校验成功不表示研究参数已经批准、数据权限已经落实或因子具有收益。合成样例不触发任何外部模型或平台调用。

## 开发文档

- [阶段 0 任务与验收](docs/STAGE0.md)
- [研究协议与待确定参数](docs/RESEARCH_PROTOCOL.md)
- [Git 与独立审核流程](docs/GIT_WORKFLOW.md)
- [标准接口说明](contracts/README.md)
- [完整开发路线与各阶段目标](docs/ROADMAP.md)
- [阶段验收与交接规范](docs/ACCEPTANCE.md)
- [完整业务路径用例](docs/FLOW_ACCEPTANCE.md)

## 开发阶段

| 阶段 | 开发目标 | 达成后的能力 |
|---|---|---|
| S0 | 固定研究协议并建立工程骨架 | 输入、输出、时间边界、研究预算和验收规则有统一定义 |
| S1 | 实现确定性因子计算与审计 | 手写因子和标签能够正确计算，非法表达式与未来数据访问得到处理 |
| S2 | 接入时点数据和历史股票池 | QuantConnect 数据进入统一接口，历史成员、证券身份与数据可用时间可追溯 |
| S3 | 实现单因子研究评估 | 固定基准因子生成可复算的预测、分层、暴露与稳定性报告 |
| S4 | 实现 LEAN 可交易回测 | 数据、因子、信号、订单、持仓、成本和净值形成完整链路 |
| S5 | 接入 GPT 假设生成和反馈 | 有限候选经过真实评估，成功与失败进入记忆并影响下一轮 |
| S6 | 实现受限搜索与组合研究 | 在统一预算内完成优化，最终配置经开发复验才可冻结 |
| S7 | 实现冻结测试和前向运行 | 独立判定晋升或归档，区分工程演练与候选经济验证 |

## 主要里程碑

1. **S4 基础闭环**：一条人工定义因子在真实数据上完成 Research 到 LEAN 的验证。
2. **S5 首版自动研究**：GPT 完成至少一轮候选生成、审计、评估、归档与反馈。
3. **S6 完整挖掘能力**：自动研究具备预算、去重、组合增量和停止规则。
4. **S7 独立验证与维护能力**：冻结流程完成独立评估，并能开展前向模拟和持续监控。

## 当前状态

本次只实现和验证 S0。F01—F08 描述后续完整系统的业务路径，S0 仅落实与契约、版本和审核状态有关的基础要求，不能将这些路径全部记为已执行。

每次开发完成后，先提交实现，再按该 SHA 在 `deliverables/` 生成开发报告、审核任务和固定源码包。输出材料不进入实现提交，避免报告包含自身 commit SHA 的循环。审核包须以本次源码和规范为准；`docs/handoff/` 保留的是历史规划交接材料，不能作为当前实现的审核证据。

main 仅用于通过独立审核后的版本。草稿 PR 和本地推送钩子辅助执行该流程；它们不等于 GitHub 服务器分支保护。服务器保护是否启用须单独核实，不能仅凭本项目文档认定。
