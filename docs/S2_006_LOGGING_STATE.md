# S2-006 与 S2-N02 日志状态修复

## 当前 Manager 值对象协议残余修复

准确 `6d6915fc814c6e5771eefcfed8b267561d068e82` 独立复审确认原 Root 字段 descriptor 修补、原 Manager41 和新增38个邻近 OS 场景均符合；但另一个单列安全用例失败：在 exact Manager 类预先添加普通 `__get__` 后，raw Logger.manager 仍是同一个 Manager 对象，读取类属性却会隐式调用该对象的 descriptor 协议。实际为 resolver → descriptor-get → fetcher → CHECKS_COMPLETED。本例仅记内存，未证明数据泄漏或费用；原 Root 标准日志泄漏见证在6d版本已正确拒绝，不能混称继续泄漏。失败原件在 `BBAG审核/reaudit_S2_006_root_6d6915fc814c_20261101_1535`，S2-006 仍 OPEN/P2；S2-N02 和有限 S2-005 关闭保持。

当前先验证参与的 stock 类与每个 MRO base 的 metaclass 都是 exact `type`，再读 MRO 或类字典。即使显式调用 `type.__getattribute__`，自定义 metaclass 的 data descriptor 仍可能执行；不能把该调用本身当作静态安全证明。

随后从这些原始类字典检查 `__get__`、`__set__`、`__delete__`，任一出现都拒绝，包括值为 None；不读取或调用 Manager 的协议来试探。该规则覆盖普通 shared Manager 以及参与 profile 的其它 plain logging 对象类型/MRO。函数、已核标准 property、classmethod 与 stock C 字典槽仍走其原有明确核验，不一律禁用合法描述符。仅在 Manager 实例字典放普通同名键不构成 Python 类型协议，正常配置仍须成功。

相邻已知分发也在首回调前静态拒绝：Logger/RootLogger 的 name、parent、level、filters、handlers、propagate、disabled、_cache 类字段覆盖；root 类引用须共享同一 root；新增 `__bool__`/`__len__` 会影响标准 Manager 的 parent 遍历，因此与原属性分发 dunder 一并拒绝。未知 class 的身份检查不用 hash/equality，plain 实例字典先要求 exact string keys。所有拒绝都在 getter 和数据回调前进行，原同Manager完整成功与 warning 状态恢复须继续通过。

新增 `tests/test_s2_manager_protocol.py` 的44项覆盖同一 exact Manager 的三种协议、None/函数、有/无合法实例字段，合法 MRO 协议、仅实例普通键和 stock 成功对照，并补 metaclass、root 字段及 truthiness 的真实入口边界；连同原121项组成165项定向测试。此处只声明已核静态布局和受测同步路径，不宣称任意 Python/native/OS 沙箱或全部 logging API 的实现真实性；动态宿主重配仍禁止，真实 Cloud/SDK 兼容性 NOT_RUN。关闭仍须新准确 SHA 独立复审。

## 历史 6d Root 字段残余修复

准确 `d7bb4f0518fec40c69c86524c2164de67e4b8fbb` 独立复审已在受测 stock warnings 范围关闭 S2-N02，原 Manager 实例覆盖也正确拒绝。但新增41个 OS 用例有40符合、1失败：RootLogger.manager 为 property，root 普通字典同时放有合法 stock Manager 时，原 dict 优先模拟忽略了类 data descriptor 优先级。实际预检执行 getter 后进入两个数据回调；另一个标准 Logger/StreamHandler 见证在隔离开启前输出合成 SID、价格和条数。旧纯内存 Manager 反例仍不改称泄漏，实际 Cloud/SDK 未执行。完整失败证据保留于 `BBAG审核/reaudit_S2_006_d7bb4f0518fe_20261101_1445`，S2-006 仍为 P2，关闭待新 SHA 独立复审。

当前修复从类 MRO 的原始字典静态读取字段，要求 Logger/RootLogger 类默认 manager 为同一 exact stock Manager；遇到 property 或其它 descriptor，即使实例字典有合法同名字段也拒绝，不调用 getter 试探。manager 与 _cache 只从已验证的普通字典读取，移除预检内 `value.manager`、`logging.root.manager`、`value._cache` 的动态读取。类上新增 _cache 字段也属不支持配置。

`object.__getattribute__(obj, "__dict__")` 本身仍会执行类上的同名 property，因此实例状态只通过静态核验的 stock C `__dict__` 槽读取；Logger、Manager、PlaceHolder、Adapter 和 stock stream handler 共用此规则。继承链上的属性分发 dunder 覆盖拒绝，方法须是实际普通 Python function 才读取来源元数据，logger/factory 配置直接读取 logging 模块字典。不存在“先执行 getter 再判结果”的分支。

新增 `tests/test_s2_root_dispatch.py` 的18项保留正常同Manager成功，覆盖 RootLogger.manager 普通/被实例字段掩盖的 descriptor、标准日志影响见证、相邻 _cache 与合法MRO中的 __dict__ descriptor、Adapter.logger 及 stock handler.stream；拒绝用例要求 ERROR、getter/resolver/fetcher 均0、无合成标记逸出，退出后配置恢复。连同原103项组成121项定向测试。真实平台兼容性仍 NOT_RUN；严格同步 profile 和原授权边界不变。

## 历史 d7bb 修复说明

基线为被审 `1f6f81d73d298df12884582c223fe744da0df82b`。独立复审在受测 stock 同步范围关闭 S2-005，但工程仍 CHANGES_REQUIRED／FAIL：S2-006（P2）允许标准 Manager 实例 getLogger 包装进入两条数据回调；本例只记录内存，没有证明数据泄漏。S2-N02（P3）则是 captureWarnings 的 NullHandler 残留，导致后续控制日志被吞。原 Profile30 的29通过/1失败、Logging75 的74通过/1失败和 OS 见证保留于 `BBAG审核/reaudit_S2_005_1f6f81d73d29_20261101_1304`，不改算通过。

以下为 d7bb 的历史修复内容；后续独审已关闭 S2-N02，S2-006 仍有上述残余。既有 S2-005 有限关闭、S0/S1、S2-001/002/003/004 和 S2-N01 原范围保持，S0-N02 仍是另一项历史 P3。

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
