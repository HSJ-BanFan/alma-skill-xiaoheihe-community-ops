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

格式约束：当前 `reply` 入口仅支持纯文本与 `read shortcodes` 目录中的官方短码，图片参数返回 `reply_image_unsupported`。

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

本说明对应 boundary 2 操作合同。作业标记与账号绑定配置决定新排程是否启用，旧 boundary 1 在途排程保留旧入口。代码支持、部署读回与普通排程效果分别核实。

任务专属原生钩子在作业身份核对后选择最近有真实 app 用户活动证据的桌面对话，以说话人标签和换行附加私有数据。来源与情绪缺失或过期仍显示实际状态。SOUL、MEMORY 和日常笔记由 Alma 原生层加载。社区台账保存公开互动，不保存私聊资料。
