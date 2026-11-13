# 社区工具参考

日常入口和一轮操作见 `SKILL.md`。参数以 `community.py <scan|read|act> --help` 为准。

## 写入结果与停止

例行 `community.py` 返回 JSON。`ok=false`、异常名称、HTTP_5xx 或任意非 `ok` 状态不能证明未发送。

| 结果 | 事实与恢复边界 |
|---|---|
| `returned` | 有效 API CID 已取得；不证明当前可见。读回或本地结算失败仍保留 CID。 |
| `acknowledged` | 有效点赞或收藏 ACK；无 CID，当前平台效果仍为 `not_observed`。 |
| `reserved / uncertain / conflict` | 规范目标保持关闭。修改正文、重读详情或新开轮次不解除发送保留。 |
| 发送前本地 `refused` | 只释放本次公开写 grant，不退已消耗的请求；原因修正后仍须完整详情与图片确认。 |
| `platform_rejected` | 低层识别有限平台拒绝状态，但已经调用发送接口。本入口停止并保留关闭意图，不退款或重发。 |
| `intent_persistence_failed` | JSON 或 SQLite 持久化失败，本轮停止。已知 CID 与失败状态同时返回。 |

`coverage.run_budget` 给出共享用量与 `stop_reason`。返回 `run_stopped` 后不继续其他后端入口。结果未知可能已生效，到对应帖子核对，不重复操作。有限范围未见评论、用户移除评论或进程退出都不是未发送证明。

### 发送意图台账

社区规范意图由 `State` 原子写入账号隔离的 `state/community/<uid>.db`。每个帖子 comment、每条精确 incoming CID reply、每种帖子 reaction 分别有规范目标。

低层 comment 与 reply 的 `state/xhh_write_intents.json` 是兼容关闭记录。实际 UID、动作与精确目标决定键，既有 SDK mutex 保护读改写；未知、sent 及旧 rejected 记录都保留关闭。缺失 UID 的旧目标保持保守阻断，不误归属当前账号。损坏或无法持久化的记录不重置为空。

当前 facade 的 fresh reservation 通过内部 State 与 operation scope 进入原 helper。裸 helper 同样检查旧 SQLite 意图，不能靠同一 PID 绕过 reserved。`pending_intents()` 只查询低层 JSON，不替代 `read op:<operation-id>`。删除、点赞与收藏不经过这份 JSON。

## 收件箱的例行与低层入口

例行更新使用 `scan incoming`，由统一 facade 核对账号、有限 profile、保留楼层与精确回复关系，返回台账和近期公开操作。`read inbox` 是本地查询。下面的消息队列函数只作旧 helper 的按需参考，不替换例行扫描、预算或规范意图。

`fetch_messages()` 直接读 `/bbs/app/user/message`，一次请求拿到
「谁回了我哪句、在哪个帖子的哪层楼」，字段是：

- `comment_a_id` / `text`：来信人和内容（别人说的）
- `comment_b_id` / `my_text`：被回复的那条（我说过的）
- `root_comment_id`：回复要用的根楼层
- `linkid` / `link_title`：哪个帖子

注意正文在 **`result.messages`** 里，不在顶层。读错这一层会永远返回空，
和"真的没消息"长得一模一样。

`check_interactions()` 的帖子反查保留作回退：它要翻一堆帖子详情，还会漏掉
我没评论过的帖子里对我的回复。`inbox_brief()` 自动优先走消息队列。

## 深楼用游标，别一次翻到底

`scan_sub_comments(root_id, pages=3, limit=20, cursor=None)` 顺着 `lastval`
游标往下翻，一轮只翻有限页，返回 `{replies, cursor, done}`：

- `done=True`：到底了；
- `done=False`：把这个 `cursor` 存下来，下次带上去从断点继续。

一次翻到底既慢又容易撞风控，中间断开还得从头再来。实测一个 5 层深的楼：
第一轮 `limit=3` 拿 3 条 `done=False`，带游标第二轮拿 2 条，两轮零重复。

## 楼中楼不传图

小黑盒 App 协议里楼中楼没有图片字段。工具因此对 `reply` 完全跳过图片链路：不预取主帖图、不要求图片确认、不占图片预算。`read i:<cid>` 返回 `coverage.asset_access=reply_text_only`。要给帖子配图只能走顶楼一级评论 `comment`。

一次读取后请用**同一批 `context.keys`** 直接发送，不要为了“刷新”再读一遍——重读会换新 key，旧 key 立刻失效并报 `context_confirmation_required`。

## 分页取齐

`read v:<hash> --page N` 的每页都是完整 JSON，形状为 `{"path":["data","items"],"items":[...]}`。多页结果的 `context.keys` 一次给出全部 page key，逐页传 `--context-key` 即可，不必再用正则从输出里抠 key。合并时按 `path` 把各页 `items` 依次拼回同一位置。

## 资源与状态

| 资源 | 数据 |
|---|---|
| `account` | 配置账号及在线身份核对 |
| `inbox` | 已发现互动、历史处理、已知回复数量及发送证据；`--all` 包含已处理条目 |
| `p:<id>` | 帖子正文、顺序块、图片地址和有限页评论 |
| `i:<cid>` | 来信、根楼层、精确接话对象、当前返回范围和历史处理证据 |
| `c:<cid>` | 当前账号的历史评论或 API 回执及最近有限范围可见性 |
| `op:<operation-id>` | 发送保留、回执、拒绝、结果不明及读回状态 |
| `shortcodes` | 当前官方短码目录 |
| `stickers --query <公开正文>` | 本地表情库候选及资产元数据 |
| `knowledge --query <公开内容>` | 只读词义查询（库由 `alma-meme-watcher` 拥有）及动态信息来源状态 |
| `taste` | 现有兴趣画像；历史占比不是任务配额 |

所有调用返回一行 JSON：`version / ok / kind / account / data / coverage / errors`。读回失败时 `ok` 可能为 `false`，`data` 仍保留已取得的回执和记录。

`inbox` 的 `eligible` 始终是当前可处理数，`displayed` 是本次展示数；`--all` 与 `--limit` 只改变展示集合。统计范围是台账已观察的互动，不是全站总量。

`reply_count_known` 是去重后的已知回复 ID 数；`reply_evidence` 区分 API 回执、精确目标观察与当前可见性。历史观察不等于当前仍可见。`handled` 来自精确回复证据、目标绑定回执或明确的本地 `close`，不由时间先后推断。

`defer` 的决定记录保留，`defer_until` 是决定时间加 24 小时；到期后重新进入可处理集合。到期只恢复展示，不自动发送、不解除结果不明的发送保留。`close` 和已有回复证据没有这个到期机制。处置动作的语义见 `SKILL.md`「来信与处置动作」。

`scan` 返回已观察范围、缺口和 `coverage.resume`；续读用 `scan incoming --resume <值>` 或对应的 `scan feed`。保存过的页内行和未完成子评论任务不会因为本轮预算结束而丢弃。每轮仍是有限范围，`full_history=false`。

公开发送前读取完整详情。结果分页时用 `read v:<hash> --page N` 取齐每页，把所有页的 key 分别传作 `--context-key <key>`。原生排程要求这些页的输出**完整出现在本次 Bash 的 stdout 里**（不要 `tail`、重定向或二次解析）；被 `host_context_not_confirmed` 拒绝时，错误信息会直接列出还缺哪几页。

对来信的发送还要求目标 CID 实际出现在最新返回楼层。`detail_required / context_confirmation_required / target_not_currently_observed / already_handled` 是目标状态；`account_binding_changed` 是账号绑定变化。State 在原子发送 reservation 中重查最新页、CID、图片计划与共享 grant。新详情读取失败也撤销旧授权，不移除历史记录。

## 图片与官方短码

一级评论的 `--image-file` 只选择本轮已发行的 `file` 快照。`--image-url` 只选择最新主帖已发行的 URL，发送时同样解析到快照，不重新下载可变 URL。两者互斥。楼中楼 `reply` 拒绝图片参数，返回 `reply_image_unsupported`。平台文本上限仍为 1000 字符，原始图片上限仍为 10 MB。

`read p:<id>` 准备主帖图片。`read stickers --query <公开正文>` 准备本地候选。每次发行有新的 asset ID、源快照和 `preview_file`；源与预览分别记录完整 SHA256 和字节数。原始动画保留，EXIF 转正后的预览只描述首帧，PNG 最大边不超过 1600、大小不超过 320 KiB。

用会话原生 `Read(file_path=<preview_file>)` 查看所有最新主帖图片，以及所选附图的预览。未挂载时通过会话 `ToolSearch` 查找原生 `Read`。caption、文件存在、upload 回执或读取 source 路径不代替预览字节证据。外部 CLI 和 caller-only 页确认不成为原生视觉证据。

图像 gate 只认本次已完成原生 Read 的预览字节：完整 SHA256 与长度必须和发行预览一致，摘要前缀、缓存名称或残留路径都不算。主帖必需的图片未准备成功时不能发送，也不能跳过。楼层图片不强制，也不声称看过全部。

发送期间快照与预览只读，防止被改写。超时或清理失败不解除未知意图，也不丢已知 CID。证据只到 `host_preview_bytes_verified`，不代表完整动画、上传后像素或模型理解。

图片决定尚未完成时，`act` 可返回本地 `refused` 与候选资产。候选须先经 `read stickers` 发行并用原生 Read 查看对应预览，再传它的 `file`。不选附图时沿用 `--without-image <原因>`。官方短码仍是独立文本，由 `read shortcodes` 目录校验，不依赖图片参数。

## 词义与动态来源

词库由独立 `alma-meme-watcher` 拥有。社区消费的公开入口是 `node D:/02_Dev/Projects/alma-meme-watcher/scripts/lookup.mjs --text=<公开语义文本> --limit 3`。社区不传数据库路径，不导入梗内部模块或读取私有表，不执行采集、导入、补字段或维护。

`read knowledge --query <公开内容>` 与读帖的 `knowledge` 字段返回 `terms`、`available`、`status` 和 `source`。词卡的公开字段为 `term / meaning / usage_context / avoid_when / origin / source_url / aliases / matched_keys / confidence / days_left`。未知旧字段保留空字符串，`days_left=null` 表示该卡不失效；社区不补写字段或生成额外 tone、brief。

公开入口成功无命中与服务失败分开。stdout 有合法单个 `{"terms":[]}` 且退出 0 时 `available=true`。启动失败、非零退出、非空错误输出或协议失败时 `status=knowledge_unavailable`、`available=false`。读帖仍保留正文、台账与完整页恢复入口。接口尚未就绪也用同一降级，社区收尾不等待梗迁移。

动态事实需要公开来源时按需使用现有检索工具；私有源对话不进入检索参数。本入口不宣称全量事实缓存或新建来源库。

## 原生私有上下文

任务专属 `chat.message.willSend` 钩子先核对作业、配置源和 `isCron / cronFor`。boundary 2 另核对固定原生版本，从真实 app 用户提交及规范时区证据选择最近桌面对话，按该用户活动的实际 `created_at` 选择，不以线程更新时间或情绪新鲜度替代。心跳、Cron、后台自动与外部来源按元数据排除；来源未证实、扫描不全或时间并列时保留不可用状态。

选中对话的实际 `activePath` 按序提供 user/assistant 文本，以及原生已确认成功的 AttemptCompletion final result。流式或停止的输出、程序通知与其自动跟随、压缩指示、推理和其他工具不变成对话原文。私有部分用说话人标签与真实换行，保留原始任务附件与脱敏边界。旧 boundary 1 在途作业仍走原配置源路径；组件存在不证明本轮 hook 实际交付。

SOUL、MEMORY 和日常笔记由 Alma 原生层加载。钩子不修改人格、记忆选择或情绪路由。基础情绪保留原生 6 小时窗口；映射情绪与选中桌面 `emotions/context/<source-id>.md` 保留各自原生 2 小时窗口及真实缺失、过期或时区缺口。桌面记录不替换 channel mapping，也不更新 Cron effective emotion。

私有资料只进入原生任务上下文。社区台账、公开活动和词卡查询不保存私聊原文。hook 诊断记录来源、状态和计数，不记录对话或 mood 原文。

## 帖子点赞与收藏

boundary 2 的例行点赞使用 `act p:<id> like --context-key <key> --confirm`，收藏使用 `act p:<id> favourite --context-key <key> --confirm`。每页 key 都分别传入。两者仅针对帖子，不接受正文、图片、金额、充电量、收藏夹或取消收藏选项。

底层原样调用 `xhh_tool.py.send_like(link_id)` 和 `send_favourite(link_id)`。like 是原 like/combo 充电接口，不宣称免费；favourite 保留默认收藏夹与原 helper 的确认参数。一个 send 后端 grant 不等于 helper 内部每个 HTTP 请求。

点赞、收藏与评论共用真实账号绑定、完整详情与主帖图像确认、request 与 write grant、未知结果阻断和已接受的历史归属解析。有效 ACK 记为 `acknowledged`，无 CID、评论根或可见性证明，`effect_verification=not_observed`。不为恢复而改用旧 helper。旧 boundary 1 在途入口仍返回 `reaction_not_active`；部署与启用以实际配置和读回为准。

## 其他平台操作与会话

例行评论、回复、点赞和收藏使用 `community.py`。已有 `send_unfavourite / delete_comment` 等低层函数保留给用户明确授权的独立操作，不是被拦截或结果未知时的恢复入口。

发帖、账号维护沿用 `D:/02_Dev/Projects/self-media-ops/tools/xiaoheihe/cli.py`；登录由用户提供手机号和验证码。检查会话的现有调用：

```bash
python -I -X utf8 "D:/02_Dev/Projects/self-media-ops/tools/xiaoheihe/cli.py" account status alt --online
```

发帖使用该 CLI 的 `--account alt publish - --confirm`，从 stdin 接收 JSON。会话失效交用户处理；修改登录状态和删除是独立的用户操作，不属于发送读回或结果不明时的恢复步骤。

## 自选板块

`scan feed --topics <逗号分隔的ID>`：盒友杂谈 `7214`、校园生活 `549999`、PC游戏 `1`、主机游戏 `23563`、游戏开发 `615783`、独立游戏开发 `807953`、Steam `475516`、手机游戏 `21325`。
