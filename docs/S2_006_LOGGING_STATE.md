# S2-006 与 S2-N02 日志状态修复

基线为被审 `1f6f81d73d298df12884582c223fe744da0df82b`。独立复审在受测 stock 同步范围关闭 S2-005，但工程仍 CHANGES_REQUIRED／FAIL：S2-006（P2）允许标准 Manager 实例 getLogger 包装进入两条数据回调；本例只记录内存，没有证明数据泄漏。S2-N02（P3）则是 captureWarnings 的 NullHandler 残留，导致后续控制日志被吞。原 Profile30 的29通过/1失败、Logging75 的74通过/1失败和 OS 见证保留于 `BBAG审核/reaudit_S2_005_1f6f81d73d29_20261101_1304`，不改算通过。

新修复的两项关闭均待准确新 SHA 独立复审；既有 S2-005 有限关闭、S0/S1、S2-001/002/003/004 和 S2-N01 原范围保持，S0-N02 仍是另一项历史 P3。

## Manager 生效分发前检

仅核 `logging.Manager.getLogger` 类字典会漏掉实例覆盖。当前前检同时核已知 Manager 类分发、辅助函数及 disable property 的标准实现，再检查普通实例字典，拒绝 getLogger、setLoggerClass、setLogRecordFactory、_fixupParents、_fixupChildren、_clear_cache、__init__ 或 disable 的实例 shadow，不调用这些包装函数来“试探”。

检查 Logger 类默认 manager、root 的静态生效 manager 和 GC Logger 实际 manager 的共享关系；Manager 的 root、registry、factory、disable 和无 handler 标志从普通字典读取，类型/身份严格检查。GC 可见的 Manager 及 registry 的 stock PlaceHolder 分发、实例 shadow 和 loggerMap 也检查，避免普通 getLogger 经 helper 进入不受支持的实现。静态分发 dunder 覆写拒绝，原单主线程、无 trace/profile、GC 未冻结和非重入限制保留。

Manager 的标准实例数据字段上若新增类级 descriptor，或 PlaceHolder.loggerMap 被类级 property 覆盖，也在首回调前拒绝；否则 plain vars 与实际属性分发可能不同。这些 getter 不被执行来测试安全性。

这些检查只约束既有受支持的同步 logging profile，不是任意 Python、动态重配、native 或 OS 沙箱；callback 仍不得替换宿主实现或另建执行器。实际 Cloud/SDK 兼容性 NOT_RUN；不符合环境须先拒绝，不得为取得平台结果放宽边界。

## captureWarnings 状态

标准 captureWarnings(True) 已将当时的 `logging._showwarning` 函数对象保存到 `warnings.showwarning`，只改 logging 模块属性挡不住正常 warnings.warn。

当前在已验证的作用域内同时封闭模块 bridge，并识别、临时封闭生效的 stock `warnings.showwarning` bridge 指针；finally 恢复原对象。截断发生在 formatwarning、getLogger 和 NullHandler 添加之前。因此不需要事后删 loggerDict、清 handler 或修 parent，也不会由该桥创建原本不存在的 py.warnings。

不替换 warnings.warn，不修改过滤规则、原 warning delegate 或 registry；过滤为 error 时仍由标准 warnings 先抛出，再按既有控制入口形成闭合 ERROR。原空/有 handler、原 logger 不存在、成功/异常后，既有 handler/filter/cache 对象和 logging registry 保持，普通控制 warning 的 lastResort／原 handler 输出继续可用。此处不是承诺任意自定义 warning hook、旧函数旁路或 host 动态重配置安全。

## 验证和交接

新增 `tests/test_s2_logging_state.py` 覆盖 14 个 Manager 类/实例方法覆盖的写前拒绝（两个数据回调及包装委托计数均0）、正常 stock factory 成功、GC 外部 manager 拒绝，及12个 warnings 场景：原无 logger/原空 handler/原有 handler × resolver/fetcher × 成功/异常。断言放在入口返回后，并验证对象身份/状态和退出后的实际控制日志，不靠恒拒绝通过。

另有7个标准实例字段 class descriptor 拒绝场景，断言 getter 和两个数据回调均未运行。新文件共37项；原66项不改断言。

原 `tests/test_s2_cloud_output.py` 与 `test_s2_cloud_check.py` 的66项保留。提交后重放本次原 Profile30、Logging75 和对应 OS child，并以外部新安全 oracle 检查成功、首 IO 前拒绝与恢复；原反例 driver 的“攻击成功”断言不能改算通过。原件/hash、机械 source/context 适配、原失败与实际 raw/JUnit 分别保存，开发者测试不代替独立关闭。

固定完整本地协议仍为 `python scripts/verify_stage2.py --output-dir <全新目录>`；准确 source/锁/Schemas/样例/原证据/回执模板在提交后生成。上传继续按准确新 SHA 的9个自有源码文件重算 hash 和字节数，m9/e0/+9编译仍只是未授权、未执行提案，不能沿用1f6旧payload。

本轮 QuantConnect（含元数据）、上传、编译、Research、真实 grant/新账本/新预留均0。原白名单 America/New_York 2020-01-02～2020-01-10、SPY/AAPL 不变；累计已用编译2/2、Research2/3，4笔实际账单未知，Research1保留后新增可用0/0。平台 NOT_RUN，经济 NOT_APPLICABLE；不合并 S2、不进入 S3，S6.4、最终及派生隔离、固定/滚动模型规则保留。
