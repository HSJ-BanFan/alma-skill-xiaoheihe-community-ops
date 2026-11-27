# 分期路线图

本路线图记录已确认的演进计划；当前可用能力与调用方式以现有入口文档为准。

## 核心定位与范围边界

定位：面向任意 Agent 可直接使用的零负担小黑盒社交运营工具。

- 双层解耦：完整保留全部 `Platform Primitive` 的最小单次读取、查询与写入能力及既有调用兼容性，在其上新增 `Ops Workflow` 运营层。
- 纳入范围：轻互动、选题、发帖工作流与周期复盘。
- 非目标：私信/群聊、多账号矩阵协同。

## 架构与领域索引

- [GLOSSARY.md](../GLOSSARY.md)：统一领域语言。
- [ADR-0001: 协议层从 xiaoheihe-api-collect 独立 Fork 演进](adr/0001-independent-sdk-fork.md)：协议层与运营层的独立演进决策。
- [ADR-0002: 声明式自主策略与仅元数据互动事实库分离](adr/0002-policy-and-fact-store-separation.md)：`Autonomy Policy` 与 `Interaction Fact` 的持久化职责边界。
- [README.md](../README.md)：项目定位、安装与验证入口。
- [SKILL.md](../SKILL.md)：Agent 调用与任务路由入口。
- [REFERENCE.md](../REFERENCE.md)：现有 `Platform Primitive` 命令与认证参考。
- [CONVERSATION.md](../CONVERSATION.md)：连续浏览与多轮讨论流程。
- [互动事实契约](INTERACTION.md)与[互动事实存储说明](INTERACTION_STORAGE.md)：现有结构化事实、身份核验与 `Operation Receipt` 契约。

## 分期演进

### Phase 0：领域语言、架构决策与路线图

当前文档 PR 新增 `GLOSSARY.md`、ADR-0001、ADR-0002 与 `docs/ROADMAP.md`，确立统一领域语言、核心架构决策和分期演进路线，不改动任何代码或运行行为。

### Phase 1：底座可用与跨平台

关联 Issue [#17](https://github.com/HSJ-BanFan/alma-skill-xiaoheihe-community-ops/issues/17)，联动 Web 发布问题 [#12](https://github.com/HSJ-BanFan/alma-skill-xiaoheihe-community-ops/issues/12)。

- 定性 Web 发帖拒绝原因；依据实证修复协议缺口或明确账号与平台能力边界。
- 补齐跨 OS 凭据后端，支持 Windows、macOS 与 Linux。
- 新增 `pyproject.toml` 与统一 `xhh` 打包入口，保留全部 `Platform Primitive`。

### Phase 2：低负担 Ops Workflow 运营层

关联 Issue [#18](https://github.com/HSJ-BanFan/alma-skill-xiaoheihe-community-ops/issues/18)，在 Phase 0 与 Phase 1 收口后按切片推进。

- 新增独立声明式 `Autonomy Policy`，配置社区范围、写入配额与停止条件；四档 `Autonomy Tier` 依次为只读观察、草稿准备（默认）、限额互动与限额公开发布。
- 新增账号级 `Write Circuit Breaker` 与单目标 `Target Write Lock`；平台风控阻止该账号后续写入，结果不明仅锁定对应操作与目标并保留核对入口。
- `Autonomy Policy` 与互动事实库分离；`Interaction Fact`、`Operation Receipt` 和实际写入计数继续遵守仅元数据契约，不持久化正文、媒体或草稿原文。
- 新增 7 个复合 `Ops Workflow` 入口：`xhh ops init`、`xhh ops status`、`xhh ops patrol`、`xhh ops inbox-triage`、`xhh ops reply`、`xhh ops publish`、`xhh ops review`；统一 JSON 信封携带 `policy` 与结构化 `next_actions`。
- 重构 `SKILL.md` 实现渐进披露，以任务路由降低 Agent 调用负担，保留全部 `Platform Primitive` 的调用兼容性。

### Phase 3：缺口协议实证与增量补齐

关联 Issue [#19](https://github.com/HSJ-BanFan/alma-skill-xiaoheihe-community-ops/issues/19)，在核心 `Ops Workflow` 闭环可用后推进。

- 实证验证并增量接入点赞/充电、用户关注相关 `Platform Primitive`。
- 实证接入创作者数据，作为周期复盘 `Ops Workflow` 的增量数据源。
- 能力说明与后续实现以真实协议证据和可复用验证为依据。

## 文档校验

`tests/verify_skill_docs.py` 扫描全仓所有 Markdown 文档的本地相对链接。新增或调整索引时，链接必须指向仓库内真实存在的文件；GitHub Issues 使用完整 URL。

本地验证执行完整测试集、便携性校验与文档检查：

```console
python -m unittest discover -s tests -v
python tests/verify_portable.py
python tests/verify_skill_docs.py
```
