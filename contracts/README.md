# S0 契约与配置说明

`src/bbag/contracts.py` 使用 Pydantic 2 定义项目接口。全部契约拒绝额外键、赋值时校验、禁止非有限数值，并对整数、布尔及浮点输入启用严格类型校验。浮点字段允许整数数值，拒绝字符串和布尔冒充数值；零预算必须是整数 `0`，不接受 `false`、`0.0` 或 `"0"`。

`CONTRACT_MODELS` 导出七个命名主契约、ResearchProtocol、ReviewReceipt 及必要子契约，可供 CLI 导出 JSON Schema。Schema、配置校验和 `VALIDATED` 仅证明结构及已实现的一致性检查，不证明运行、权限隔离、经济有效或独立审核通过。

| 契约 | 内容与边界 |
|---|---|
| DataSpec | 合成来源、USA、daily、有效 IANA 时区、唯一非空字段、归一口径和股票池规则；真实平台接入属于 S2 |
| FactorSpec | 假设、符号公式、所需字段、回看期和方向；S0 只保存非空公式，不解析或执行算子 |
| LabelSpec | `next_session_open` 入场、`hold_h_sessions` 退出及正整数持有期、收益与基准定义 |
| ExecutionSpec | `after_data_available` 信号、下一交易时段开盘入场、持有期退出，独立调仓周期、风险及成本 |
| EvaluationSpec | 指标白名单、对应阈值、时间顺序验证窗口和最终结果连同派生内容隔离的声明 |
| ExperimentRecord | 关联协议哈希、源码提交或空值、脏工作区状态和带时区创建时刻；平台 NOT_RUN、经济 NOT_EVALUATED、审核 PENDING |
| ReleaseManifest | 提交、配置哈希、相对工件路径及内容哈希；审核始终 PENDING，不自动发布或晋升 |
| ResearchProtocol | 聚合协议，固定 S0、ENGINEERING_TEST、DRAFT；保留 pending_decisions，不批准研究 |
| ReviewReceipt | S0 审核回执的结构一致性检查，不验证审核者身份、不授权合并或进入 S1 |

## 时间、数值和跨契约校验

TimeRange 为含端点的日期区间，接收 `date` 或严格 `YYYY-MM-DD` 字符串，不接受布尔、数值或会丢失时刻的 `datetime`。要求 start ≤ end，且 development.end < final_test.start。

`DataSplitSpec.check_development_range(start, end)` 成功返回 None，越出开发区间则抛出 ValueError；非法日期与倒置区间触发 ValidationError。此方法仅校验一项拟议请求，不是 S2 运行时权限控制，也不访问数据。

所需因子字段必须包含于数据字段；入场及退出规则须与执行定义一致。字段、指标、待定项和工件路径等不允许重复。阈值键来自指标白名单且包含在 metrics 中：coverage 在 [0,1]，IC/RankIC 在 [-1,1]，turnover 非负，净超额收益阈值要求有限。最大单股权重大于零且不超过一，总敞口大于零；数值合法不等于风险已批准。

S0 的模型调用、平台任务、优化任务和支出预算强制为零。候选数量和本地运行秒数为正整数，不授予外部调用权限。时区通过 ZoneInfo 验证，Windows 环境需要安装 tzdata。

## 加载、序列化与来源记录

`load_protocol(path)` 从 UTF-8 JSON 加载 ResearchProtocol，拒绝重复 JSON 键和 NaN/Infinity 常量。`canonical_protocol_bytes(protocol)` 重新校验协议，再输出键排序、紧凑、保留中文、无非有限数值的 UTF-8 JSON，便于根 CLI 计算 SHA-256。

赋值校验不能拦截列表、字典的每次原地修改；使用前应重新校验。规范序列化入口会执行该复验。工件路径规范为 `/`，禁止绝对路径、盘符、上级目录、空或点组件及 Windows 歧义名称；这只是路径语法校验，文件存在性、内容哈希和链接解析由交付工具核验。

ReviewReceipt 的 S0 APPROVED 需要工程 PASS、平台 NOT_APPLICABLE、零阻断项、非空证据和带时区审核时刻。平台 PASS、FAIL 或 NOT_RUN 均不构成 S0 的有效批准状态。PENDING 和 CHANGES_REQUIRED 仍可读取上述原有平台枚举，始终不能通过批准检查。

此次修正只收紧 APPROVED 的跨字段语义校验，Schema 版本仍为 1.0，字段、枚举和默认值不变；原先被错误接受的矛盾批准现在被拒绝。回执默认 PENDING，S0 经济状态固定 NOT_APPLICABLE；普通合法 NOT_APPLICABLE 批准仍只产生 advisory 一致性结果，不能认证审核者身份、授权合并或进入下一阶段。

`configs/s0.example.json` 只演示合成配置。2015–2020 开发区和 2021–2022 最终区是日期校验样例，不取数；公式、费用、门槛、敞口和窗口均为 DRAFT，未决研究参数明确保存在 pending_decisions。本阶段不得据样例执行公式、平台任务、付费优化或真实交易。
