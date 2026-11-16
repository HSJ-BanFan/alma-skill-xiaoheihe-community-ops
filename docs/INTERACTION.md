# 互动接口参考

互动记录保存可验证的评论 ID、作者 UID、回复关系、交互类型、时间、来信目标与发送证据。持久化记录不包含帖子正文、评论全文、图片、媒体 URL 或原始通知 JSON。调用方决定继续讨论的范围、正文和风格。全部新命令使用现有 `{version, ok, kind, data, coverage, error}` 信封，`version=2`、`kind=interaction`。

## 本地查询

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> --data-dir <dir> interaction status <post-id> --peer <uid> --root <cid>
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> --data-dir <dir> interaction history <post-id> --peer <uid> --root <cid> --limit 20
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> --data-dir <dir> interaction history <post-id> --peer <uid> --root <cid> --limit 20 --resume <coverage.resume>
```

`--peer` 和 `--root` 可省略。POST 接受帖子 ID 或现有 CLI 支持的标准链接。账号选择沿用全局 `--account`、`--config` 或 `--env`。查询使用配置中的 UID 定位本地记录，不构造网络客户端，也不核验当前登录。

`status` 返回该范围的已知计数、`pending`、`unresolved_operations`、`peer_summaries`、祖先缺口和来源覆盖。`history` 的 `data.items` 返回交互类型、作者 UID、帖子与根楼、评论 CID、直接回复 CID 和 UID、平台创建时间、本地观察时间、字段证据以及关联操作 ID。缺少平台创建时间时 `created_at=null`，回执时间不会充当创建时间。需要原文时，用这些 ID 调用 `post` 或 `thread --around <CID>`，读取当下的内容。

`items.interaction_type` 为 `comment`、`reply` 或关系不足时的 `unknown`。帖子级的 `known_actions` 单独列出 `like`、`favourite` 和 `unfavourite` 的操作 ID、账号、目标、发送状态与时间。它们不增加评论数或讨论轮数，也不代表当前点赞或收藏状态。peer 和 root 查询只返回属于该讨论范围的记录。

历史默认从较新消息开始，优先按平台创建时间排序。没有创建时间时使用回执时间或首次观察时间，`order_time_source` 标明依据；`created_at` 仍保持 null，刷新时间不参与排序。相同排序时间按数值 CID 降序稳定分页。`--limit` 默认 20，范围 1 至 100。续读绑定 UID、帖子、peer、root 和排序，页大小可改变。`has_more_local` 只描述已知本地记录，`coverage.resume` 为 null 表示本地这一页已结束。分页期间新出现的较新记录在下一次首屏查询取得；分页不是平台全量历史证明。

peer 筛选保留自己的开场评论和已知祖先的关系记录。仅为说明回复链而加入的其他用户祖先标记 `context_only=true`，不增加该 peer 的消息数、待处理集合或完成轮数。

本地查询可先恢复已提交回执的派生记录。恢复使用原目标及回执中的精确 CID，不发送 POST。`projection_complete=false` 保留恢复缺口。数据库不存在时查询返回空的已知记录与 `participation=unknown`，不会创建目录或 SQLite 文件。帮助及离线 doctor 同样不创建状态。

## 参与与待处理证据

| 字段或状态 | 定义 |
| --- | --- |
| `participation=confirmed` | 有自己的评论列表记录，或公开观察明确给出自己的作者 UID |
| `participation=submitted` | 有可信提交回执，尚无上述参与确认 |
| `participation=uncertain` | 有未解决发送操作，尚无可靠参与记录 |
| `participation=unknown` | 本地已知范围不足，不能证明从未评论 |
| `my_known_comment_count` | 该范围内具有自己 UID 的不同 CID，包含回执派生记录 |
| `my_publicly_observed_comment_count` | 公开读取明确观察到自己作者 UID 的不同 CID |
| `completed_round_count` | 不同对方 CID 被自己的公开精确回复覆盖的数量，公开证据须包含自己的 UID 和直接回复关系 |
| `count_semantics` | 固定为 `public_verified_lower_bound`，完成轮数是当前公开证据的下界 |
| `discussion_status` | `confirmed` 表示有与其他 UID 的明确直接互动，`unknown` 表示证据不足 |
| `relation_completeness` | `partial` 表示关系仍有缺口，`complete_for_known_comments` 只覆盖已知评论，`unknown` 表示缺少可判定记录 |
| `own_reply_relations_complete` | 已知自己的回复是否都有明确直接 CID 和目标 UID；没有已知回复时为 null |
| `unattributed_own_comment_count` | 目标 UID 未知的已知自己的评论数，peer 查询也保留这些可能相关的记录数量 |
| `awaiting_my_reply` | 结构化来信或明确目标尚无自己的回复或在途操作 |
| `awaiting_public_readback` | 已有提交回执，精确作者和回复关系的公开读回未确认 |
| `replied_visibility_unverified` | 自己的评论列表已给出回复，公开证据不足 |
| `write_uncertain` | 相关原操作仍为 reserved 或 uncertain |
| `answered` | 自己的精确直接回复已在公开读取中观察到 |

`pending` 是来信及明确目标的处置列表，其中包含已 answered 的条目。普通同帖评论不会全部进入待回复集合。已验证个人回复收件箱中的结构化 A/B 评论对是来信证据；A 的作者 UID 仍须来自明确用户字段，B 的作者不会仅因处于收件箱就被写成自己。

自己的评论列表中已有明确回复 CID 时，该目标也进入处置列表，状态为已有回复或待公开读回；即使尚未取得对方节点，也不会把自己的已知回复当作未发送。

一条对方输入收到多个自己的回复，仍只计一个完成回合。重复页、变化的通知 ID、重复操作和旧快照不会增加计数。root 筛选要求对方输入属于该根楼；peer 筛选要求对方作者是该 UID。缺少作者、父节点或一致根楼时保留缺口，`ancestry_complete` 与 `history_complete` 分别描述关系及历史范围。

旧评论可以同时返回 `participation=confirmed`、`completed_round_count=0` 和 `own_reply_relations_complete=false`。这表示已经参与，当前缺少足以验证完成回合的直接关系。`unattributed_own_comment_ids` 列出尚不能分配给具体 peer 的自己的评论，peer 查询仍保留这个不确定集合。明确的 `reply_to_uid` 可以说明互动对象，公开轮数仍要求现有严格证据。

子评论接口可能返回完整直接回复链，却没有根楼本身。此时自己的直接关系可以完整，`own_reply_relations_complete=true`，同时 `ancestry_complete=false`。完成轮数继续保持下界标签，`history_complete=false`；读取根楼原文时再通过 `thread` 定位上下文。

`public_seen_at` 表示曾在公开帖子评论入口观察到该 CID，不保证当前仍可见，也不证明匿名访客可见。有限分页中未再出现某个 CID 不推断删除。

`discussion_status=confirmed` 可以来自已验证个人接口中的明确直接关系。它确认互动对象，公开读回和完成轮数分别由 `public_seen_at` 与 `completed_round_count` 给出。

完成轮数与 answered 还要求自己回复和对方输入具有一致的已知根楼。祖先完整性会逐个核对中间节点及最终根楼的归属；根楼筛选不会隐藏矛盾关系。矛盾根楼或循环祖先保留关系缺口，不作为已完成证据。

`unresolved_operations` 独立于来信状态。后来出现一条可见回复可以把来信标为 answered，原未知操作仍保留在未解决列表及 `unresolved_operation_ids` 中。类似正文、相近时间或同目标都不能把未知操作改成 returned。

同一来信的自动操作 ID 为 `incoming-<CID>`。通知 ID 改变仍沿用这个 ID。独立的用户新操作可以使用新 ID，工具允许在同一帖子合法再次评论。

## 自动记录的身份边界

`identity_established` 表示该 UID 的本地互动命名空间曾经在线核验，`identity_last_verified_at` 是历史核验时间。`source.identity_verified_this_read` 表示本次读取是否核验当前登录，兼容的 `source.identity_verified` 保持同一含义。这些字段不能互相替代。

正向身份核验可来自 `account-info`、评论或回复写前核验、个人收件箱、自己的评论列表、带身份核验的在线浏览或 `interaction sync`。身份来源只保存核验证据，不保存账号凭据。账号昵称与内容 ID 不建立作者身份。

`inbox` 和 `my-comments` 每次先核验实际 UID，再读取个人数据。原始 inbox 的 `entries` 或旧 `messages` 列表、完整文本、未读统计及其他消息字段保持原契约。错误列表、非对象消息、错误正文类型或明确的 own-comment 作者错配返回错误。缺少帖子归属的个人记录保留原始输出，并在导入 `gaps` 中标记，工具不猜帖子。

普通公开 `post`、`comments` 和 `thread` 不增加身份核验请求。若已有可信 UID 命名空间，它们自动补充公开观察，并标记当前身份未核验；这不证明当前 Cookie 属于该 UID。没有可信命名空间时返回公开/config-only 数据及未知本地摘要，不建立个人历史。`post --requests 1` 仍只发一次 HTTP。帖子同一响应中的评论也参与自动记录，无需单独再读一次。

独立 `replies --post-id` 的帖子参数是调用方声明。自动导入需要该 UID 下已有根楼到帖子的本地可靠归属；不满足时返回 `root_membership_not_established`，原公开输出仍保留。`thread` 和同步的子评论来自已经在帖子入口观察到的根楼。

每个有效读取页先保存事实，再推进网络续读。普通浏览的 `coverage.interaction_capture` 报告保存结果，失败时标记 `interaction_cache_incomplete` 并保留读取内容。旧快照离线展示或未交付输出重放不把旧观察当作新平台事件。快照平台差异计算排除本地参与摘要。

`post.data.interaction` 与 `feed`、`hot` 每个条目的 `interaction` 包含轻量参与、计数及上述下界和完整性标记，不展开 peer 关系记录或待处理集合。信息流使用批量本地查找，不额外发 HTTP。已知零条与 unknown 都不能作为从未评论的依据。

本地参与摘要读取失败时，保留成功取得的帖子或信息流正文。摘要标记 `cache_incomplete=true`、计数为 null，不能据此断言尚未参与。同步的身份保存失败则返回 storage_error，保留原续读位置和此前本地状态。

## 有界同步

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> --data-dir <dir> interaction sync <post-id> --requests 10 --seconds 30 --pages 10
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> --data-dir <dir> interaction sync <post-id> --requests 10 --seconds 30 --pages 10 --resume <coverage.resume>
```

`sync` 按所选协议先核验 UID。已有已验证评论列表记录时，优先读取这些记录指向且仍需关系或公开证据的根楼。工具保留直接缺少 peer 的候选，不因 `--peer` 提前排除它们。没有候选时，先各读一页最新收件箱和自己的评论，再读取目标帖子的根楼与必要深楼；新评论列表发现的候选也先于无关帖子页处理。目标读取完成后，用保存的私有 offset 交替补读旧收件箱与评论页。个人接口是账号级列表，导入筛选到指定帖子；peer 与 root 缩小查询摘要，root 还限制需要深读的讨论分支。每次同步只读取，不发送评论、不修改已读状态，也不切换协议或账号。

| 参数 | 默认与范围 |
| --- | --- |
| `--requests` | 10，1 至 100，身份核验计一次 |
| `--seconds` | 30，1 至 100，覆盖签名与网络接收 |
| `--pages` | 10，1 至 100，各来源读取合计，身份核验不计页 |
| `--limit` | 20，1 至 100，私有列表及根楼页大小 |
| `--sort` | `time_aes`，可选 `hot`、`time_aes`、`time_desc` |
| `--peer`、`--root` | 可选 UID 或 CID |
| `--resume` | 同一账号与查询的续读令牌 |

续读绑定 UID、协议、帖子、peer、root、limit 和 sort。请求、时间及总页数预算可以改变。非法或错配令牌在 HTTP 之前拒绝。公开根楼续读要求已有根楼记录；`known_replies` 续读可以使用该 UID 已验证评论列表中的明确根楼引用。引用必须属于所选帖子与根楼范围，已观察到冲突帖子归属时拒绝。引用只授权有界补读，不生成根楼记录，也不扩大独立 `replies --post-id` 的导入权限。断点保留来源、offset、根楼页号、待补读根楼和输入游标；每页事实先保存，再推进下一位置。

候选与本轮已检查根楼各保留最多 100 个 ID。达到上限时 `gaps` 返回 `known_root_candidates_truncated` 或 `known_roots_truncated`；可以用 `history` 取得需要的根楼 ID，再通过 `--root` 缩小后续补读。`known_done` 只记录本轮已经检查的根楼，用于续读去重。新的独立同步会重新检查需要补全的记录。

引用的依据消失或出现冲突时，旧续读令牌在 HTTP 之前返回 `XhhConfigError`。现有事实保留，重新运行不带 `--resume` 的同步会按当前有效依据重新选择候选。

达到预算时保留 `request_budget`、`time_budget` 或 `page_budget` 停止原因及令牌。HTTP 408、5xx、DNS、TLS、超时和连接重置等可重试错误保留此前已读数据及安全续读令牌。验证码、限流、登录失效、畸形后续页和其他不可重试错误产生 `ok=false`、`platform_error`，保留此前已读数据和 `coverage.continuation` 诊断，但不返回续读令牌。重复游标产生 `cursor_stalled`，已经取得的节点仍保留。

保存失败产生 `storage_error` 和 `interaction_cache_incomplete`，`data.unpersisted_page` 在本次响应中保留未能导入事实的完整页，续读位置保持在该页；该原始页不进入互动数据库。当前登录在续读前失效时，保留已有本地事实和断点诊断，但不返回原查询令牌，当前身份标记为未核验。

`coverage.scan_finished=true` 表示本次来源遍历已走到可取得的终点，不表示全量历史已覆盖。没有续读位置、空数组和 own-comment 列表的终点证据不足时仍保留 gaps。第一版的 `history_complete` 始终为 false，`completion=unknown`。新一轮检查最新通知从 offset 0 开始，而不是把旧历史游标作为最新消息检查。

## 保存与恢复

SQLite 位于 `--data-dir/operations.sqlite3`，四张互动表与原 operations 共库。CID、帖子和 UID 使用 TEXT，评论身份为 UID 加 CID，帖子归属不可改写。连接启用 foreign keys、WAL、FULL synchronous 和 10 秒 busy timeout。目录和数据库使用现有本地所有者权限边界。网络不在写事务中执行。

评论 reservation 与帖子、根楼、回复目标和请求指纹同时提交。上传后、评论 POST 前再次按指纹核对保留的目标及正文，并保存提交指纹。正文、图片和完整请求只用于本次发送。目标绑定或提交核对失败时不会继续评论 POST。回执先由 operations 提交，派生评论 ID、关系及投影标记另在一个事务中更新；派生失败不会把 returned 改成未发送。

字段证据区分 public、own_list、inbox 和 receipt，记录结构化字段的来源和观察时间。相同来源按平台编辑版本或观察时间合并；稀疏缺失保留已有事实。旧回执不覆盖后来的平台关系证明，也不把旧创建时间改成当前时间。来源、同步断点和回执只保存允许的结构化字段。

根楼证据额外记录 `caller_context`。独立 `replies --post-id` 的调用方根楼声明不会覆盖已有可信根楼值及其来源；已验证评论列表或可信公开读取可以替换旧的调用方声明。其他公开作者和直接回复字段继续按原证据规则更新。

数据库版本为 2。首次使用实验版本 1 的互动库时，工具在事务中保留 ID、关系、时间、发送指纹与状态，移除旧正文、媒体、原始通知和完整请求列。只有原 operations 表的旧库不通过哈希猜测历史目标。缺少数据库的本地查询仍不会创建状态。SQLite 的删除清理与 WAL checkpoint 会在迁移时执行；持有旧快照的并发读取可能延后 WAL 清理。
