---
name: xiaoheihe-tools
description: 独立的小黑盒社区与内容工具，提供社区发现、信息流、搜索、正文、评论、账号、写入、互动历史、预算与 checkpoint corpus，以及官方原生表情、图片贴纸、Markdown 转换、投稿预览和读回核对；用户需要浏览或操作小黑盒，或了解评论限制、文章/图文/热点计划、图片封面和投稿格式时使用。
---

# 小黑盒工具

本技能提供平台工具。调用方决定内容、语气、是否互动和执行时间。

## 调用

找到本技能目录，将 `<skill>` 替换为实际绝对路径。全部 Python 代码随技能提供，使用 Python 3.10 或更高版本。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --help
python -I -X utf8 "<skill>/scripts/xhh.py" --protocol web doctor --offline
python -I -X utf8 "<skill>/scripts/xhh.py" describe
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> account-info
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> feed <topic-id> --limit 20
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> post <link-id>
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> --protocol app games info <steam-app-id>
python -I -X utf8 "<skill>/scripts/xhh.py" corpus plan <manifest.json>
python -I -X utf8 "<skill>/scripts/xhh.py" corpus run <run-id>
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> inbox --limit 20
python -I -X utf8 "<skill>/scripts/xhh.py" preview-post <spec.json>
```

账号显式使用 `--account <alias>`、`--config <file>` 或 `--env` 之一。全局参数放在子命令前。状态目录使用 `--data-dir <directory>` 或 `XHH_DATA_DIR`，默认 `~/.xiaoheihe-tools`。

首次登录、凭据配置与 App 签名器见 [REFERENCE.md](REFERENCE.md)。Web 协议使用内置 Python 签名器；App 协议需要用户提供签名器和 Java。协议由账号配置或 `--protocol` 明确选择，失败后保持该协议。

账号管理命令也返回一行 JSON。缺失签名器、风险令牌、未绑定身份、会话或在线核验失败时，读取 `error.code` 和 `error.fix`，不要解析自由文本错误。

用户要求逛论坛、回复收件箱来信或继续多轮讨论时，先读取 [CONVERSATION.md](CONVERSATION.md)。按其中的每轮流程补读正文、定位目标、复用操作 ID并读回新评论。

公开 HTML 正文、媒体下载和离线快照查看不要求账号。App 或 Web 会话只用于对应协议的认证读取、个人数据和写操作；短信验证码只用于创建缺失的 App 会话。保存成功后复用会话，不重复登录。`native` 缺少 App 会话时返回 `app_session_missing`，`website` 缺少 Web 会话时返回 `web_session_missing`；`auto` 跳过缺失的会话并继续尝试可用的公开 HTML。

准备继续既有讨论、核对来信处置或恢复发送结果时，读取 [互动参考](docs/INTERACTION.md)。`interaction status <post-id>` 查询参与、轮数和待处理状态，`interaction history <post-id>` 返回评论 ID、作者、回复关系和时间；`--peer <uid>`、`--root <cid>` 缩小讨论范围。互动存储只保存结构化事实，原文和配图按需要用 `post`、`thread` 当前读取取得。`interaction sync <post-id>` 在预算内核验账号并补录事实，调用方决定是否回复及回复风格。

`completed_round_count` 是 `public_verified_lower_bound`，零轮表示当前尚无已验证完成回合。`own_reply_relations_complete=false` 或 `unattributed_own_comment_count>0` 表示自己的旧回复还缺直接关系；运行 `interaction sync <post-id>`，工具优先补读已验证评论列表指向的根楼，预算不足时使用 `coverage.resume`。`discussion_status=confirmed` 表示已知与其他 UID 有直接互动。根楼上下文和全量历史的完整性分别读取 `ancestry_complete` 与 `history_complete`。

## 原生表情与图片贴纸

选择表情、创建带表情的帖子或使用自制贴纸时，读取 [表情内容流程](references/EMOJIS.md)。按表达意图调用 `emojis search`，用返回的 ID 查详情和预览。目录中的含义说明标记来源与核验状态，排名分数是关键词匹配结果。

`describe` 返回精简命令目录，`describe --command compose` 返回结构化内容的输入、输出 schema 和对应 CLI 参数。`describe --command "emojis get"` 可查询子命令。内容节点使用 `text`、`native_emoji` 或 `image_sticker`。原生表情 ID 来自目录；图片贴纸 ID 来自 `stickers import`。创建新图片使用当前宿主的绘图能力，再登记文件。

`compose` 只做本地校验和准备，可用 `--out` 保存准备结果，用 `--preview-file` 生成自包含 HTML。生成图片预览需要可选 Pillow 和首次读取官方贴图。准备结果绑定内容、素材摘要、目录版本与协议。

发送已获授权的内容时，用 `send` 提交准备结果，提供明确账号、稳定操作 ID 与 `--confirm`。帖子默认保存草稿，`--public` 才公开发布。`verify` 读回并核对内容，分别报告回执、内容一致性、公开可见性与图标渲染证据。恢复同一次操作沿用其 ID。

## 平台数据

`post` 返回结构化正文，`comments` 返回独立评论节点。正文按顺序保留文字、图片和链接，未知块保留平台数据并报告缺口。`body.status` 区分可用、缺失和解析失败；`body.completeness` 只有在明确证据存在时才为 `complete`。摘要留在 `excerpt`，不会替代正文。

用 `community list/search/info/children` 找社区，`topics` 搜话题，`feed` 和 `hot` 浏览轻量条目，`search` 搜帖子、游戏或用户。信息流的创建时间、评论数和充电数排序只作用于本次返回的条目，不能当作全站排序。评论可选 `--sort hot/time_aes/time_desc`。列表中出现无法规范化的混合记录时，工具保留可用条目，并在 `coverage.gaps` 与 `coverage.invalid` 中报告缺口。

`games info STEAM_APP_ID` 用 App GET `/game/get_game_detail?appid=<id>` 读取一个游戏详情资源。返回 `game`、`community`、`related_games`、`relation_groups`、`menus` 和 `source`；返回的 `steam_appid` 或 `appid` 必须匹配请求。社区有可用 `topic_detail.topic_id` 且 `has_bbs` 明确为真时才提供 feed 导航。三组关系 `morelike`、`similar_games`、`publisher_games` 按游戏 ID 合并并保留全部 `relations`。组内空数组为 `observed_empty`；缺少键为 `not_returned` 并报告缺口，不表示没有关系。无效形状或记录会报告缺口，保留有效游戏并返回 `partial`。此命令拒绝 `--pages > 1` 和 `--resume`，`--limit` 不截断嵌套关系，`--raw` 返回一次详情请求的原始响应。字段合同、计数和证据范围见 [REFERENCE.md](REFERENCE.md#games-info-steam_app_id)。

`feed --order newest/recommended/recent-replies` 选择平台原生发布时间、推荐或回复时间排序。`--sort` 只对已取得条目排序，两个选择的作用范围不同。

`thread <post-id> <root-id> --around <comment-id>` 读取根楼层、回复对象、祖先链和当前可取得的后续回复。已观察目标的 `reply_location` 给出回复所需的根楼页号、每页大小、排序和目标所在页的输入游标。`target_observed=false` 和 `missing_ancestor_ids` 表示上下文仍不足，不生成缺失楼层。

分页读取可设置 `--pages`、`--requests`、`--seconds` 和条目上限 `--limit`。这些是硬上限；同一次读取中的重定向也计入请求数。`coverage.completion` 区分确认结束、还有更多和未知。预算停止时保留已读内容与 `coverage.resume`。相同资源、筛选和账号可用 `--resume` 续读。单资源一次返回的集合被 `--limit` 截断时返回 `partial/item_budget`，报告 `single_response_truncated` 和 `omitted`，且不生成 resume；需要时提高上限后重读。例外是 `user --view relations`，它返回 `partial/relation_collection_partial` 和 `missing_count`，不报告 `omitted`。`complete`、`partial`、`blocked`、`retryable_failure`、`failed` 和 `cursor_stalled` 是不同状态。验证码、限流、会话失效和未知非成功状态会停止且不返回 resume。

需要按多个社区、查询或话题批量收集公开条目时使用 `corpus`。v1 支持 `feed`、`search --type link` 和 `hashtags links`。Manifest 固定账号、协议、查询、过滤和配额，一个 run 的所有身份核对、页面请求和 reader fallback 共享全局预算。每次持久化 request attempt 最多派发一次 HTTP 请求；进程在请求后退出时，这次 reservation 仍计入配额。每一页会先持久化，再交付条目、去重实体、查询成员关系和未消费的同页余量。相同帖子出现在多个查询中只保存一份内容，并保留多条 membership。完整 Manifest 字段、query 类型和输出见 [REFERENCE.md](REFERENCE.md#有界-corpus)。

`corpus plan`、`corpus status`、`corpus run` 和 `corpus resume` 是完整入口。`plan` 与 `status` 不加载账号。`status` 可在无凭据、无签名器时读取持久化 checkpoint。`paused_budget` 只有在原 Manifest 仍有配额时可恢复。HTTP 408、5xx、DNS、TLS、timeout 和连接重置等可重试传输错误产生 `paused_retryable`，可直接 resume。验证码、限流、会话失效或不可重试平台错误产生 `blocked_human`，不得自动恢复。用户处理阻塞后执行 `corpus resume <run-id> --acknowledge-block`。固定配额耗尽不能通过 resume 扩大。`quota_reached` 只表示达到声明的配额，不表示平台历史已抓完。若 request 配额已经耗尽但同一页仍有 remainder，可用 `corpus resume <run-id> --max-requests 0` 先消费本地 remainder；只有需要读取新页面时才要求新的网络额度。

`user <uid>` 读取作者公开资料，`--view posts/followers/following` 继续导航。粉丝和关注视图接受平台顶层 `follow_list`，返回条目、总数与 `follow_cnt`；平台忽略请求页大小时仍按实际返回位置生成安全续读。`--view relations` 只返回当次平台 `count/users` 集合，不把空集合或用户列表解释为当前账号的已关注、被关注或互关状态。`favourites folders/posts` 读取当前账号的收藏夹和内容。`inbox` 保留平台消息字段，读取 `entries` 或旧 `messages` 列表，不改已读状态。

`asset <CDN-URL>` 下载平台图片并生成可用本地图像工具查看的 `preview_file`，需要可选 Pillow 依赖。源图与预览都有完整摘要；动画预览只展示首帧，原文件保留。`emojis list/search/get` 使用验证过的官方目录快照，`emojis refresh` 从官方创作者资源更新。缓存异常在 `saved_error` 报告，失效刷新保留原缓存。

读取时原生短码、官方 `data-emoji` 标签和可唯一识别的字体标签归一化为 `native_emoji`，保留 ID、短码及位置。未知或冲突的身份保留原始数据并报告缺口。

`post --reader native/website/html/auto` 选择正文读取方式。`native` 要求 App，`website` 要求 Web，`html` 读取无凭据的公开页面。`html` 不接受 `--protocol`，不需要账号或签名器；如果同时传入 `--account`，工具先验证别名，只把账号 UID 用作快照命名空间。显式入口不会切换协议，`auto` 只跳过缺失的会话并记录每次尝试。正文和身份证据按实际读取器记录，公开 HTML 不会继承 App 或 Web 的核验结果。除只读元数据外，所需正文为空会以 `body_missing` 失败。标题或作者没有公开证据时保留缺口，不从帖子 ID 或通用站点标题推断。`--section` 和 `--body-format` 可减少不需要的输出，`--raw` 包含成功 reader 的原始响应，仅用于检查。

`data.body_candidates` 为每个成功 reader 提供正文长度、块数、块类型、缺口、来源和正文摘要。它不复制正文。用这些摘要比较 reader，不要把 metadata 来源当成正文来源。

普通浏览的 `source.configured_uid` 是配置中的数据命名空间，`source.identity_verified=false` 只表示本次没有核验在线会话身份。需要持续浏览时用 `--snapshot <name>` 保存公开观察与断点，`--since <name>` 查看同一查询的新增、变化和未再次观察到的条目。保存、`--since` 和网络续读会先核验在线 UID。未交付的 pending 响应可以在唯一名称下先离线重放；名称跨多个账号时仍需显式选择账号。其他离线快照操作按配置 UID 隔离，不表示身份已核验。缺失条目不推断为删除。

当同一查询的续读或已有快照在取得新页前遇到不可重试错误时，工具保留已有数据，原子写入 `blocked`，清除 resume 和 pending。可重试的传输或平台错误不修改已有快照。正文快照续读时，如果新一轮成功 reader 都没有可交付正文，工具保留旧正文、正文来源和候选摘要，并报告 `reader_body_missing_after_resume`。预算暂停仍返回安全 resume；reader 已耗尽时返回 `partial` 且不返回 resume。同名快照属于另一查询时保持原快照不变。没有已有数据时不会创建空快照。

`coverage.full_history=false` 表示有限范围。空单页不能证明全量历史已查完。平台接口缺少预期消息字段时返回错误，不把协议异常当作空收件箱。

`captcha_required` 和 `rate_limited` 始终包含 `retry_allowed=false` 与 `cooldown_required=true`。`retry_after_seconds` 只在平台提供可信等待时间时为整数，否则为 `null`。遇到任一冷却错误时停止当前 run 的所有后续请求。

`post.data.interaction` 和 `feed`、`hot` 各条目的 `interaction` 是本地轻量摘要，不增加 HTTP 请求。`unknown` 和已知零条都不能证明从未参与。`identity_established` 表示该 UID 曾在本地历史中被核验；`source.identity_verified_this_read` 表示本次读取核验。普通公开读取可以为已有可信 UID 补充公开事实，仍保留本次未核验的标签。仅配置 UID 且没有可信历史时，公开读取不会建立个人历史。`inbox` 和 `my-comments` 每次先核验实际 UID，验证请求计入 `--requests` 和 `--seconds`。

## 投稿规则与格式

回答评论限制、文章/图文/热点计划的用途与申请门槛、奖励机会或计划元数据时，先读 [评论与投稿规则](references/platform-rules.md)。该文件区分工具强制、官方指引日期、按日期观察和平台决定，不把选项存在、普通发帖权限或旧账号拒绝当成资格结论。

编写或发布正文、本地预览、上传图片、封面和贴纸时，读 [投稿格式与媒体](references/formatting.md)。发帖 spec 首选 `post_format`。Web 的 `image_text` 发送字面纯文本和尾部图片，`article` 接受 HTML/Markdown 和就地图片；两种都支持独立封面。App 保留旧 SDK 协议。`preview-post` 默认 Web，向 App 账号发送前用全局 `--protocol app` 预览相同协议。CLI spec 的 `content_format` 默认 `text`；转换 Markdown 时显式写 `"post_format": "article", "content_format": "markdown"`，并按 `requirements-formatting.txt` 安装可选依赖。预览检查规范 spec、真实 payload 和媒体摘要，不读账号、不联网、不写状态。预览或 API 回执都不证明公开可见或计划审核通过。规则表和格式说明只保留在这两份 reference 中。

## 写操作

调用方先确认实际账号与精确目标，读取相关帖子和楼层，再提交已经确定的正文或图片。评论和回复支持在其他用户的帖子下参与讨论；工具在发送前核验实际账号 UID 与帖子存在，回复还核对根楼层、精确目标及所选分页。操作绑定实际账号 UID 和帖子 ID，回复另绑定根楼层与目标 CID。编辑或删除帖子仍要求帖子归当前账号所有，删除评论仍要求评论归当前账号所有；SDK 的 canary 可见性探测仍只用于自己的帖子。每个写操作提供 `--confirm` 和唯一 `--operation-id`。同一次用户操作的后续查询或重试沿用该 ID。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> comment <link-id> --text-file <utf8-file> --operation-id <id> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> reply <link-id> <root-id> <reply-id> --text-file <utf8-file> --operation-id <id> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> operation <id>
```

本工具的一级评论可附 `--image-file` 或 `--image-url`。本工具的楼中楼接受文字和原生表情短码，图片参数由 CLI 拒绝；平台客户端是否允许楼中楼图片尚未验证。需要稳定配图时将图片放在该帖的一级评论。回复时复用读取所得的 `--page`、`--limit`、`--sort`；子评论首屏省略 `--cursor`，后续页使用目标所在页的输入游标。回复前工具核对目标确实出现在该帖、根楼层与所选分页中。

`returned` 表示取得 API 回执，公开可见性仍需读回核对。`write_uncertain`、`reserved` 或回执持久化失败可能已经发送，保留操作 ID并到平台核对。更换操作 ID不能证明旧请求未发送。验证码、限流或登录失效时停止该任务，交用户处理。

登录、发帖、上传、收藏和删除的参数与输出见 [REFERENCE.md](REFERENCE.md)。
