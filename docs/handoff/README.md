# BBAG 开发与审核交接报告

> 历史规划材料：本目录主报告、启动说明、references 和 SOURCE_MANIFEST.json 保存首次实施前的规划快照，不能作为当前 S0 已实现或已通过审核的证据。当前交付按准确实现 SHA 生成于 deliverables/stages/S0，流程见 [Git 与独立审核流程](../GIT_WORKFLOW.md)。templates 继续作为交接模板使用。

本目录将同一套项目规范分别整理为开发者和独立审核者可直接使用的报告。references 保存本次交接的规范快照；项目根目录及 docs 中的主规范继续作为后续维护位置。文件校验用于识别版本，不能代替运行测试。

| 接收者 | 主报告 | 启动说明 | 结果模板 |
|---|---|---|---|
| Codex 开发 | [开发报告](CODEX_DEVELOPMENT_REPORT.md) | [开发启动说明](CODEX_START.md) | [阶段交付模板](templates/STAGE_HANDOFF_TEMPLATE.md) |
| GPT 或 GPT Work 审核 | [审核任务报告](GPT_AUDIT_REPORT.md) | [审核启动说明](GPT_START.md) | [审核结果模板](templates/AUDIT_RESULT_TEMPLATE.md) |

分别生成的 ZIP 包包含对应主报告、START_HERE.md、结果模板、四份规范快照、用户提供的原始流程图、SOURCE_MANIFEST.json 来源记录及 manifest.json 校验清单。包内相对链接可以在解压后继续使用。

开发首次执行范围为 S0，后续按阶段依赖与独立验收推进。审核每次还须取得被审阶段的固定源码和原始运行材料；当前只有规划规范，不能对系统功能或经济结果给出通过结论。

本目录中的任务段落用于后续交接，打包本身不启动系统开发、平台回测或模拟部署。
