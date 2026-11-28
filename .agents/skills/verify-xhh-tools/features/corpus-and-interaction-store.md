# 验证 Corpus 与互动事实库

先完成 [验证 Skill](../SKILL.md) 的 Launch。Corpus 的合成数据库使用单独的仓外 `--data-dir`，与零状态发现路径分开。

## 当前源码与测试映射

| 行为 | 当前入口与参数 | 源码符号 | 既有测试 |
| --- | --- | --- | --- |
| 合成计划与状态 | `corpus plan <manifest>`、`corpus status <run_id>` | `scripts/corpus_cli.py:register`、`dispatch`；`scripts/corpus_model.py:CorpusManifest.load`、`CorpusManifest.from_value`；`scripts/corpus_store.py:CorpusStore.plan`、`CorpusStore.status` | `tests/test_corpus_model.py` |
| 预算、续跑与阻塞 | `corpus run/resume`，`--max-requests`、`--max-seconds`、`--max-new-records`，resume 的 `--acknowledge-block` | `scripts/corpus_cli.py:register`、`dispatch`；`scripts/corpus_store.py:CorpusStore.acquire_lease` | `tests/test_corpus_budget.py`、`tests/test_corpus_runner.py` |
| 互动范围与读取 | `interaction status/history/sync <link_id>`，`--peer`、`--root`；history 与 sync 的 `--limit`、`--resume` | `scripts/xhh.py:parser`、`main`；`scripts/interaction_history.py:Scope`、`InteractionHistory` | `tests/test_interaction_cli.py`、`tests/test_interaction_history.py` |
| 同步预算 | `interaction sync` 的 `--pages`、`--requests`、`--seconds`、`--sort` | `scripts/xhh.py:parser`；`scripts/interaction_reads.py:synchronize` | `tests/test_interaction_cli.py`、`tests/test_interaction_semantics.py` |
| 仅元数据事实与回执 | 既有测试的合成 Journal 与数据库检查 | `scripts/operations.py:Journal.execute`、`Journal.settle`；`scripts/interaction_history.py:receipt_metadata`、`message_manifest` | `tests/test_interaction_metadata.py`、`tests/test_operation_receipt_compatibility.py` |

`run`、`resume` 与 `interaction` 的行为在这里仅由既有 fixture 测试覆盖。它们不是无账号离线探针，不在本切片直接执行线上调用。

## 合成 Corpus plan 与 status

保存下列仓外 manifest。`fixture` 和 `101` 仅为合成账号标记，plan 与 status 不登录或验证其真实性。

```json
{
  "schema_version": 1,
  "run_id": "offline-plan",
  "account": {"alias": "fixture", "uid": "101"},
  "reader": {"protocol": "web", "page_size": 5},
  "limits": {"requests": 2, "seconds": 10, "unique_records": 5},
  "queries": [{
    "query_id": "synthetic-search",
    "kind": "search",
    "target": {"query": "合成检索"},
    "filters": {},
    "limits": {"pages": 1, "new_records": 5}
  }]
}
```

1. 执行 `python -I -X utf8 -S scripts/xhh.py --data-dir "<CORPUS_DATA_PATH>" corpus plan "<MANIFEST_JSON>"`。退出码为 `0`，JSON `data.action` 为 `plan`。
2. 执行 `python -I -X utf8 -S scripts/xhh.py --data-dir "<CORPUS_DATA_PATH>" corpus status offline-plan`。退出码为 `0`，JSON `data.action` 为 `status`。
3. 比对两次输出的 `coverage.manifest_sha256` 与 run 身份。该摘要绑定本次合成 manifest，区别于 SDK 的历史来源摘要。
4. 保留仓外数据库与日志用于复测。此步骤有显式本地状态写入，不能纳入 CLI 发现与预览的零状态写入结论。

## 互动事实的隐私契约

执行全量 `unittest discover`，保留元数据测试的原始结果。`tests/test_interaction_metadata.py` 检查合成正文与媒体标记不进入数据库字段、返回元数据或数据库字节，并检查旧内容存储的清理。`tests/test_interaction_history.py` 与 `tests/test_interaction_cli.py` 覆盖账号范围、回执投影和读取关系。单测只证明 fixture 行为。

`operations.sqlite3` 保存 `Interaction Fact`、`Operation Receipt` 与写入元数据，不保存正文、媒体、草稿原文或 `Autonomy Policy`。策略分离以 [ADR-0002](../../../../docs/adr/0002-policy-and-fact-store-separation.md) 为准，既有存储细节见 [互动存储](../../../../docs/INTERACTION_STORAGE.md)。Corpus 的内容数据库不改变该隐私边界。

真实账号互动历史、身份验证、同步覆盖与平台写入回执为 `not_tested`。本切片不新增自主策略、熔断或 `xhh ops` 工作流代码。
