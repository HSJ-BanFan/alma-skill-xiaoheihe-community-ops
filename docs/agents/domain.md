# 领域与架构索引

领域术语以 [GLOSSARY.md](../../GLOSSARY.md) 为准。本页记录维护与验收入口，不另行定义术语。

## 先读哪些契约

| 维护问题 | 权威文档 | 核对边界 |
| --- | --- | --- |
| 平台能力与运营任务的归属 | [领域词汇](../../GLOSSARY.md)、[分期路线图](../ROADMAP.md) | `Platform Primitive` 保持单次平台交互职责。`Ops Workflow` 组合能力并承担运营目标。 |
| SDK 的来源与演进 | [ADR-0001](../adr/0001-independent-sdk-fork.md) | `scripts/xhh_sdk` 已独立 Fork，由本仓库独立拥有并演进。 |
| 自主边界与互动持久化 | [ADR-0002](../adr/0002-policy-and-fact-store-separation.md)、[互动存储](../INTERACTION_STORAGE.md) | `Autonomy Policy` 独立于 `operations.sqlite3`。互动事实库只保存结构化元数据。 |
| 当前入口与未来功能 | [调用参考](../../REFERENCE.md)、[分期路线图](../ROADMAP.md) | 当前入口必须从 `scripts/xhh.py` 实核。路线图中的 `xhh ops` 是未来计划。 |

`Interaction Fact`、`Operation Receipt` 与实际写入计数遵守仅元数据契约。正文、媒体、草稿原文与自主策略不进入互动事实库。Corpus 的内容采集存储有独立职责，不能把其内容持久化能力推导为互动事实库的许可。

## SDK 来源记录与当前校验

`scripts/xhh_sdk/SNAPSHOT.json` 是历史来源记录（provenance），不是永久运行时冻结契约。当前 `main` 的 `tests/verify_portable.py` 仍逐文件比对该记录中的历史 SHA-256。这是现存测试约束，本切片不修改该脚本或 SDK。

该校验通过只说明当前文件符合历史摘要。历史摘要不能充当动态产物完整性证明。取消历史摘要约束、验证打包产物或调整 Windows 行为需另行授权。PR #21 的打包与 Windows 修复不属于本切片。

## 协作与验证入口

- [Issue 交接约定](issue-tracker.md) 记录范围、阶段门禁与待审证据。
- [分诊标签约定](triage-labels.md) 记录下一位处理者及暂停条件。
- `.agents/skills/verify-xhh-tools/SKILL.md` 提供无账号离线验证步骤。
- `.agents/skills/verify-xhh-tools/features/README.md` 提供真实命令、源码符号与既有测试的 Feature Map。

`tests/verify_portable.py` 的 `COPY_FOLDERS = ("scripts", "tests", "references", "docs")` 不包含 `.agents/`。因此本目录引用验证 Skill 时使用行内代码路径，保持便携副本的 Markdown 链接可解析。

## 保留的缺口

根 `AGENTS.md` 因常设禁令仍缺失。本切片也禁止创建、编辑或覆盖 `CLAUDE.md` 与 `bot.json`。代理入口缺口须由另行授权的任务处理，不能宣称仓库工作流已完全补齐或 clean。

本地文档与离线验证证据不解锁 P1-2、P1-3 或 Phase 2、Phase 3，也不继承其他任务的授权。
