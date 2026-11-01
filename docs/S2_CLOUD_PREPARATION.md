# S2 云内标准化、SID 绑定与预算续接

基线为负责人已核实接受的准确 `e412c9d0a3ec9148bab7e1f60ce614d2fa1b1ad2`。该次独立复审关闭 S2-004；Windows 1586 通过、12 跳过，既有 228 安全项和 99 字节绑定项通过。这里只登记已提供的历史结论，不把它作为新实现的批准。新实现须按提交后生成的完整 SHA 复审；整阶段仍 PENDING，平台 NOT_RUN，经济 NOT_APPLICABLE。

本轮只实现并测试离线逻辑，不调用 QuantConnect，包括不查询项目、源文件、节点或账单。不增加额度，不创建新真实账本或任务，不消耗最后一次 Research，不合并 S2、不进入 S3。旧失败、已关闭问题和原云端证据全部保留。

## 原授权与数据边界

只允许 America/New_York 当地日期 2020-01-02 至 2020-01-10（两端包含）、SPY/AAPL、OHLCV、ENGINEERING_TEST。其余日期/资产禁止，包括回看窗口、为了找到公司行动而扩展范围。当前已记录的目标项目是 38698074；本轮有界桥接固定该编号，但不证明项目当前归属、权限、源文件或节点可用。

原累计上限是编译 2、Research 3、零新增费用。实际历史已用编译 2/2、Research 2/3；四笔实际账单均未知。保留 Research 1 后，当前新增任务可用量为编译 0、Research 0。源码提交、进程重启、新计划或新账本均不能恢复额度。

## 云内数据接口

[s2_cloud_data.py](../src/bbag/s2_cloud_data.py) 提供 `CloudDataPlan`、`CloudResolution`、`CloudDailySnapshot`、`build_cloud_bundle` 和 `extract_sdk_daily_snapshot`。它不包含默认网络客户端、自动 SDK 查询或元数据推断。离线测试注入明确标记为 SYNTHETIC 的身份、TradeBar 与回包；没有真实 Cloud 行情进入本地。

1. 在 resolver、fetcher、SDK history factory 的第一次调用之前，重新验证 typed plan、原 AccessGuard、外部可信策略摘要、Cloud 执行位置声明、原项目/日期/资产/字段、分类和输出模式。
2. resolver 返回准确 project/source/config/原 alias request/policy 的绑定，以及明确声明的 alias、SID、映射有效区间、身份、ticker 历史和知识证据。SID 必须区别于 SPY/AAPL 别名；重复、跨资产、未来知识、过期区间和错绑定拒绝。结构合规与 PROVIDER_DOCUMENTED 标签不认证其真实性。
3. `CloudMappingGuard` 保留原 owner policy，授权列表仍是 SPY/AAPL。内部 SID 子请求必须可逆映射至原 alias 子请求，再交回原 guard 授权。仅支持 query/quality；不能借映射执行 export、cache、batch 或 cloud_dispatch，也不制造 OWNER_AUTHORIZED SID policy。
4. fetcher 提供明确版本、日历会话、声明 RAW 的数据与取得时点。SDK 提取复制 native OHLCV，不调整、填充或推算价格；拒绝非有限、布尔数值、错 SID、重复/越界会话、错误开收盘时点及 SDKHistory 声明的非 raw/填充设置。内部组合摘要与解析的同一输入 image 绑定；结果可通过 `revalidate_cloud_result` 从捕获 image 重新验证，拒绝后续对象污染。没有默认真实 factory，不能据此证明实际 SDK 订阅配置。
5. 构造既有 `StandardDataBundle` 并保留原数据/身份/质量契约。原 alias request 和策略摘要存于内部映射上下文，SID request 不替换负责人原授权。持有此对象的应用必须留在 Cloud；execution_location 字符串不是操作系统隔离或平台身份认证。

原始行情、SID 表、数值、内部数据摘要及可重建派生结果均只能在 Cloud 内。旧 SYNTHETIC-only 本地缓存未被改成 Cloud 缓存；真实 Cloud 缓存、重启持久化和性能证明仍未实现/未运行。真实数据到 S1 因子面板也未开放，原 `to_s1_panel` 继续只接受 SYNTHETIC。

桥接的内部 source_sha256 是已验证 typed resolution 的规范 image、分隔换行和实际解析的 snapshot bytes 的组合摘要。snapshot 若传 bytes 则保留其原 bytes，若传 typed model 则使用规范 bytes；resolver 原非规范 JSON 字节不会原样保留。该摘要标识本桥输入，并不声称是 SDK/供应商服务器 wire 原包摘要。原 alias request 在 plan/映射上下文中保留；StandardDataBundle 的 request/provenance 则绑定派生 SID request，两者通过同一已核映射相连。

## 知识时点和控制输出

身份知识证据缺失直接拒绝，不能用“现在取得”或“订阅成功”补造 listed_from/known_at。成员与公司行动用 NOT_PROVIDED / PROVIDED_EMPTY / PROVIDED_NONEMPTY 区分；空列表不证明真实覆盖或历史事件验证成功。`BAR_END_PROXY` 只声明 bar-end 时钟，不是真实供应商披露或修订证明；即使输入声明 PROVIDER_DOCUMENTED，也不认证其来源。

[s2_cloud_check.py](../src/bbag/s2_cloud_check.py) 在同一桥接内计算质量并只返回固定 `CloudStandardizationReceipt`：project_id、源码/配置/原请求/策略绑定和封闭检查状态。无 SID、行数、覆盖率、行情、数据摘要或自由异常文本；执行区 stdout/stderr 丢弃。错误只返回 STANDARDIZATION_REJECTED；无法形成合法绑定时只抛 INVALID_REQUEST，不应向 Notebook 发布 traceback。

后续 d561 独立复审用普通预配置 StreamHandler 证实旧重定向不足，登记 S2-005（工程 FAIL）；该旧结论与原证据保留。当前修复另行封闭标准 logging 生成/分发及便利入口，原对象精确恢复，不使用 logging.disable；只支持经过前检的单主线程、标准实现、未冻结 GC 的同步环境。线程/trace/profile、自定义实现、嵌套或其他不受支持环境在数据回调前拒绝，回调内 Thread.start 被拒绝。真实 Notebook/SDK 如不满足该 profile 也会拒绝，不能凭本地合成通过声明 Cloud 可用。具体支持范围、取消异常与未支持旁路见 [S2-005](S2_005_LOG_OUTPUT.md)；新 SHA 仍须独立复审。

有数据且声明日历的每个 SID/session 字段齐全才通过 requested_field_coverage；空数据失败。成员为空或未提供，质量覆盖保持 UNVERIFIED，不能用空分母通过。非空成员/公司行动的 PASS 仅表示已提供并通过结构检查；provider 身份历史与披露修订真值始终 UNVERIFIED，缓存/平台恢复始终 NOT_RUN。这些回执是 DECLARED_INPUT_CHECKS / NOT_AUTHENTICATED，不是独立平台批准。

## 预算续接接口

[s2_continuation.py](../src/bbag/s2_continuation.py) 接收完整原 `CampaignReadiness`、新 source/config，以及由调用应用独立保留的 `expected_baseline_sha256` 和完整 `required_synthetic_ledgers`。每次调用 fresh 读取两份原账本及元数据，使用 S2-004 的捕获字节解析/隔离 SQLite 图像，两轮检查整链稳定性。保留原配置身份、四次尝试、源版本、原始元数据、未知账单和局部 budget；不改原 2/3 caps，也不把各旧库局部上限相加。

`OwnerBudgetIncrement` 只是有界提案；DRAFT 或 JSON 自称 OWNER_APPROVED 均不能扩大可用量。只有外部可信摘要 allowlist 承认、且 baseline/source/config 完整匹配时，preview 才计算提案增量。始终保留 Research 1，UNKNOWN/活动历史阻止计划通过；省略、重复、篡改或复制历史拒绝。以后合成演练可追加 SYNTHETIC_TEST 账本，并在完整链中扣减；真实追加账本/运行时 grant/dispatch **未实现**，必须另行设计和复审。preview 一律 dispatch_allowed=False，不创建库、不登记授权、不预留任务。

外部摘要和完整 later 清单是调用应用的信任输入，并不认证负责人身份；若调用方同时改掉自身保留上下文和 JSON，普通库无法发现删史。本轮交付以既有真实完整链生成 Δ0 的只读续接结果，没有把合成用例当成真实新消耗。

## 准确上传清单与编译增量

[plan_stage2_upload.py](../scripts/plan_stage2_upload.py) 在新 SHA 提交、工作树干净后生成 UPLOAD_MANIFEST.json、逐文件明文 payload 与 UPLOAD_PLAN.md。每项核 Git blob、磁盘原 bytes、SHA-256、字节数、远端路径、静态直接依赖和上传顺序；包含隐藏 Git 标记的修改也拒绝。FULL_FRESH 的精确闭包保留 main.py 和 bbag.s2_cloud_check 入口，无通配/编码打包/Notebook 搬运源码。

当前声明闭包为 cloud/main.py，以及 bbag 的 __init__、jsonio、contracts、s1_contracts、s2_access、s2_data、s2_cloud_data、s2_cloud_check，共 **9 文件**。最终文件数、每项摘要与排序必须以准确 SHA 生成物为准。外部 SDK/pydantic/tzdata 不复制进源码；requirements.lock 留在审核包，不能当作 Cloud 已安装证明。

每次 create_file/edit_file 连接器写操作自动编译 1 次；m 个文件加 e 次额外明确编译，需要增量 m+e。默认 e=0，若最终仍为 9 文件，则需要另行授权编译 **+9**（累计 cap 提案 11，已用仍 2）。没有把该提案应用到预算。远端状态 UNKNOWN_NOT_QUERIED，不按旧文件存在或字节相同推测可跳过；将来差量方案须先有获授权的真实源文件观察再单独冻结。任何失败/UNKNOWN 调用照计，未批准盲目重试。

```powershell
python scripts/plan_stage2_upload.py --commit <准确完整SHA> --output-dir <新上传计划目录> --max-file-bytes 128000 --max-total-bytes 1000000
python scripts/plan_stage2_continuation.py --commit <准确完整SHA> --baseline <已保留原CampaignReadiness.json> --baseline-sha256 <外部保留的规范摘要> --output-dir <新续接目录>
```

文件大小是显式工程限制，不是平台套餐/节点保证。续接命令只接受原真实活动，使用捕获配置/基线 bytes，固定无增量/无新账本；全部输出是草稿，只读原历史。Schema 通过 `python -m bbag schemas --stage S2 --output-dir contracts/s2 --check` 核验。示例 JSON 均为 ENGINEERING_TEST/DRAFT，不可作为运行授权。

[Cloud plan 示例](../configs/s2.cloud-data.example.json) 使用合成 a/b/c 摘要，未绑定真实部署；plan 自身不携带运行批准，保持 DRAFT 语义。[预算增量示例](../configs/s2.budget-increment.example.json) 明示 DRAFT、Δ0，不能加入真实可信授权表。真实候选 source/config 和准确上传摘要只在提交后的交付物中填写。

## 平台验收仍需补齐

| 条件 | 本轮状态 | 后续最小动作与授权条件 |
|---|---|---|
| 新 SHA 独立工程复审 | PENDING | 固定源码、锁、Schemas、合成及旧安全回归；先完成复审 |
| 上传与自动编译 | NOT_RUN | 核实当前项目/远端源码/资源后冻结写清单；负责人另批 m+e 增量，当前为 0 |
| 真实 SDK 查询工厂 | NOT_IMPLEMENTED | 在 Cloud 接入受审 history 参数与 RAW 订阅；不能把注入接口当作真实接线已完成 |
| 身份与知识元数据 | NOT_RUN | 取得许可且可追溯的映射/上市/知识时点、实际供应商版本；缺证保持拒绝/未验证 |
| 当前资源与费用 | UNKNOWN | 核实节点权限、可用资源、零增量费用与执行/轮询上限；未知账单不推定为 0 |
| 真实 Research | NOT_RUN | 当前剩 1 必须保留；未来若要新增任务，另批有界增量并实现/复审真实续账和预留入口 |
| 供应商披露/修订、历史池、事件 | NOT_RUN | 仅原白名单内取证；没有非空样本则保留缺口，不越界寻找案例 |
| Cloud 缓存/恢复/性能 | NOT_IMPLEMENTED / NOT_RUN | 分别完成云内存储与严格完整性、真实独立进程/会话重复命中、资源及 UNKNOWN 核查方案 |
| 独立平台验收 | PENDING | 使用真实准确源版本、任务/同步证据和控制回执；旧七标志与本地 mock 不代替 |

这些缺口不由增加次数自动解决。当前授权仅够离线工作；未补充任何真实上传、查询、节点切换或费用权限。保持 S6.4 最终配置复验、封存最终测试及其派生隔离、固定模型工件和滚动训练规则，F01—F08 不因局部测试通过而视为全路径完成。
