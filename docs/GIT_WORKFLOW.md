# BBAG Git 与独立审核流程

仓库为 [wwooyy429 BBAG](https://github.com/wwooyy429/BBAG)。main 保存负责人批准的基线。S0 的 f46fad2ff4b3ec9659001659fa44201e10951fd5 已获准合并为 b2555efe7201c473bdb66c7dd1c14552ad130ac5，见 [S0验收记录](S0_ACCEPTANCE.md)。S1 在 `codex/stage-1-factor-engine` 开发，未经本阶段独立复审和新授权不合并，也不进入 S2。

## 每次开发的固定顺序

1. 读取当前阶段任务和前置审核结论，在开发分支核实实际文件。
2. 完成范围内实现、自测和原始证据，明确未验证项。
3. 将源码、测试、Schema、依赖锁和维护文档提交到开发分支。
4. 取得准确完整 commit SHA。提交后如有代码或配置变化，生成新提交并重新验证受影响部分。
5. 按该 SHA 生成开发报告和 GPT Work 审核请求，导出准确提交源码并核对文件哈希。
6. 将报告和审核材料置于 Git 忽略的 `deliverables/`；以草稿 PR 交付，注明实现 SHA 和审核尚待完成。
7. GPT Work 对固定 SHA 独立复验。缺陷交回 Codex，在开发分支修复、提交新 SHA、生成新报告和审核包。
8. 核实独立审核结论确实绑定当前 PR head SHA，阻断项均已解决，再评估合并资格。当前授权为 S1，S1合并和下一阶段需另行授权。

不删除失败运行、旧报告、旧结论和复验记录，不把开发自测或协作代理检查写为独立 GPT Work 验收通过。

## 按提交组织交付

```text
deliverables/
  stages/S0/
    <完整实现SHA>/
      DEVELOPMENT_REPORT.md
      GPT_WORK_AUDIT_REQUEST.md
      AUDIT_RESULT_TEMPLATE.md
      review_request.json
      REPRODUCE.md
      SOURCE_COMMIT.txt
      manifest.json
      source.zip
      evidence/
    BBAG_S0_GPT_Work_<SHA前12位>.zip
```

实际打包目录可以增加 ZIP 和复现说明，但须保持一个报告、源码快照、证据与实现 SHA 的关联。清单记录来源、配置、数据或样例、环境及各文件哈希。源码快照必须从已提交内容导出，不能将不断变化的工作目录冒充固定提交。

开发报告在实现提交之后生成，因此报告不需要再提交回同一实现 SHA。若需要在仓库登记交付索引，单独注明它引用的实现提交，不能把索引提交误当被审实现版本。

提交后的标准命令如下，输出目录首次使用且不得覆盖旧证据：

```text
python scripts/verify_stage0.py --output-dir artifacts/verification/<完整实现SHA>
python scripts/build_review_package.py --commit <完整实现SHA> --evidence-dir artifacts/verification/<完整实现SHA>
```

打包器要求当前 HEAD 与实现 SHA 相同、工作区干净、开发验证来自该 SHA 且独立审核仍为 PENDING。源码从 Git 提交导出；未知来源、过期证据或未完成开发检查均不能冒充可交审的固定版本。

证据树应由普通文件和目录组成，子符号链接、junction、循环及根外路径在读取契约和创建交付前拒绝；需要引用的材料先复制为证据根内的普通文件。普通同步云文件不会仅因 Cloud reparse 标记被当作链接。实际每 SHA 输出目录与最终 ZIP 不得与证据树重叠，安全的共同父目录允许。已有输出目录、ZIP 或悬空目标链接会在创建新输出前拒绝；ZIP 使用排他创建，保留旧材料。

## 审核与合并条件

| 检查 | 合格要求 |
|---|---|
| 代码来源 | 审核源码、开发报告、包清单与 PR head SHA 对应 |
| 实际证据 | 运行命令、输入和原始输出可取得，失败与未运行项如实保留 |
| 独立性 | 审核来自独立 GPT Work 任务，开发自评不替代 |
| 审核范围 | 工程、平台和经济结论分别判定，S0 不虚构平台或经济结果 |
| 缺陷 | 当前范围内阻断项已处理，修复版本有对应复验 |
| 后续动作 | 未通过前保持草稿和 PENDING，不合并、不自动开始下一阶段 |

审核回执中的 `audited_commit` 必须匹配准确待审 SHA。结构检查通过不认证审核者身份、不证明证据真实、不授予合并权限。开发者不得自填通过来解除暂停；负责人须核实审核来源和报告适用范围。

## 本地辅助约束

仓库提供 `.githooks/pre-push`，配置方式为：

```text
git config core.hooksPath .githooks
```

钩子阻止直接推送 main 或 master，鼓励通过经过审核的 PR 更新基线。钩子只在已配置的本地仓库生效，不能阻止绕过钩子或从其他客户端改远程；草稿 PR 也不等于服务器端分支保护。

服务器端规则应单独核实。若尚未启用或当前工具无管理权限，在开发报告中明确保留此限制，不宣称仓库已实现服务器强制审核。无论服务器配置如何，Codex 仍须遵守本次不合并S1、不进入S2的用户要求。

S1使用scripts/verify_stage1.py和scripts/build_stage1_package.py，输出按deliverables/stages/S1/<准确SHA>组织；S0脚本不能冒充S1证据。S1回执为独立契约，不能用S0已通过回执批准S1。

## CI 与审核区别

阶段 0 CI 验证安装、契约、CLI、Schema 和单元测试，保留测试输出。CI 不调用 QuantConnect、GPT 或交易账户，不自动合并，不产生独立 GPT Work 审核批准。CI 通过与独立审核通过是两个不同条件。

## 历史规划材料

`docs/handoff/` 及早期打包材料保留当时的规划快照，可能描述尚未实施的阶段。新交付从当前准确实现提交生成，不能拿旧快照、旧哈希或旧模板中的状态作为新版本已验收的证据。
