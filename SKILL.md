---
name: xiaoheihe-community-ops
description: Use when 用户要求逛小黑盒、读帖子、检查收件箱、评论、回复楼中楼、帖子点赞或收藏；发帖和账号登录走按需参考。
allowed-tools:
  - Bash
  - Read
  - ToolSearch
  - Write
  - WebSearch
  - WebFetch
---

# 小黑盒社区

## 入口

技能目录里的 `community.py` 提供 `scan / read / act`，默认账号 `alt`。统一调用：

```bash
python -I -X utf8 "C:/Users/33308/.config/alma/skills/xiaoheihe-community-ops/community.py" <操作>
```

## 一轮操作

1. `scan incoming` 核对账号并更新互动，返回台账与近期公开操作数据。`read inbox` 只用于额外台账查询，不固定重复 `read account`。查询本身不标已处理。

   `scan incoming` 先导入平台消息队列再反查旧帖。台账结清只表示已发现的条目处理完，不等于平台上没有新来信：`coverage.status=done` 且无 `gaps` 才能说本轮无待办，`partial` 一律按"未查完"表述。`eligible` 可能大于 `displayed`，差额是 `--limit` 截断，不是不存在。
2. 条目是 `i:<cid>`，帖子是 `p:<id>`。`read i:<cid>` 或 `read p:<id>` 返回正文、楼层和精确回复关系。部分结果用 `read v:<hash> --page N` 取齐所有页。主帖图片和所选表情的 `preview_file` 用会话原生 `Read` 查看，未挂载时通过会话 `ToolSearch` 查找 `Read`。
3. 回复文件保存为 UTF-8。读完详情后，每页的 key 分别传作 `--context-key <key>`。`act i:<cid> reply --text-file <文件> --context-key <key> --confirm` 回复该条；`act p:<id> comment --text-file <文件> --context-key <key> --confirm` 发一级评论。根楼层和回复对象由工具解析。原生排程还核对先前已完成的 Bash 结果是否包含完整详情页。
4. `act i:<cid> close` 记录无需继续处理；`defer` 记录暂缓。两者不是公开回复。
5. `scan feed` 浏览默认板块。帖子点赞用 `act p:<id> like --context-key <key> --confirm`，收藏用 `act p:<id> favourite --context-key <key> --confirm`。它们和评论共用账号、详情确认、图片证据与写预算，不带正文、图片、金额或收藏夹参数。

`act` 已保存发送尝试并做有限范围读回。操作可用 `read op:<operation-id>` 查询；历史评论和回执可用 `read c:<cid>` 查询，附 `--refresh` 才联网更新可见性。

`community.py` 返回 JSON。命令失败、HTTP_5xx、异常名称或任意非 `ok` 状态不证明未发送。`reserved / uncertain / conflict` 保持目标关闭，更换正文、页、账号别名或轮次不解除它。只有已证明发生在发送前的本地拒绝可释放该次写入；已经调用发送接口的 `WriteRejected` 也不授权重新提交。

社区规范意图与回执保存在账号隔离的 `state/community/<uid>.db`。旧 helper 的 `state/xhh_write_intents.json` 是兼容关闭记录，按实际 UID、动作和精确目标互斥，不按正文开新意图。无法读取或持久化意图时，本轮停止。已有 CID 或 reaction ACK 与停止原因一起保留。结果未知时到对应帖子核对，不重复操作。

例行收件箱使用 `scan incoming` 的有限 profile、保留楼层及精确目标台账。低层 `fetch_messages` 和 `scan_sub_comments` 是按需参考，不替代规范意图、预算或未知结果保护。细节见 `REFERENCE.md`。

## 来信与处置动作

收件箱台账的每条来信是 `i:<cid>`，`read i:<cid>` 给出正文、根楼层、精确接话对象与图片。来信有三种处置，语义不同：

- `act i:<cid> reply --text-file <文件> --context-key <key> --confirm`：在该楼层公开回复，接话对象由工具解析。
- `act i:<cid> close`：标记无需继续处理，不产生公开内容。
- `act i:<cid> defer`：暂缓，`defer_until` 为决定时间加 24 小时，到期只恢复展示。

`close` 与 `defer` 是本地记录，不是公开回复。已处理条目不会因查询重新进入待办。

格式约束：楼中楼是**纯文本通道**——平台协议没有图片字段。因此 `reply` 不需要图片确认、不预取主帖图片、也不消耗图片预算；传 `--image-file` / `--image-url` 会返回 `reply_image_unsupported`。`read i:<cid>` 的 `coverage.asset_access` 为 `reply_text_only`，表示这是纯文本目标。官方短码仍是文本，楼中楼可以用。

## 操作边界

- 已认证排程共享最多 120 个后端操作请求、最多三次公开写入，相邻写入至少间隔 15 秒。三次是上限，不是评论目标。用量与停止原因以 `coverage.run_budget` 为准。
- `returned` 是 API 回执；`reserved / uncertain` 保留在台账并阻断再次发送。当前有限范围未找到评论，不重开历史已处理状态。
- `captcha_stop / rate_limited / session_expired` 进入停止状态，交用户处理。`coverage` 保留扫描范围、缺口与续读位置，空收件箱不代表全量历史已查完。
- 一级评论可附图，当前入口的楼中楼使用文本。官方短码是独立文本资源，读取 `read shortcodes`；附图参数与短码校验分开。
- 主号限常规游戏、生活、开发范围；其他敏感范围由用户决定。私聊资料留在 Alma 私有上下文中，公开检索与发送的数据来源限可公开资料。

## 梗词边界（2026-11-13 起）

词库由独立插件 `alma-meme-watcher` 拥有。社区只通过其公开 `scripts/lookup.mjs` 消费词卡，不导入梗内部模块或读取私有表，不提供采集、导入或维护入口。

- 查词用 `read knowledge --query <公开内容>`。返回词卡的公开字段为 `term / meaning / usage_context / avoid_when / origin / source_url / aliases / matched_keys / confidence / days_left`，`days_left=null` 表示不失效。本入口不生成额外 `tone` 或 `brief`。
- 成功无命中返回 `available=true` 与空词卡。启动、数据库或协议失败返回 `status=knowledge_unavailable`、`available=false`，读帖的正文、台账与详情页继续保留。
- 词库维护由独立插件处理。接口未就绪不等于读帖失败，也不要求社区完成梗迁移。

## 按需参考

`REFERENCE.md`：图片选择与参数、词义与动态来源、扫描状态、其他平台操作、会话维护。

本说明对应 boundary 2 操作合同。账号绑定配置决定新排程是否启用，旧 boundary 1 在途排程保留旧入口。代码支持、部署读回与普通排程效果分别核实。

## 回复密度

`read i:<cid>` 的返回里带 `relation`，是技能提供的**事实**，不是建议：

```json
"relation": {"replies_in_post": 2,
             "replies_last_hours": {"hours": 24, "count": 2},
             "last_accepted_at": 1794632520.3}
```

- `replies_in_post`：本帖内我回过这个人几次
- `replies_last_hours`：24 小时内我回过这个人几次

**要不要继续接话由调用方判断。** 技能不设每轮条数目标，也不劝收手——但同一帖内来回超过两三轮通常已经没有新东西可说，值得先想一下再发。对方是真人还是另一个 AI 不必先判定；这里限制的是对话密度，不是身份。

## 与宿主的边界

本技能只做平台操作：读帖、读楼层、发评论与楼中楼回复、点赞收藏、维护公开互动台账。**不负责人格、语气、情绪、记忆、调度身份，也不读取宿主的私聊记录。**

下面这些由调用它的 agent 自己提供，本技能既不读也不写：

- **身份与语气**：宿主的人格文件与表达方式，不经由本技能注入。
- **情绪**：情绪状态由宿主自行装配，本技能不搬运、不改写。
- **调度合法性**：宿主在自己的运行环境里验证本轮是否可以写入，验证结果通过 `XHH_RUN_SCOPE` 签名信封传入。
- **长期记忆与日常笔记**：由宿主原生层加载。

### 运行范围信封

宿主若判断本轮受运行预算约束，应设置两个环境变量，本技能验证签名后才采用：

- `XHH_RUN_SCOPE`：Base64 编码的 JSON（`run_id`、`started_at`、`deadline`、`contract_version`、`request_limit`、`write_limit`、`minimum_write_interval`、`account_alias`、可选 `account_uid`、可选 `now`，以及 `signature`）。
- `XHH_RUN_SCOPE_SECRET`：与宿主约定的签名密钥，至少 16 字符。

签名用 `HMAC-SHA256(secret, canoncial_json_without_signature)`，结果写入 `signature`。本技能会强制上限：**请求 ≤120、公开写入 ≤3、相邻写入 ≥15 秒、单轮 ≤3600 秒**，信封不能放宽这些值。

没有信封时按普通交互运行处理；信封缺失、过期、签名不符、参数越界一律拒绝，不猜测。

### 数据位置

台账、证据与资产默认在 `~/.config/alma/state`、`~/.config/alma/scratch`。宿主可用 `XHH_STATE_ROOT`、`XHH_SCRATCH_ROOT`、`XHH_HOST_CONFIG` 改到自己的目录。

技能不保存私聊资料；社区台账只保存公开互动与发送回执。
