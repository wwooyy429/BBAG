# S2 数据边界、云端检查与持久任务

云端部署时将仓库 [cloud/main.py](../cloud/main.py) 映射为项目main.py，将src/bbag/*.py映射为bbag/*.py，保留cloud/s2_probe.py路径。入口只承载已授权日期，不添加证券或交易规则；实际数据检查从Research调用探针。上传文件须与准确提交逐字节核对，部署入口也属于固定源码，不能只在外部临时生成。

## 状态与前置

S2 当前为 **IN_PROGRESS**，独立审核 **PENDING**。负责人已接受准确 S1 提交 `c56db883cd7841c94517ee9817d7ad551e811dc2`，授权合并并开始 S2；S1 实际合并结果为 `41789706f710e131d4a654e349798ddb15ea70f5`，详见 [S1 验收记录](S1_ACCEPTANCE.md)。S1 批准不覆盖本次新增代码，也不是 S2 平台通过。

当前实现首先交付标准数据与访问契约、合成数据链路、持久任务台账，以及有限的 QuantConnect Cloud 检查入口。实现、开发自测、真实小样本执行和独立验收分别记录，不能将首次子里程碑交接写成整阶段已完成。S2 原路线的真实非空公司行动、更名、退市和历史股票池重建仍需相应证据；现有小样本白名单不覆盖全部退出条件。真实数据到后续 S1 计算及 S3 评估的集成不在当前探针中实现，也不因此宣称后续阶段完成。

本规范不预填尚未发生的云端执行结果。实际项目编号、调用、尝试次数、源码上传核对、返回元数据、费用可知性和结论在提交后绑定准确 SHA 的证据中登记。没有真实证据的适用平台项保持 NOT_RUN，模拟 SDK、导入日志和开发回归不能登记为平台 PASS；S2 独立批准另由准确版本的审核回执确认。

## 本次工程授权

负责人仅批准以下真实小样本工程范围；这不是 alpha 研究参数、最终测试选择或经济批准。

| 项目 | 边界 |
|---|---|
| 市场日期 | America/New_York 的 2020-01-02 至 2020-01-10，首尾包含；其余日期拒绝 |
| 资产 | SPY、AAPL；其余资产拒绝 |
| 行情口径 | 日频、RAW、OHLCV；不填充、不含盘前盘后 |
| 编译尝试 | 最多 2 次 |
| Research 检查尝试 | 最多 3 次 |
| 新增费用 | 上限 0；不购买、升级或启动付费选项 |
| 用途 | ENGINEERING_TEST，不运行因子经济评估、回测、优化、部署或 S3 |

[云范围配置](../configs/s2.cloud-scope.json) 记录该授权的工程副本；其中批准时间是开发会话收到并登记授权的时刻，不是外部签名认证。配置文件不自行产生运行时权限。实际账单没有返回时费用保持未知，不能把“未观察到账单”或零费用预留写成实际消费 0。

## 数据留在 Cloud

本阶段按 QuantConnect 官方 [Security and IP 中的 Data Use 说明](https://www.quantconnect.com/docs/v2/cloud-platform/security-and-ip) 和 [Terms 第 2.6 节](https://www.quantconnect.com/terms) 执行云数据边界。云许可行情及可重建这些行情的派生内容保留在 QuantConnect Cloud，不经 MCP 输出、Notebook 显示、日志、文件下载或本地交付包导出。本工程没有取得额外的数据导出权，也不把机构派生数据例外推定为当前组织已有权限。

本地只接收固定白名单中的检查布尔值、任务状态、封闭错误类别，以及已知请求、源码、配置的绑定元数据。不接收价格、成交量、SID 明细、事件时间序列、图、任意 stdout/stderr、市场数据摘要或原始行情哈希。源码、配置与安全控制元数据的哈希可以用于绑定；市场数据哈希不是控制输出字段。

本地标准数据样例均明确为 SYNTHETIC。真实云端探针直接在 Cloud 内检查 SDK 返回的 `TradeBar`，当前不会生成或导出真实 `StandardDataBundle`，也没有在真实行情上执行 S1 因子、标签或 S3 评估。

## 访问契约与运行时门禁

[s2_access.py](../src/bbag/s2_access.py) 提供 `DataRequest`、`AccessPolicy` 和 `AccessGuard`。请求显式携带 provider、闭区间日期、资产、字段、aware `as_of`、用途、分类、祖先分类及输出模式。provider 为 SYNTHETIC 或 QUANTCONNECT_CLOUD；输出模式为 LOCAL_DATA、CONTROL_ONLY 或 IN_CLOUD_DATA。

`AccessGuard.authorize(request, operation)` 在提供者调用、缓存读取/写入和数据处理之前重新验证输入。范围、资产、字段、用途、`as_of`、重复项及未知分类均受检查；SEALED_FINAL、SEALED_DERIVATIVE 及受封存来源污染的派生输入不能进入开发流程。`authorize_artifact` 使用工件的实际分类与祖先再次检查，不能仅改查询标签来洗掉来源。`authorize_batch` 先检查全批，调用者才可开始第一项 I/O。

SYNTHETIC 工程策略只授予合成测试。OWNER_AUTHORIZED 策略除批准元数据外，还必须使 `canonical_policy_sha256(policy)` 精确进入应用代码提供的 `trusted_policy_sha256s` 集合；读取 JSON、验证配置或生成计划均不会登记该集合。更改策略内容使原 digest 授权失配。

CONTROL_ONLY 仅允许 cloud_dispatch、cache_read、cache_write，不能转成数据 query、quality 或 export。本地 QUANTCONNECT_CLOUD 请求即使日期和授权正确，也不能使用 LOCAL_DATA；Cloud 数据处理需要 Cloud 执行位置及 IN_CLOUD_DATA。云探针只在完成授权后创建新的云内请求再校验，原控制请求不原地改写。

这是应用入口的运行时控制，依赖应用、操作者和提供者权限共同遵守。Python 模型和配置不是操作系统沙箱，也没有建立 QuantConnect 服务端 ACL。持有管理员工具的操作者若绕过受控入口直接查询，应用门禁无法拦截；不能把本地规则或草稿 PR 描述成平台侧强制权限。

## 标准数据与合成链路

[s2_data.py](../src/bbag/s2_data.py) 使用 `StandardDataBundle` 绑定原请求、provider 版本、原字节来源、请求哈希、取得时刻、分类、显式日历与时区。原始开高低收是未复权成交价格，volume 是未复权股数；拆股和分红另列事件，不将 raw 价格变化称为含公司行动的总收益。

| 接口/模型 | 实际语义 |
|---|---|
| `CalendarSession` / `DailyBar` | 使用显式会话，不按工作日补齐；唯一 session/SID 键；缺值为 null；可用时点不早于收盘，也不晚于请求及取得截止 |
| `SecurityIdentity` / `TickerMapping` | SID 与 ticker 分开；上市、退市和更名边界有独立已知时刻；ticker 有效期首尾包含，退市日为上市区间的排除端 |
| `MembershipEvent` | 按每个会话信号时刻已知且已生效的成员事件重建，结合身份及 ticker 知识边界，不用末期幸存名单回填 |
| `historical_universe` / `LiquidityRule` | 可选流动性筛选使用前 N 个完整会话的 raw close×volume 均值，固定滞后 1 期；缺任一输入则排除，按分数降序及 SID 升序稳定排序 |
| `action_entitlement` | 已知且生效的单事件持仓权益：拆股改变股数，分红按原股数计算现金；不生成复权价格或总收益系列 |
| `data_quality` | 在时点成员集上报告覆盖、字段缺失、迟到/陈旧、零量及 raw close 跳变，附已知公司行动上下文；raw 跳变不是经济信号 |
| `standardize_daily_json` | 先门禁、再解析有界原字节；拒绝重复 JSON 键、非有限数、额外字段及不一致时间/身份；数值按 raw 口径绑定，不静默复权 |
| `to_s1_panel` | 仅允许 SYNTHETIC 投影，保持 S1 的 synthetic_fixture 来源；拒绝把真实 Cloud 数据标成合成面板 |

标准数据函数检查选择请求属于原请求的子集、原请求本身仍获授权，并检查实际工件来源。云内 raw 模型的存在不提供本地 Cloud 行情导入入口。真实行情到 S1 的后续集成及 S3 必须另行实现和验证，不能用当前合成投影冒充。

[合成配置](../configs/s2.example.json)、[原始合成输入](../tests/fixtures/s2_raw.json) 与 [手列标准答案](../tests/fixtures/s2_expected.json) 是 ENGINEERING_TEST/DRAFT。夹具包含身份/成员变化及滞后流动性排序、raw 拆股跳变、覆盖与单事件权益例子；例如 10 股经 2:1 拆股为 20 股，10 股按每股 1 的分红取得现金 10。标准答案由字面手算给出，不能据此声明真实公司行动或真实历史股票池验证完成。

## 持久任务与恢复

[s2_tasks.py](../src/bbag/s2_tasks.py) 将 `TaskSpec`、配置身份、尝试和安全元数据保存到 SQLite 与不可覆盖的元数据文件。请求身份包含 operation、project、源码、配置、请求哈希及响应类别；不同配置数与实际尝试数分开记账。缓存复用应在预留新尝试之前检查，不能把复用算作新的提供者执行。

`TaskLedger.begin_attempt` 先持久保存 UNKNOWN 意图和费用预留，再允许调用 transport。`submit` 对相同请求/关联重复调用复用已有记录，不自动重复调提供者。accepted、compiling、compiled、running、run_finished、result_available、completed、error、incomplete 和 UNKNOWN 分开；执行成功与检查布尔结论也分开。编译完成、收到任务或返回检查结果，不等于独立验收或经济批准。

异步任务没有真实 `platform_task_id`，也没有实际观察到的 invocation/cell 组合时，普通 `record_event` 即使收到 completed 声明也保持有效状态 UNKNOWN，并保留 reported_status。`normalize_qc_check_response` 不制造 ID；没有账单时 `actual_cost_microusd` 保持 null。异常只保存封闭类别，不保存可能含行情的错误原文。JSON 导入状态声明和 callback 返回都不认证平台来源。

同步工具确实返回、但没有提供任务 ID 时，应用可以显式调用 `record_synchronous_result`。`SynchronousCompletionRecord` 绑定当前本地预留尝试、观察时刻、精确请求/源码/配置及安全 `TaskResponse` 元数据哈希；不合成提供者 ID。该记录是调用应用对新鲜同步返回的显式断言，构造 JSON 本身不能证明观察发生，也不认证调用者或平台。导入旧日志不能自动走此路径；该断言不增加访问权限、不推断零账单、不触发提交或重试。

UNKNOWN 未核实前禁止盲目重提。`reconcile_not_submitted` 要求与同次尝试绑定的显式未提交核查记录；完成该核查也只是恢复条件，后续尝试仍需显式 retry、剩余预算及访问授权。重启、重复完成消息、哈希绑定、元数据文件和数据库完整性均按实际记录检查。LOCAL_NORMALIZED_METADATA 明示由本地模型规范化生成，不能改称平台原始响应；CALLER_METADATA_BYTES 也仅表示调用者提供的安全元数据字节。

## QuantConnect 控制网关与云探针

[s2_quantconnect.py](../src/bbag/s2_quantconnect.py) 不含凭据或内建远程客户端。`QuantConnectCapabilities` 只是声明的工具/项目能力，不认证项目可用性；实际项目清单和权限须通过真实连接器核实。`QuantConnectCheckRequest` 仅支持 research_check、daily、raw、cloud_only、verdicts_only 和 CONTROL_ONLY，工程上限为 4 个资产、32 个日历日期、128 条历史行；这些通用上限不会放宽本次更窄的负责人白名单。

`canonical_qc_request` 与 `qc_request_sha256` 绑定完整计划；`dispatch` 在立即重查门禁后调用一次注入 callback，不读缓存，供持久台账预留的实际尝试使用。`cached_result` 先重查权限，只复用完整匹配请求的检查元数据，不能用日期前缀或跨版本命中。`check` 是可复用缓存的便利入口，不能在已预留实际执行后隐藏一次缓存命中。`accept_metadata` 只验证导入声明，不写 callback 缓存；`release_metadata` 重新授权并验证固定字段后释放控制对象。

响应只包含项目、operation、绑定 hash、status、封闭 error_category 及 `check_results`。字段之外的原始行情、日志或图形即使出现在 callback 中也会拒绝，错误不回显该内容；重复 JSON 键、非有限数、非布尔 verdict、其它请求的绑定或不一致 check 集合同样拒绝。网关结果始终注明 NOT_AUTHENTICATED、未认证来源及无市场数据导出；自身 platform_integration 保持 NOT_RUN，外部真实执行证据与独立平台结论另行登记。

[cloud/s2_probe.py](../cloud/s2_probe.py) 的 `run_cloud_check(plan, policy, trusted_policy_sha256)` 用于已授权的小样本。完整 preflight 在 SDK 导入、QuantBook 创建、订阅和 history 之前；内部云请求通过同一授权再校验。使用 RAW 日频、fill_forward=False、extended_market_hours=False、daily_precise_end_time=True。typed `history[TradeBar]` 使用显式日期范围：2020-01-02 零时至 2020-01-11 零时的排除端，以纳入 1 月 10 日，不授予 1 月 11 日数据。不会使用可能跨界的条数或相对跨度重载。

Research 上下文时钟与这段查询范围分开设置。按官方 [QuantBook 初始化说明](https://www.quantconnect.com/docs/v2/research-environment/initialization)，`set_start_date` 在 Research 中限制 history 可返回数据的最晚日期，不能把它当成查询起始日。本探针在添加股票前将 QuantBook 时钟设为查询排除端 2020-01-11，并把其上下文 end_date 设为更晚的 2020-01-12；这两个上下文设置不发起额外日期的 history 查询，实际请求仍只有上述白名单区间。若把时钟错误地放在 1 月 2 日零时，合法查询会被截成空结果。修复移动上下文时钟，保留查询参数和 data_presence 的真实判定，不把空数据改写为通过。模拟 SDK 回归也按该时钟截断历史，检查旧行为为空、修复后合成数据可见的区别。

| 固定检查名 | 限定含义 |
|---|---|
| data_presence | 每个请求身份至少出现一条 bar；不是全日期覆盖率证明 |
| requested_bounds | 实际返回的身份和会话在请求边界内 |
| field_availability | 返回 OHLCV 有限、价格为正、成交量非负且 OHLC 物理关系成立；不是实际供应商历史披露时点证明 |
| normalization_raw | 显式 RAW 请求及订阅配置一致；不据此替代公司行动调整比对 |
| point_in_time | bar 区间与交易所开收盘、截止时刻、无填充及逐 SID 严格时序一致；不证明整个历史数据的发布时间、修订或幸存者偏差已解决 |
| identity_consistency | 此次订阅内部 SID 唯一，返回身份与请求对应；不证明非空更名/退市事件链 |
| no_data_export | 此探针仅释放固定控制对象，行情及内部标识不进入返回值；不是对整个平台或管理员工具的安全认证 |

Python SDK 的意外 stdout/stderr 在探针执行区丢弃，不作为 Notebook 数据发布。有效请求的拒绝和执行异常返回有限错误类别；无法绑定项目/源码的非法计划只抛 `CloudProbeRejected`，消息仅为 INVALID_REQUEST，调用 cell 须捕获并仅发布该常量类别，不输出 traceback 或异常 repr。探针不返回计数、价格、时间序列、数据哈希或 DataFrame。

当前 71 项网关检查和 40 项云探针模拟 SDK 检查属于开发自测的实际记录，不是独立审核或真实平台 PASS，也不是最终全库计数。它们验证了门禁先于 callback/缓存、字段拒绝、来源声明、异常脱敏、Research 时钟截断及模拟 SDK 调用参数。真实 Cloud 小样本必须在固定提交后另行执行并保留准确源码与安全元数据证据；非空拆股、分红、更名和退市案例若在白名单外，保持 NOT_RUN，不能为补齐覆盖而扩大查询。

## 本地复现与提交后交接

以下本地命令不调用远程平台，不登记真实运行时授权。

```powershell
.\.venv\Scripts\python.exe -m bbag s2-validate --config configs/s2.example.json
.\.venv\Scripts\python.exe -m bbag s2-validate --config configs/s2.cloud-scope.json
.\.venv\Scripts\python.exe -m bbag s2-run-synthetic --config configs/s2.example.json --output-dir artifacts/s2-demo --run-id synthetic
.\.venv\Scripts\python.exe scripts/check_stage2_results.py --run artifacts/s2-demo/synthetic
.\.venv\Scripts\python.exe -m bbag schemas --stage S2 --output-dir contracts/s2 --check
```

在核实真实项目编号后，`s2-cloud-plan --config configs/s2.cloud-scope.json --project-id <实际编号>` 仅生成计划，仍返回 runtime_grant_enrolled=False、platform_execution=NOT_RUN。它不上传、不查询、不登记任务，也不核实已部署源码。真实执行须核对干净准确提交、实际上传源码、请求和应用外的授权登记。

提交后使用 `scripts/verify_stage2.py --output-dir <全新目录>` 采集固定十项本地证据，再以 `scripts/build_stage2_package.py --commit <完整SHA> --evidence-dir <证据目录>` 生成该 SHA 的源码、锁依赖、合成样例、原始输出、JUnit、复现命令、文件哈希、待审请求与空模板。正常采集和打包不是独立批准；生成物放在 tracked source 之外，不覆盖旧审核、失败或平台调用记录。平台证据仅包含许可允许的控制元数据，不带云行情。

正式研究参数、真实数据扩展与完整性能/资源门槛仍待适用阶段预登记，已声明的输入上限不是性能承诺。当前阶段不运行完整 F01—F08，不进入 S3；S6.4 最终配置复验、最终测试及派生信息隔离、固定/滚动模型规则继续保留。代码或协议改变后，相关旧结论须评估失效范围，以新 SHA 及新证据复验，不自动合并或推进阶段。
