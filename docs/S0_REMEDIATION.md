# S0 审核修复范围与证据规则

本轮仅修复 `b2d502048dfed1f8765514a018bc91560d87ec22` 收到的三项 S0 阻断问题。原结论为 CHANGES_REQUIRED、工程 FAIL、平台与经济 NOT_APPLICABLE。原报告自述执行者为独立 Codex 会话，未认证 GPT Work 身份；负责人提供的修复任务构成本轮 S0 授权，不构成合并或阶段推进授权。

| 编号 | 修复约束 | 开发回归 |
|---|---|---|
| S0-001 | 配置、回执及所有打包 JSON 在契约校验前统一拒绝任意深度重复键、非有限常量和浮点溢出 | `test_review_json_regressions.py`、`test_package_contract_regressions.py` |
| S0-002 | 按实际 testcase 计算结果；拒绝空明细、失败、错误、全部跳过及摘要矛盾；每个嵌套 suite 的摘要覆盖自身子树，汇总不重复计算 | `test_junit_regressions.py` |
| S0-003 | 完整验证 ResearchProtocol、ExperimentRecord 和 ReviewReceipt；再核对规范协议字节、原始 SHA-256、准确代码 SHA、clean 状态及 S0 待审状态；验证失败先于任何打包输出 | `test_package_contract_regressions.py` |

契约与 Schema 版本仍为 1.0，字段及合法值不变。本轮修正读取和证据接纳规则，不迁移合法 collector 输出。协议证据须逐字节等于 `canonical_protocol_bytes`，哈希针对保存的原始字节；仅改空白再重算哈希也不能冒充规范输出。JSON 模式的严格时间校验保留合法 ISO 带时区时间，拒绝缺失、无时区及错误类型。

JUnit 支持无命名空间的 testsuite 根与 testsuites 容器及其嵌套。每个 testsuite 必须提供 tests、failures、errors、skipped 非负整数摘要，并与其子树 testcase 一致。testsuites 可以不提供摘要；提供时须完整且一致。至少一个 testcase 实际成功，任何 failure/error 明细均拒绝。合法的部分跳过可保留，并按明细如实统计。

原审核文件、失败日志、原测试和反例保留原样，生成的每 SHA 交付包附带来源、大小及哈希。对原审核测试进行新版本复跑时，只透明适配 SHA、目录和真实证据的统计量，保存原件、适配差异及原始输出；开发者复跑不作为新的独立验收。

新提交的开发报告、缺陷对照和审核请求在提交后生成，绑定新完整 SHA。旧版 CHANGES_REQUIRED 保留；新版本独立审核为 PENDING。继续保持草稿 PR，不合并 main、不启用自动合并、不启动 S1，不调用模型或量化平台。

完整 F01—F08 路径仍未运行。S0 本轮仅执行契约、CLI、来源及证据接纳回归；S6.4 复验、最终测试隔离与固定/滚动模型规则不变。研究与资源门槛由项目负责人在相应正式工作前确认，DRAFT 演示值不构成批准。
