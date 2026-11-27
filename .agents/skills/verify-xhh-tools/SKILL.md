---
name: verify-xhh-tools
description: 验证小黑盒工具的离线 CLI 契约、文档链接或隔离便携性时使用。建立仓外 Python 环境，运行既有测试和合成输入，记录原始证据与未覆盖项。
---

# 验证小黑盒工具的离线契约

仅执行无账号离线验证与既有合成测试。按 [Feature Map](features/README.md) 选择真实入口，以 [领域索引](../../../docs/agents/domain.md)、[ADR-0001](../../../docs/adr/0001-independent-sdk-fork.md) 与 [ADR-0002](../../../docs/adr/0002-policy-and-fact-store-separation.md) 核对边界。

## Launch

1. 核对任务授权、当前独立 Worktree、分支、HEAD 和刷新后的 `origin/main`。记录与规格基线的差异。
2. 在仓库树外创建本次证据目录，保存环境、合成 JSON、临时副本与原始日志。
3. 使用已核对的 Python 3.10+ 创建仓外虚拟环境。优先复用 pip cache，安装当前 `requirements-test.txt` 与 `requirements-formatting.txt`。
4. 记录 Python 精确版本、解释器路径、`pip freeze` 和安装退出码。
5. 对每个验证进程清除全部 `XHH_*` 与 `PYTHONPATH`。只注入 `XHH_DATA_DIR=<ISOLATED_DATA_PATH>`，该路径必须是仓外尚不存在的空状态路径。

下文 `python` 指该隔离虚拟环境的解释器。将临时文件操作限制在本次证据目录。测试临时目录和生成缓存也需外置或在验收前清理本次生成物。

## Doctor 与离线用例

在仓库根目录执行，并为每条命令分别保存 stdout、stderr 和精确退出码。SA-03 必须在最终本地 commit 后执行全套命令。

```console
python -I -X utf8 -m unittest discover -s tests -v
python -I -X utf8 tests/verify_portable.py
python -I -X utf8 tests/verify_skill_docs.py
python -I -X utf8 -S scripts/xhh.py --help
python -I -X utf8 -S scripts/xhh.py --protocol web doctor --offline
python -I -X utf8 -S scripts/xhh.py describe
python -I -X utf8 -S scripts/xhh.py --data-dir "<ISOLATED_DATA_PATH>" preview-post "<TEXT_SPEC_JSON>"
python -I -X utf8 -S scripts/xhh.py --data-dir "<ISOLATED_DATA_PATH>" preview-post "<HTML_SPEC_JSON>"
```

以上命令预期退出码均为 `0`。文本与 HTML 的输入和输出断言见 [CLI 与预览](features/cli-offline-and-preview.md)。Doctor 必须显示未检查账号、未执行签名器，不能推导登录或线上发布能力。

需要 Corpus 计划或互动存储契约时读 [Corpus 与互动事实库](features/corpus-and-interaction-store.md)。需要目录查询或消息准备时读 [表情与 compose](features/emojis-and-compose.md)。Corpus plan 允许写入单独的仓外合成存储，不属于上述零状态写入断言。

## 负向控制

1. 检查离线命令使用的状态路径仍不存在且文件数为 `0`。路径被创建也算失败。
2. 使用 [Markdown 合成输入](features/cli-offline-and-preview.md)，在 `-I -S` 下运行 `preview-post`。预期退出码为 `1`、JSON `ok: false` 且错误指向 `requirements-formatting.txt`。
3. 在仓外临时副本中删除被引用的 `references/formatting.md`，运行 `python -I -X utf8 tests/verify_skill_docs.py "<TEMP_COPY>"`。预期退出码为 `1`，`broken_links` 必须精确指向该缺失目标。
4. 扫描全部新增文档，确认没有绝对宿主路径、真实身份与凭据或任务禁用词。核对每个 CLI 参数、源码符号和测试路径真实存在。
5. 核对 `git diff --name-status origin/main...HEAD` 恰好为任务允许的八个 `A` 文件。`git diff --check origin/main...HEAD` 必须退出 `0`，`git status --porcelain=v1` 必须为空。

所有负向控制保存原始结果。退出码符合预期之外，还必须核对失败原因。

## 当前便携性校验的边界

SDK 已独立 Fork，`scripts/xhh_sdk/SNAPSHOT.json` 只记录历史来源（provenance）。当前 `main` 的 `tests/verify_portable.py` 仍有历史摘要校验约束。本切片保持该脚本原样，历史摘要匹配不证明动态产物完整性。

`COPY_FOLDERS = ("scripts", "tests", "references", "docs")` 不复制 `.agents/`。`docs/agents/*.md` 到本 Skill 的引用必须保持反引号行内代码路径。仓库根的文档检查覆盖本 Skill 的相对链接，便携副本检查只覆盖实际复制的文档。

PR #21 的打包与 Windows 修复不属本切片。这里使用既有 `scripts/xhh.py`，不假定已存在打包后的 `xhh` 或路线图中的 `xhh ops` 入口。

## 失败诊断与交接

测试失败时保存原始日志、环境和退出码，停止验收并回报协调者。不得修改既有测试、依赖、SDK、计数或历史摘要来绕过失败。

报告写明完整 commit SHA、分支、Worktree、核对基线、变更清单、每条命令结果与证据路径。SA-03 只做本地待审提交，不推送或创建 PR。

真实登录、发帖、评论、公开可见性、创作者计划审核、App 签名器在线调用、Issue #12 与 PR #21 打包及 Windows 修复均为 `not_tested`。离线合成证据不替代这些线上验收。

根 `AGENTS.md` 因常设禁令仍缺失，`CLAUDE.md` 与 `bot.json` 也禁止创建或修改。交接保留代理入口缺口，不宣称仓库工作流已完全补齐，不解锁其他阶段。
