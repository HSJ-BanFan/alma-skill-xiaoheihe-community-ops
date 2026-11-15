---
name: xiaoheihe-tools
description: 独立的小黑盒社区工具，提供社区发现、信息流、搜索、正文、评论、账号和写入功能，以及有预算和 checkpoint 的多查询 corpus。用户需要读取或操作小黑盒时使用。
---

# 小黑盒工具

本技能提供平台工具。调用方决定内容、语气、是否互动和执行时间。

## 调用

找到本技能目录，将 `<skill>` 替换为实际绝对路径。全部 Python 代码随技能提供，使用 Python 3.10 或更高版本。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --help
python -I -X utf8 "<skill>/scripts/xhh.py" --protocol web doctor --offline
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> account-info
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> feed <topic-id> --limit 20
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> post <link-id>
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> --protocol app games info <steam-app-id>
python -I -X utf8 "<skill>/scripts/xhh.py" corpus plan <manifest.json>
python -I -X utf8 "<skill>/scripts/xhh.py" corpus run <run-id>
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> inbox --limit 20
```

账号显式使用 `--account <alias>`、`--config <file>` 或 `--env` 之一。全局参数放在子命令前。状态目录使用 `--data-dir <directory>` 或 `XHH_DATA_DIR`，默认 `~/.xiaoheihe-tools`。

首次登录、凭据配置与 App 签名器见 [REFERENCE.md](REFERENCE.md)。Web 协议使用内置 Python 签名器；App 协议需要用户提供签名器和 Java。协议由账号配置或 `--protocol` 明确选择，失败后保持该协议。

用户要求逛论坛、回复收件箱来信或继续多轮讨论时，先读取 [CONVERSATION.md](CONVERSATION.md)。按其中的每轮流程补读正文、定位目标、复用操作 ID并读回新评论。

公开 HTML 正文、媒体下载和离线快照查看不要求账号。App 或 Web 会话只用于对应协议的认证读取、个人数据和写操作；短信验证码只用于创建缺失的 App 会话。保存成功后复用会话，不重复登录。`native` 缺少 App 会话时返回 `app_session_missing`，`website` 缺少 Web 会话时返回 `web_session_missing`；`auto` 跳过缺失的会话并继续尝试可用的公开 HTML。

## 平台数据

`post` 返回结构化正文，`comments` 返回独立评论节点。正文按顺序保留文字、图片和链接，未知块保留平台数据并报告缺口。`body.status` 区分可用、缺失和解析失败；`body.completeness` 只有在明确证据存在时才为 `complete`。摘要留在 `excerpt`，不会替代正文。

用 `community list/search/info/children` 找社区，`topics` 搜话题，`feed` 和 `hot` 浏览轻量条目，`search` 搜帖子、游戏或用户。信息流的创建时间、评论数和充电数排序只作用于本次返回的条目，不能当作全站排序。评论可选 `--sort hot/time_aes/time_desc`。列表中出现无法规范化的混合记录时，工具保留可用条目，并在 `coverage.gaps` 与 `coverage.invalid` 中报告缺口。

`games info STEAM_APP_ID` 用 App GET `/game/get_game_detail?appid=<id>` 读取一个游戏详情资源。返回 `game`、`community`、`related_games`、`relation_groups`、`menus` 和 `source`；返回的 `steam_appid` 或 `appid` 必须匹配请求。社区有可用 `topic_detail.topic_id` 且 `has_bbs` 明确为真时才提供 feed 导航。三组关系 `morelike`、`similar_games`、`publisher_games` 按游戏 ID 合并并保留全部 `relations`。组内空数组为 `observed_empty`；缺少键为 `not_returned` 并报告缺口，不表示没有关系。无效形状或记录会报告缺口，保留有效游戏并返回 `partial`。此命令拒绝 `--pages > 1` 和 `--resume`，`--limit` 不截断嵌套关系，`--raw` 返回一次详情请求的原始响应。字段合同、计数和证据范围见 [REFERENCE.md](REFERENCE.md#games-info-steam_app_id)。

`feed --order newest/recommended/recent-replies` 选择平台原生发布时间、推荐或回复时间排序。`--sort` 只对已取得条目排序，两个选择的作用范围不同。

`thread <post-id> <root-id> --around <comment-id>` 读取根楼层、回复对象、祖先链和当前可取得的后续回复。已观察目标的 `reply_location` 给出回复所需的根楼页号、每页大小、排序和目标所在页的输入游标。`target_observed=false` 和 `missing_ancestor_ids` 表示上下文仍不足，不生成缺失楼层。

分页读取可设置 `--pages`、`--requests`、`--seconds` 和条目上限 `--limit`。这些是硬上限；同一次读取中的重定向也计入请求数。`coverage.completion` 区分确认结束、还有更多和未知。预算停止时保留已读内容与 `coverage.resume`。相同资源、筛选和账号可用 `--resume` 续读。`complete`、`partial`、`blocked`、`retryable_failure`、`failed` 和 `cursor_stalled` 是不同状态。验证码、限流、会话失效和未知非成功状态会停止且不返回 resume。

需要按多个社区、查询或话题批量收集公开条目时使用 `corpus`。v1 支持 `feed`、`search --type link` 和 `hashtags links`。Manifest 固定账号、协议、查询、过滤和配额，一个 run 的所有身份核对、页面请求和 reader fallback 共享全局预算。每次持久化 request attempt 最多派发一次 HTTP 请求；进程在请求后退出时，这次 reservation 仍计入配额。每一页会先持久化，再交付条目、去重实体、查询成员关系和未消费的同页余量。相同帖子出现在多个查询中只保存一份内容，并保留多条 membership。完整 Manifest 字段、query 类型和输出见 [REFERENCE.md](REFERENCE.md#有界-corpus)。

`corpus plan`、`corpus status`、`corpus run` 和 `corpus resume` 是完整入口。`plan` 与 `status` 不加载账号。`status` 可在无凭据、无签名器时读取持久化 checkpoint。`paused_budget` 只有在原 Manifest 仍有配额时可恢复。HTTP 408、5xx、DNS、TLS、timeout 和连接重置等可重试传输错误产生 `paused_retryable`，可直接 resume。验证码、限流、会话失效或不可重试平台错误产生 `blocked_human`，不得自动恢复。用户处理阻塞后执行 `corpus resume <run-id> --acknowledge-block`。固定配额耗尽不能通过 resume 扩大。`quota_reached` 只表示达到声明的配额，不表示平台历史已抓完。若 request 配额已经耗尽但同一页仍有 remainder，可用 `corpus resume <run-id> --max-requests 0` 先消费本地 remainder；只有需要读取新页面时才要求新的网络额度。

`user <uid>` 读取作者公开资料，`--view posts/followers/following` 继续导航。`favourites folders/posts` 读取当前账号的收藏夹和内容。`inbox` 保留平台消息字段，读取 `entries` 或旧 `messages` 列表，不改已读状态。

`asset <CDN-URL>` 下载平台图片并生成可用本地图像工具查看的 `preview_file`，需要可选 Pillow 依赖。源图与预览都有完整摘要；动画预览只展示首帧，原文件保留。官方表情使用 `emojis` 返回的平台目录。

`post --reader native/website/html/auto` 选择正文读取方式。`native` 要求 App，`website` 要求 Web，`html` 读取无凭据的公开页面。`html` 不接受 `--protocol`，不需要账号或签名器；显式入口不会切换协议，`auto` 只跳过缺失的会话并记录每次尝试。正文和身份证据按实际读取器记录，公开 HTML 不会继承 App 或 Web 的核验结果。除只读元数据外，所需正文为空会以 `body_missing` 失败。标题或作者没有公开证据时保留缺口，不从帖子 ID 或通用站点标题推断。`--section` 和 `--body-format` 可减少不需要的输出，`--raw` 包含成功 reader 的原始响应，仅用于检查。

普通浏览的 `source.configured_uid` 是配置中的数据命名空间，`source.identity_verified=false` 只表示本次没有核验在线会话身份。需要持续浏览时用 `--snapshot <name>` 保存公开观察与断点，`--since <name>` 查看同一查询的新增、变化和未再次观察到的条目。保存、`--since` 和网络续读会先核验在线 UID。未交付的 pending 响应可以在唯一名称下先离线重放；名称跨多个账号时仍需显式选择账号。其他离线快照操作按配置 UID 隔离，不表示身份已核验。缺失条目不推断为删除。

`coverage.full_history=false` 表示有限范围。空单页不能证明全量历史已查完。平台接口缺少预期消息字段时返回错误，不把协议异常当作空收件箱。

## 写操作

调用方先确认实际账号与精确目标，读取相关帖子和楼层，再提交已经确定的正文或图片。每个写操作提供 `--confirm` 和唯一 `--operation-id`。同一次用户操作的后续查询或重试沿用该 ID。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> comment <link-id> --text-file <utf8-file> --operation-id <id> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> reply <link-id> <root-id> <reply-id> --text-file <utf8-file> --operation-id <id> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> operation <id>
```

一级评论可附 `--image-file` 或 `--image-url`。楼中楼使用纯文本，图片参数由 CLI 拒绝。回复时复用读取所得的 `--page`、`--limit`、`--sort`；子评论首屏省略 `--cursor`，后续页使用目标所在页的输入游标。回复前工具核对目标确实出现在该帖、根楼层与所选分页中。

`returned` 表示取得 API 回执，公开可见性仍需读回核对。`write_uncertain`、`reserved` 或回执持久化失败可能已经发送，保留操作 ID并到平台核对。更换操作 ID不能证明旧请求未发送。验证码、限流或登录失效时停止该任务，交用户处理。

登录、发帖、上传、收藏和删除的参数与输出见 [REFERENCE.md](REFERENCE.md)。
