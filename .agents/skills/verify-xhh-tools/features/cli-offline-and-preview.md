# 验证 CLI 发现与帖子预览

先完成 [验证 Skill](../SKILL.md) 的 Launch。以下状态路径与合成 JSON 均位于仓外证据目录。全局 `--protocol` 与 `--data-dir` 放在子命令之前。

## 当前源码与测试映射

| 行为 | CLI 参数 | 源码符号 | 既有测试 |
| --- | --- | --- | --- |
| 根命令发现 | `--help` | `scripts/xhh.py:parser` | `tests/test_agent_schemas.py`、`tests/test_tools.py` |
| Web 离线 Doctor | `--protocol web doctor --offline` | `scripts/xhh.py:main`、`scripts/xhh_sdk/cli.py:main` | `tests/test_tools.py`、`tests/verify_portable.py` |
| 命令索引与 schema | `describe`、`describe --command compose` | `scripts/agent_commands.py:register`、`describe`、`command_definitions` | `tests/test_agent_schemas.py` |
| text 与 html 帖子预览 | `preview-post <spec_file>`，可选 `--public` | `scripts/xhh.py:preview_command`、`scripts/postspec.py:preview_post` | `tests/test_post_preview.py`、`tests/test_postspec.py` |
| Markdown 格式依赖诊断 | 同一 `preview-post` 入口 | `scripts/postspec.py:_markdown_module`、`renderer_version` | `tests/test_post_preview.py`、`tests/verify_portable.py` |

## 准备合成输入

将下列对象分别保存为仓外 UTF-8 JSON 文件。输入不含账号、媒体或平台目标。

Text 输入为：

```json
{"content":"第一行\n第二行","content_format":"text","original":false}
```

HTML 输入为：

```json
{"content":"<p>已有 HTML</p>","post_format":"article","content_format":"html","original":false}
```

Markdown 负向输入为：

```json
{"content":"# 合成标题","post_format":"article","content_format":"markdown"}
```

## 执行与断言

1. 执行 `python -I -X utf8 -S scripts/xhh.py --help`。退出码为 `0`，帮助列出 `preview-post` 与 `comment`。
2. 执行 `python -I -X utf8 -S scripts/xhh.py --protocol web doctor --offline`。退出码为 `0`，`signer_ready: true`、`java_required: false`、`account_checked: false`、`signer_executed: false`。
3. 执行 `python -I -X utf8 -S scripts/xhh.py describe`。退出码为 `0`，`data.commands` 含 `compose`，`data.schema_lookup` 指向 `describe --command <name>`。
4. 对 text 与 html 文件分别执行 `python -I -X utf8 -S scripts/xhh.py --data-dir "<ISOLATED_DATA_PATH>" preview-post "<SPEC_JSON>"`。退出码为 `0`，JSON `ok: true`。
5. 解析 text 结果的 `data.payload.text`，首块为 `{"type":"text","text":"第一行\n第二行"}`。HTML 结果的 `data.payload.text` 保留 `<p>已有 HTML</p>`。
6. 核对两次预览的 `coverage.network: "not_used"`、`account_checked: false`、`operation_id_reserved: false`、`public_visibility: "not_checked"`。
7. 用同一命令处理 Markdown 负向输入。`-S` 屏蔽已安装的可选格式依赖，预期退出码为 `1`、JSON `ok: false`，`error.message` 指向 `requirements-formatting.txt`。
8. 核对状态路径仍不存在，文件数为 `0`。预览输出只保存到仓外日志，不进入账号状态存储。

Doctor 的输出是离线运行条件，帖子预览是准备阶段载荷。两者不证明真实登录、发帖、公开可见性或 App 签名器在线调用。
