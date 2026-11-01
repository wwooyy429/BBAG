# S2-005 标准日志出口修复

后续准确 `1f6f81d73d298df12884582c223fe744da0df82b` 的独立复审仅在受测 stock 同步范围关闭 S2-005，但整提交因新增 S2-006 仍 FAIL；S2-N02 另为 P3。当前续修见 [Manager 与警告桥状态](S2_006_LOGGING_STATE.md)，旧失败、有限关闭和新发现均保留，不把本说明视为整 S2 平台批准。

被拒版本为准确 `d5615f678d46836d50722e1ea9f7d5f4a26ad597`。独立复审 CHANGES_REQUIRED／工程 FAIL，新增 P2 S2-005：预配置普通 logging.StreamHandler 持有旧 stdout/stderr，绕过 redirect_stdout/redirect_stderr。成功返回 CHECKS_COMPLETED 或错误回执均不能阻止旁路日志输出合成 SID、价格、计数和摘要。原四项安全期望失败与根成功路径见证保存在 `BBAG审核/audit_S2_cloud_d5615f678d46_20261101_0313`，不得改算通过或覆盖。原 1813/12、228 和既有关闭项仅是该审核的实际结果，新修复须绑定新 SHA 独立复审。

## 支持的同步 profile

`run_cloud_data_check` 支持标准 Python 3.11+ logging 的普通同步消息入口，包括 root、getLogger、直接构造的未注册 Logger、标准 LoggerAdapter，以及预先绑定原 stdout/stderr 的普通 StreamHandler。这里只提供应用内输出边界，不认证 QuantConnect 宿主或供应商真值。

在 resolver/fetcher 前取得进程级非重入锁，并检查：当前是唯一 Python 主线程、没有 trace/profile、GC freeze count 为零；root/manager/factory、Logger/Adapter 类及日志入口为标准实现，没有已知实例覆盖或 RootLogger shadow。用 GC 的 type/issubclass 检查发现直接构造且不在 loggerDict 的对象，不调用任意对象 repr 或代理 __class__。已知不支持的配置返回固定 ERROR，数据回调不得执行；重入/并发不等待、不创建任务。

回调必须同步使用标准消息接口，不得替换 logging 实现、动态重新配置宿主、保存并调用任意旧实现、用 native/OS 文件描述符或另建执行器越过边界。这不是任意 Python、信号、native IO 或恶意代码沙箱。实际 Cloud Notebook 常有后台线程；如不满足该窄 profile 必须拒绝，不能放宽检查再声称安全。未来真正 SDK 运行需要先验证兼容的隔离执行环境及其版本，再按独立审核/负责人授权推进；本轮不启动 worker 或 SDK。

## 隔离和恢复

- 临时让 Logger.isEnabledFor 返回 false，阻止普通消息修改冷热 cache 或进入 Adapter.process／LogRecordFactory；不调用 logging.disable，也不改变 manager.disable。单独 disable 对未注册 Logger 的缓存并不可靠。
- 封闭 Logger._log/handle、Handler.handle、StreamHandler.emit；模块级 log/debug/info/warning/warn/error/exception/critical/fatal 和 basicConfig 同步静默。basicConfig 的封闭也保护预先保存的标准模块 warning 函数，避免 root 原无 handler 时产生新的 sink handler。
- Logger.warn/LoggerAdapter.warn 另行静默，避免弃用 warning 在 captureWarnings 场景下修改 py.warnings 配置。
- 既有 exact 标准 StreamHandler/FileHandler 的 stream 字典项临时换成 sink，再恢复同一原对象；不调用 setStream，不 flush、close 原 stream，也不修改 handler 的 formatter/filter/level 或 logger 配置。普通预保存标准 emit 也使用被换入的 stream；不据此承诺任意自定义 formatter/filter／旧函数旁路安全。
- 同期 stdout/stderr 指向 sink，Thread.start 被拒绝，防止支持范围内的 Python 线程晚于作用域输出。native/_thread 或自行替换实现仍不属于该 profile，禁止把线程预检当成 OS 隔离。
- finally 在全部异常路径反向恢复原方法和 stream，释放锁；数据回调的 BaseException（含携带敏感消息的 SystemExit/KeyboardInterrupt/GeneratorExit）也只形成固定 STANDARDIZATION_REJECTED 回执，不把原消息、cause 或 traceback 交给调用者。此 API 的取消表现为 ERROR，控制器应停止，不据此盲目重提任务。

原 alias→SID、RAW 标准化、质量、预算与任务模型未更改。isEnabledFor 等方法只在作用域临时替换；退出后普通控制日志仍能正常输出。无自动 logger 配置、线程、SDK 导入、任务预留或运行授权。

## 复测与证据

`tests/test_s2_cloud_output.py` 使用合成 fixture，覆盖成功/错误 × resolver/fetcher × stdout/stderr × getLogger/direct Logger × 冷热 cache；额外检查非标准整数日志级别、原配置/cache/对象恢复、退出后控制日志、标准 Adapter、root/lastResort/模块便利函数、cached emit、延迟 FileHandler、取消异常、嵌套/worker/已有线程/新 Thread、冻结 GC 与不支持 factory/方法/实例/manager 的写前拒绝。旧 `tests/test_s2_cloud_check.py` 的成功与拒绝断言保留。

提交后按新 SHA 跑完整 CI 和 Windows 定向，另重放独立原四项安全 pytest、四个 minimal OS 错误路径及原根成功 child。保留原件字节／hash；只在明确的运行上下文副本中绑定新 source SHA，不改变旧安全断言。成功 child 的旧 driver 断言“泄漏成功”，不能改成产品安全 oracle；使用原 child 加外部“实际完成且流无泄漏”断言，单独记录适配差异。原失败及开发过程中失败迭代均保留，开发者重放不是独立验收。

完整复现入口仍为 `python scripts/verify_stage2.py --output-dir <新目录>`；上传清单按准确新 SHA 的 `scripts/plan_stage2_upload.py` 重算逐字节摘要和字节数。源码闭包仍为 9 文件，m=9/e=0 的编译 +9 提案未获授权、未执行；不能沿用 d561 的旧 payload hash/字节数。

本轮 QuantConnect 工具调用（包括元数据）、上传、编译、Research、真实 grant/新账本/新预留均为 0。原白名单 America/New_York 2020-01-02～2020-01-10、SPY/AAPL 不变；累计编译已用 2/2、Research 2/3、四笔实际账单未知，保留 Research 1 后新增可用 0/0。平台 NOT_RUN，经济 NOT_APPLICABLE；S2 不合并、S3 不开始。S6.4、最终及派生隔离、固定/滚动模型规则保留。
