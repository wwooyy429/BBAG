# S2-004：预算核验绑定已捕获字节

准确被审版本 `ecf424b06f5b7ef9290011a883e395de3bec7bd1` 的独立结论是 CHANGES_REQUIRED／工程 FAIL，新增 S2-004（P2）。原报告、回执、两类真实文件 ABA 反例及其静态退出 2／竞态退出 0 的证据保留在 `BBAG审核/audit_S2_ecf424b06f5b_20261031_2321`，不改成新版本通过。该审核仅在声明时钟契约范围关闭 S2-N01，供应商真实披露与修订仍未验证。

## 两处修复

元数据先按原文件名和大小上限捕获 bytes，再用同一 bytes 核对事件引用的长度、SHA-256 并严格解析。公共 `jsonio.parse_json_object` 复用原文件读取器的 UTF-8、逐层重复键、非有限常量及数值溢出拒绝，之后仍走 JSON 模式的严格类型模型。`CALLER_METADATA_BYTES` 可保留合法的空白、键序及转义编码；`LOCAL_NORMALIZED_METADATA` 仍额外要求原捕获字节等于规范编码。原路径不会在摘要核对后再被 JSON parser 打开。

数据库先捕获有上限的 bytes 并核对声明 pin，随后只连接 `:memory:`，通过 Python 3.11 起支持的 SQLite deserialize API 构建隔离查询图像。后续完整性、表/列/主键、来源/事件、费用、完整链等原查询都读取这个图像，不再打开原数据库路径。图像启用 query_only、trusted_schema=OFF 和内存临时存储；authorizer 只允许固定表读取及检查用只读 PRAGMA，拒绝写入、ATTACH、SQL 函数及修改这些限制的语句。连接关闭释放图像；没有新持久运行库、TaskLedger、grant、任务预留或派发。

原 TaskLedger 使用 WAL。即使正常关闭且 sidecar 消失，其主文件仍可有 WAL 格式头。SQLite 官方 deserialize API 不支持直接查询该格式，因此只在私有内存图像中把文件头偏移 18、19 的日志格式字节从 2/2 改为 1/1；原已核 bytes、数据页、磁盘文件及所报摘要都不变。已有 1/1 图像保持内容不变，其他头版本拒绝。该适配是已 pin 输入的确定性格式处理，不再读取磁盘上的另一份 B。[SQLite 官方说明](https://www.sqlite.org/c3ref/deserialize.html)

开始、每库结束及整链结束的原文件/元数据/sidecar 稳定性检查继续保留，但只作为辅助。承诺是“判定来自已核的 A”：错误 A 不会因解析时磁盘暂为合法 B 而成功。它不宣称能检测每一次被恢复的瞬时写入，也不认证调用者声明的 pin、负责人身份或平台授权。缺少 deserialize 支持时明确拒绝，不降级为按路径查询或创建空运行库。

## 复验与结论范围

新增回归使用合成临时账本及真实文件写线程，在捕获后把错误 A 换为合法 B，调用真实 JSON parser／SQLite snapshot 查询，再恢复 A；须确认写入实际发生、首尾原件相同且错误 A 始终被拒绝。覆盖来源/配置/请求错配、重复键/额外字段/非有限及类型错误、数据库 schema/事件矛盾；保留合法 caller 非规范编码、WAL/rollback 图像、只读查询及原闭项正例。旧的 predecessor 修改测试仅把调度点从原路径 connect 移到第二个捕获图像的构建边界，拒绝条件和原写线程保持。

共享 JSON 文件读取器现在委托同一严格 parser，S0/S1 调用不放宽；其受影响回归仍纳入新 SHA 完整基线。原审核脚本若依赖已删除的路径读取 hook，需明确保留原件，并用单独记录的调度适配副本复测，不能把未触发的旧攻击标成通过。开发自测、协作者检查和 CI 都不是独立验收；准确新 SHA、原始证据与未填写复审模板在提交后生成。

`src/bbag/jsonio.py` 也是历史八文件 Cloud 映射中的一份，因此不能再声称八份 Cloud 源码全部等于本次新 SHA。其余七份映射未改，云端保留旧部署；本地 JSON reader 提取和新的 readiness 图像路径均未上传或在 Cloud 执行。这属于部署身份缺口，不能用旧探针结果换写成本次平台证据。

本轮仅离线修复，平台 NOT_RUN、经济 NOT_APPLICABLE，不合并 S2、不进入 S3。日期仍 America/New_York 2020-01-02～2020-01-10，仅 SPY/AAPL；compile 已用 2/2、Research 已用 2/3，剩余 0/1，最后一次 Research 保留。原两库及四份 metadata 不改写，四笔实际账单仍未知，预算不重置。S6.4、最终及派生隔离、固定/滚动模型规则保持。
