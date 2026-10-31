# BBAG 量化因子研究系统

BBAG 的完整目标是由 GPT 提出经济假设，经 QuantConnect Research 和 LEAN 验证，再由独立评估程序及审核者复验。S0、S1 已经负责人验收并合并，当前授权范围为 **S2 平台适配与时点数据**；模型调用、alpha研究评估、策略回测、优化及部署属于后续阶段。

本分支的交付状态以绑定 commit SHA 的开发报告和真实验证日志为准。负责人已核实并接受 `259962860d2301ead894c5d649a29eab1f8db1ac` 的 S0/S1 独立批准，授权合并为 `7aac4886b50493c09235c49b5de90643b7090e07` 并恢复 S2。S0-004、S0-N01、S1-001、S1-002 关闭记录保留；旧失败记录不改写。S2 正在集成该基线，范围及未完成项见 [恢复记录](docs/S2_RESUMPTION.md)。S2 新实现独立审核保持 PENDING，不自动合并或进入 S3。

负责人已接受准确 `5fe1b62c09216e17f001afde80fbcdfda7c51bdc` 的独立本地工程 PASS，S2-001、S2-002（含多事件遗漏）、S2-003 均关闭。原 `93ca4116…` 失败及所有历史证据保留，见 [修复说明](docs/S2_AUDIT_REMEDIATION.md)。当前继续 S2 离线能力：冻结快照时钟、合成分批/不可覆盖缓存、历史身份/公司行动诊断及累计预算只读计划。新代码独立审核仍为 PENDING；整 S2 平台与真实数据退出条件未齐，不合并、不进入 S3。

S2 接口、白名单、许可与未验证边界见 [S2规范](docs/STAGE2.md)。本地使用合成数据；真实数据留在 QuantConnect Cloud，仅返回固定控制状态。原白名单及累计上限不变：2020-01-02 至 2020-01-10 的 SPY/AAPL，编译最多 2 次、Research 最多 3 次、零新增费用。已用编译 2/2、Research 2/3，剩余 0/1；不因本次集成或新台账重置。四次历史调用的实际账单未知，不冒称费用为零。本轮离线集成不新增平台调用。

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
- [S1 验收、锁修复与实际合并](docs/S1_ACCEPTANCE.md)
- [S2 恢复、累计预算与证据边界](docs/S2_RESUMPTION.md)
- [S2 独立审核三项阻断修复](docs/S2_AUDIT_REMEDIATION.md)
- [S2 离线续建、使用入口与平台验收缺口](docs/S2_OFFLINE_CONTINUATION.md)
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

最新已审基础提交为 `259962860d2301ead894c5d649a29eab1f8db1ac`，实际 main 合并结果为 `7aac4886b50493c09235c49b5de90643b7090e07`，两者源码树相同。S2 在开发分支恢复，当前首轮子里程碑仍为 IN_PROGRESS；旧 Cloud 检查绑定原 `2b4e1285`，不换写为新 SHA 平台执行。F01—F08 完整业务路径未执行，不能由局部工程检查代替。

每次开发完成后，先提交实现，再按该 SHA 在 `deliverables/` 生成开发报告、审核任务和固定源码包。输出材料不进入实现提交，避免报告包含自身 commit SHA 的循环。审核包须以本次源码和规范为准；`docs/handoff/` 保留的是历史规划交接材料，不能作为当前实现的审核证据。

main 仅用于通过独立审核后的版本。草稿 PR 和本地推送钩子辅助执行该流程；它们不等于 GitHub 服务器分支保护。服务器保护是否启用须单独核实，不能仅凭本项目文档认定。
