# S0 补充开发材料核对与修补

核对基线：`2eccc6418674314c2463a34783b8dd9fa1ad2971`。补充输入为 `deliverables/BBAG_Codex_开发包_20261031_v1.zip` 与 `docs/handoff/CODEX_DEVELOPMENT_REPORT.md`，均属于首次实施前的规划材料。ZIP 中原报告与维护文件的正文一致，维护文件另有历史版本说明。补充输入用于检查现有 S0，不授权 S1—S7。

## 要求覆盖

| 补充要求 | 当前 S0 对应 | 核对结论 |
|---|---|---|
| Python 工程、依赖记录、运行入口 | pyproject.toml、requirements.lock、src/bbag、CLI、CI | 已覆盖 |
| 七个主接口及配置／审核子契约 | contracts.py 中13个模型与已跟踪JSON Schema | 已覆盖，Schema版本1.0 |
| 机器可读样例、待确定事项 | configs/s0.example.json、RESEARCH_PROTOCOL.md | ENGINEERING_TEST/DRAFT；正式研究与资源值仍待负责人确认 |
| 严格类型、额外键、非法数值与日期、跨契约规则 | StrictContract、jsonio、ResearchProtocol、相关回归 | 已覆盖；本轮补充回执批准状态的一致性 |
| 时间因果、最终信息及派生隔离、固定／滚动模型规则 | RESEARCH_PROTOCOL、ROADMAP、ACCEPTANCE、FLOW_ACCEPTANCE | S0设计已覆盖，真实执行属于后续阶段；S6.4不可省略 |
| 编号、协议哈希、代码与工作区来源 | ExperimentRecord、规范化字节、git_provenance、来源边界测试 | 已覆盖 |
| 固定提交、原始证据、复现、哈希和未填写模板 | verify_stage0、build_review_package、每SHA交审目录 | 已覆盖；本轮修补路径与冲突前置检查 |
| 因子、真实平台、收益评估、完整F01—F08 | S1—S7规范及其退出条件 | 后续阶段，不属于本轮遗漏，不实现或运行 |

## 四项实际修补

| 编号 | 已复现的问题 | 修复规则 | 回归入口 |
|---|---|---|---|
| S0-SUP-001 | 合成S0回执同时声明 APPROVED 与平台 FAIL，CLI仍结构通过 | 仅APPROVED增加平台NOT_APPLICABLE要求；PENDING/CHANGES_REQUIRED历史枚举保留 | test_review_platform_consistency.py |
| S0-SUP-002 | 证据内junction使完整打包收入证据根外的合成文件 | 契约读取前遍历证据树，拒绝子symlink/junction、canonical越界和特殊文件；不误拒普通Cloud reparse文件 | test_package_path_boundaries.py |
| S0-SUP-003 | 输出放在证据子树时，source.zip先创建，copytree将输出复制回输入 | 在任何新输出创建前拒绝实际SHA目录／ZIP与证据根相等或互为祖先；安全共同父目录仍允许 | test_package_path_boundaries.py |
| S0-SUP-004 | 已有目标ZIP时，报错前先留下完整SHA目录、READY_FOR_REVIEW报告及manifest，阻断重试 | 前置检查已有SHA目录、ZIP及悬空链接；最终ZIP排他创建 | test_package_path_boundaries.py |

原问题仅在隔离合成夹具复现。junction反例实际产生包并核实根外合成内容；递归复制在确认真实危险copytree调用后由测试guard中止，记录为受控复现，不宣称自然完成或自然退出。旧输出保留，不用反例包作为开发验证、独立批准或经济证据。

字段、枚举、默认值及13份Schema不变，契约版本仍为1.0。新增APPROVED跨字段条件拒绝原先语义矛盾的状态，合法S0批准仍只提供advisory结果，不认证身份、不授权合并。证据子链接和重叠输出路径不再接纳；复现材料应交付普通文件副本，并使用与证据树分离的交付路径。普通文件、目录和合法旧PENDING/CHANGES_REQUIRED结构仍可读取。

旧 `b2d502048dfed1f8765514a018bc91560d87ec22` 的 CHANGES_REQUIRED 与原始失败证据保留。基线 `2eccc6418674314c2463a34783b8dd9fa1ad2971` 仍未取得独立验收；其开发验证不能自动覆盖本轮代码。修补后另提交、重新验证和按新完整SHA打包，独立审核保持PENDING，不合并main、不启用自动合并、不进入S1。
