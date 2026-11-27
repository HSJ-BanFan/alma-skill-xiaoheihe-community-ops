# 离线 Feature Map

按用户可观察行为选择验证页。命令与源码映射基于当前 `main`，每次维护都重新核对 `scripts/xhh.py` 的 parser 和对应模块。

| 功能 | 当前入口 | 验证页 | 主要既有测试 |
| --- | --- | --- | --- |
| 无账号发现与运行诊断 | `--help`、`--protocol web doctor --offline`、`describe`、`describe --command <name>` | [CLI 与预览](cli-offline-and-preview.md) | `tests/test_agent_schemas.py`、`tests/test_tools.py` |
| 本地帖子载荷预览 | `preview-post <spec_file>` | [CLI 与预览](cli-offline-and-preview.md) | `tests/test_post_preview.py`、`tests/test_postspec.py` |
| 合成 Corpus 计划与状态 | `corpus plan <manifest>`、`corpus status <run_id>` | [Corpus 与互动事实库](corpus-and-interaction-store.md) | `tests/test_corpus_model.py`、`tests/test_corpus_budget.py`、`tests/test_corpus_runner.py` |
| 互动事实与操作回执 | `interaction status/history/sync` 的既有 fixture 测试 | [Corpus 与互动事实库](corpus-and-interaction-store.md) | `tests/test_interaction_metadata.py`、`tests/test_interaction_history.py`、`tests/test_interaction_cli.py` |
| 原生表情查询与本地消息准备 | `emojis list/search/get`、`compose` | [表情与 compose](emojis-and-compose.md) | `tests/test_emoji_catalog.py`、`tests/test_agent_schemas.py`、`tests/test_agent_messages.py` |
| 文档链接与隔离便携性 | `tests/verify_skill_docs.py`、`tests/verify_portable.py` | [表情与 compose](emojis-and-compose.md) | `tests/test_skill_docs.py` |

具体参数与源码符号见各验证页。`describe` 是紧凑的部分命令索引，`describe --command <name>` 展开已收录入口的 schema。Corpus 和 Doctor 的参数直接核对 parser 与 `--help`，不假定所有命令都出现在 describe 中。

全套执行顺序、环境隔离和失败处理见 [验证 Skill](../SKILL.md)。文档契约见 [领域索引](../../../../docs/agents/domain.md) 与 [路线图](../../../../docs/ROADMAP.md)。

## 验证边界

无账号离线发现、预览和普通目录查询要求零状态写入。Corpus plan 写入单独的仓外合成数据库。互动 CLI 会涉及账号身份与存储，当前只通过已有 fixture 测试覆盖，不直接连接真实账号。

本地图不提供虚构的 ops 或打包入口。独立 SDK Fork 的历史来源摘要不是动态产物完整性证明。便携复制未包含 `.agents/`，全仓链接检查与便携副本检查各自只证明被扫描的文档。

真实登录、发帖与评论、公开可见性、App 签名器在线调用、Issue #12、PR #21 打包及 Windows 修复均为 `not_tested`。根 `AGENTS.md` 因常设禁令仍缺失，本地图不补齐该入口。
