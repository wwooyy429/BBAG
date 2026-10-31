# S1 确定性计算与公式审计

负责人于2026-10-31接受S0并授权合并和S1。被审S0提交为 `f46fad2ff4b3ec9659001659fa44201e10951fd5`，实际合并结果 `b2555efe7201c473bdb66c7dd1c14552ad130ac5`，源码树相同。S1在 `codex/stage-1-factor-engine` 开发，新SHA独立审核保持PENDING，不合并S1、不进入S2。

## 范围和接口

本阶段提供受限表达式、纯算子、合成时点面板、批量/逐期因子、独立未来价格标签及准确提交交付。无统计alpha评估、模型请求、QuantConnect、回测、优化或部署；外部模型和量化平台调用/费用为零。完整F01—F08均未运行，S6.4开发复验、最终信息及派生隔离、固定/滚动模型规则仍按原规范执行。

S0的13个契约和Schema保持。S1另有16个版本1.0契约，Schema在contracts/s1；S1Config聚合MarketPanel、固定universe、S0 FactorSpec、AuditLimits、NumericPolicy、LabelRule、as_of及未决事项。S1ReviewReceipt专用于S1，S0回执不能批准S1。JSON入口统一拒绝额外键、任意深度重复键、非有限常量及浮点溢出。样例和输出均为ENGINEERING_TEST/DRAFT，不代表研究参数或经济批准。

## 时间和数据

Session显式给出日期及开盘、收盘、信号时刻，要求 `open_at < close_at <= signal_at < 下一session.open_at`。开收盘日期匹配IANA时区，时间比较转换UTC以处理DST fold。合成日历不代表交易所真实校验。

Observation以(session,asset)唯一；values必须恰含panel.fields，用null表示缺值，缺整条bar也视为缺失。字段共享保守available_at，且不得早于当日close_at。更精细的逐字段可用时点、公司行动、历史成分和真实数据权限由S2落实。字段名为不含语言关键词的ASCII标识符；日历有序且不重复。

每个因子行使用自身signal_at截止，as_of只决定哪些信号已经形成。递归历史操作数使用当前决策signal_at当时已知的历史：迟到旧报价可参与以后信号，不回填过去已形成因子。批量保留全表但逐次检查时点；逐期仅送入当前session前缀和已到达观察，两者共享纯算子。因子服务没有标签参数或标签调用。

固定universe同时决定目标资产和截面排名范围；基准不自动加入排名。S1不重建真实历史股票池。

## 公式、数值和限制

只允许字段、有限数字、括号、白名单调用及+/-/*/。return作为本DSL函数合法，不执行Python代码；属性、下标、字符串、布尔、关键字参数、推导式和未知名称拒绝。根必须引用字段；时序/排名输入也须引用字段，算术可以广播标量。

| 算子 | 语义 |
|---|---|
| neg/add/sub/mul/div | 逐资产/session运算，除0输出null |
| delay(x,n) | 当前决策已知的第n个过去session的x |
| return(x,n) | 当前x/第n个过去session的x−1，非未来标签 |
| rolling_mean(x,n) | 最近n个session完整窗口均值 |
| rolling_std(x,n) | 完整窗口总体标准差，ddof为整数0 |
| ts_rank(x,n) | 当前值在窗口的平均名次，以(名次−1)/(n−1)归一 |
| cs_rank(x) | 固定universe内非缺失值的同样归一平均名次 |

窗口不足、窗口内任一缺失、算术缺值、除零及非有限结果输出null；不前填、不跳过缺失session、不裁剪。常数窗口std=0且rank=0.5；单个有效排名=0.5，截面缺失资产仍为null。tie按浮点精确相等，答案容差不改变排名。均值和总体标准差采用稳定中间求和。

窗口必须为裸正整数字面量，拒负数、0、5.0、1e0、布尔或表达式窗口。数字字面量每次出现（含窗口）计入parameter_count，不等同经济搜索的独立试验数。AST统计节点、深度及包含当前session的回看期：field=1，算术取子最大，delay/return加n，rolling加n−1，cs_rank不增加。声明字段恰等于引用集合，声明回看可更保守但不能不足或超限。

示例cs_rank(neg(return(close,2)))回看3、节点5、深度4、参数1。解析器硬限4096字符和64层语法嵌套。面板最多256session、32资产、16字段、8192观察；工程工作量估计为 `节点 × min(回看,日历长度) × 资产数 × 已形成信号数`，超配置上限先拒绝。该值为工程输入限制，不是性能承诺或正式研究预算批准。

## 标签与成熟

LabelRule独立读取未来输入。目标i在i+1开盘入场，i+1+H开盘退出，H不等同调仓周期。记录signal_at、entry_at、exit_at、mature_at；成熟取退出时刻和所有目标/基准必要报价available_at的最大值。因此共同日线availability比真实退出开盘更保守。

当前固定开盘事件只支持 `price_field="open"`，用于目标与基准的相同开盘区间。配置、直接标签服务与CLI均拒绝 `close`、`volume`、别名或其它报价字段，不能用延迟成熟来替代事件与价格的一致性；也不静默改写为open。字段仍可作为因子输入。此约束修复原提交 `f5c07d2b878c3b7e9f122bcf064ef45589d9aaa7` 的阻断S1-001；原先被误接纳的非open标签配置须拒绝，详见 [修复说明](S1_REMEDIATION.md)。S1 Schema版本仍为1.0，LabelRule及嵌套配置/结果的price_field收紧为常量open；合法open配置和既有手算答案不变。

| 状态 | 行为 |
|---|---|
| MATURED | 必要正价格有效且as_of达到成熟时间，才返回value |
| NOT_MATURED | 尚未可评估，value=null |
| INSUFFICIENT_FUTURE | 已登记日历不足以定位完整未来区间，value=null |
| MISSING_INPUT | 到期缺报价、价非正或计算非有限，value=null |

price_return=退出/入场−1；excess_price_return再减相同入场/退出的基准价格收益。S1不把原始价格收益称为总收益，没有实现公司行动、分红、成本或真实市场。S0 excess_total_return仍为正式协议方向，待S2—S4有对应数据与授权后实现，合成价格夹具不能替代。

未来数据按定义可以改变标签，不能改变过去因子；未成熟结果不披露value。S3训练仍须按当时已成熟标签准入。S1没有训练或运行时封存数据访问控制，后者仍由S2落实。

## 手算与复现

configs/s1.example.json有8个合成session、A/B/C目标和BM基准。tests/fixtures/s1_expected.json手列24个排名，用整数交叉乘积约分记录15个成熟超额价格标签和9个未来日历不足状态。第一个A答案为100/5151，来自(106×101−103×102)/(102×101)。参考检查不调用生产算子/标签。

绝对及相对容差预登记1e−12。另独立测试延迟、滚动均值/标准差、常数和并列、缺失/除零/溢出、晚到、未来扰动、截断、批量/逐期、DST、成熟精确边界、配置原地突变和工程工作量边界。

```powershell
.\.venv\Scripts\python.exe -m bbag s1-audit --config configs/s1.example.json
.\.venv\Scripts\python.exe -m bbag s1-factors --config configs/s1.example.json --output-dir artifacts/s1-demo --run-id batch --mode batch
.\.venv\Scripts\python.exe -m bbag s1-factors --config configs/s1.example.json --output-dir artifacts/s1-demo --run-id incremental --mode incremental
.\.venv\Scripts\python.exe -m bbag s1-labels --config configs/s1.example.json --output-dir artifacts/s1-demo --run-id labels
.\.venv\Scripts\python.exe scripts/check_stage1_results.py --expected tests/fixtures/s1_expected.json --batch artifacts/s1-demo/batch --incremental artifacts/s1-demo/incremental --labels artifacts/s1-demo/labels
.\.venv\Scripts\python.exe -m bbag schemas --stage S1 --output-dir contracts/s1 --check
```

输出不可覆盖；run.json关联规范配置hash、实际结果字节hash、代码SHA、脏状态和创建时刻。提交后新SHA干净源码采集原始证据，使用S1专用验证/打包工具；S0脚本和回执不能冒充S1。

```text
python scripts/verify_stage1.py --output-dir artifacts/verification/S1/<完整实现SHA>
python scripts/build_stage1_package.py --commit <完整实现SHA> --evidence-dir artifacts/verification/S1/<完整实现SHA>
```

打包包含准确提交源码、依赖锁、合成配置和手算答案、stdout/stderr、JUnit、复现命令、文件hash、待审请求及空模板。可选历史审核目录按实际存在性描述，不把缺目录写成已交付。新SHA独立审核PENDING，不自动合并或进入S2。
