# 验证原生表情与本地消息准备

先完成 [验证 Skill](../SKILL.md) 的 Launch。普通目录查询与不含图片的 compose 使用捆绑目录，不请求账号或执行平台写入。

## 当前源码与测试映射

| 行为 | 当前 CLI 参数 | 源码符号 | 既有测试 |
| --- | --- | --- | --- |
| 有界原生目录与搜索 | `emojis list`、`emojis search <query>`；`--limit`、`--usage`、`--avoid`、`--offset` | `scripts/agent_commands.py:register`、`execute`；`scripts/emoji_catalog.py:Catalog.search`、`Catalog.info` | `tests/test_emoji_catalog.py`、`tests/test_agent_schemas.py` |
| 精确目录条目 | `emojis get <emoji_id>`，离线流程不加 `--preview` | `scripts/emoji_catalog.py:Catalog.get`、`Catalog.render` | `tests/test_emoji_catalog.py` |
| 本地消息准备 | `compose <spec_file>` 或 `compose --spec-json <json>`；`--dry-run`，可选 `--out` | `scripts/agent_commands.py:register`、`execute`；`scripts/messages.py:compose`、`draft_schema`、`prepared_schema` | `tests/test_agent_schemas.py`、`tests/test_agent_messages.py` |
| 文档链接验证 | `tests/verify_skill_docs.py [root]` | `tests/verify_skill_docs.py:check`、`local_link_targets`、`frontmatter` | `tests/test_skill_docs.py` |
| 隔离便携性验证 | `tests/verify_portable.py` | `tests/verify_portable.py:copy_skill`、`main` | 内部重跑全量 unittest 与无账号离线探针 |

`--limit` 为 `1..100`，`--offset` 非负，`--usage` 为 `post`、`comment` 或 `reply`，`--avoid` 可重复。`compose` 的两个输入来源互斥。以上约束以 `register` 的 parser 为准。

## 查询与准备步骤

1. 执行 `python -I -X utf8 -S scripts/xhh.py emojis list --limit 2 --offset 0`。退出码为 `0`，结果为有界目录页。
2. 执行 `python -I -X utf8 -S scripts/xhh.py emojis search "无奈" --usage reply --limit 2 --avoid sarcasm`。退出码为 `0`，结果带关键词排序依据。`rank_score` 不是概率。
3. 从列表中取真实返回的 `id`，执行 `python -I -X utf8 -S scripts/xhh.py emojis get "<EMOJI_ID>"`。退出码为 `0`，返回精确条目及目录来源。
4. 将该 ID 写入仓外 typed spec，例如 `{"kind":"post","content":[{"type":"text","text":"合成消息"},{"type":"native_emoji","id":"<EMOJI_ID>"}]}`。
5. 执行 `python -I -X utf8 -S scripts/xhh.py compose "<TYPED_SPEC_JSON>" --dry-run`。退出码为 `0`，`data.dry_run: true`、`coverage.platform_write: false`、`data.public_visibility: "not_checked"`。
6. 比对 `data.preview.native_emoji_ids` 与 `data.expected.emoji_ids`，确认使用目录 ID 而非猜测原生标记。
7. 核对状态路径仍不存在。若任务要求 `--out`，仅输出到明确指定的仓外文件，并单独记录该文件写入。

`emojis get --preview` 会下载官方 sprite，`emojis refresh` 会访问官方资源，`compose --preview-file` 也可能请求 sprite。这些参数不属于此离线流程。图片贴纸导入与图片预览通过已有合成单测覆盖。

`send` 与 `verify` 是另行授权的账号流程。本切片不直接执行它们。原生 span 与 shortcode 的编码、图片贴纸的目标限制，以 [原生表情参考](../../../../references/EMOJIS.md) 和既有测试为准。fixture 成功不证明客户端图标呈现或公开可见性。

## 文档与便携性门禁

1. 执行 `python -I -X utf8 tests/verify_skill_docs.py`。退出码为 `0`，`ok: true`，没有 `broken_links`、缺失 frontmatter 或未索引的 references。
2. 执行 `python -I -X utf8 tests/verify_portable.py`。退出码为 `0`，隔离副本通过离线探针、文本与 HTML 预览、缺依赖诊断、链接检查和全量 fixture 测试。
3. 在单独仓外副本删除 `references/formatting.md` 后重跑文档检查。退出码为 `1`，`broken_links` 精确报告该缺失目标。

当前便携校验仍比对 SDK 历史摘要。本切片不改该约束，不把 `sdk_snapshot_verified` 当作动态产物完整性证明。`.agents/` 未被复制，`docs/agents` 通过行内代码引用本 Skill，仓库全量文档检查覆盖本 Skill 的链接。

根 `AGENTS.md` 因常设禁令仍缺失。文档门禁通过不表示仓库工作流完全补齐，PR #21 的打包与 Windows 修复仍为 `not_tested`。
