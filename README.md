# BBAG 量化因子研究系统

BBAG 的完整目标是由 GPT 提出经济假设，经 QuantConnect Research 和 LEAN 验证，再由独立评估程序及审核者复验。S0 已经负责人验收并合并，当前范围为 **S1 确定性因子计算与公式审计**，使用合成输入与独立未来价格标签；平台接入、模型调用、研究评估、回测及部署属于后续阶段。

本分支的交付状态以绑定 commit SHA 的开发报告和真实验证日志为准。原 S1 提交 `f5c07d2b878c3b7e9f122bcf064ef45589d9aaa7` 独立审核为 **CHANGES_REQUIRED / 工程 FAIL**，报价字段与开盘事件不一致的 S1-001 修复见 [修复说明](docs/S1_REMEDIATION.md)。新修复 SHA 须另行独立复审，结论保持 **PENDING**；开发自测和协作代理检查不能替代该审核。S1 未独立通过并获负责人授权前不合并 main，不进入 S2。

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
- [S0 验收及实际合并记录](docs/S0_ACCEPTANCE.md)
- [S1 算子、时间、标签与交付规范](docs/STAGE1.md)
- [S0 补充材料核对与修补](docs/S0_SUPPLEMENT_CHECK.md)
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

S0 被审 SHA 为 f46fad2ff4b3ec9659001659fa44201e10951fd5，合并结果为 b2555efe7201c473bdb66c7dd1c14552ad130ac5。S1 在独立开发分支交审，不继承 S0 批准。F01—F08 描述完整系统路径，当前仅验证适用的工程、计算与因果边界，不能将完整路径记为已执行。

每次开发完成后，先提交实现，再按该 SHA 在 `deliverables/` 生成开发报告、审核任务和固定源码包。输出材料不进入实现提交，避免报告包含自身 commit SHA 的循环。审核包须以本次源码和规范为准；`docs/handoff/` 保留的是历史规划交接材料，不能作为当前实现的审核证据。

main 仅用于通过独立审核后的版本。草稿 PR 和本地推送钩子辅助执行该流程；它们不等于 GitHub 服务器分支保护。服务器保护是否启用须单独核实，不能仅凭本项目文档认定。
