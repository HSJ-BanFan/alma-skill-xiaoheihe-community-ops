# 小黑盒命令参考

`scripts/xhh.py` 是唯一技能入口。下面的示例均使用 Python 3.10 或更高版本，将 `<skill>` 替换为技能的绝对路径。平台命令参数可通过各子命令的 `--help` 查询。

## 账号和运行环境

所有平台命令显式选择账号来源。

| 参数 | 来源 |
| --- | --- |
| `--account ALIAS` | `--data-dir` 下的 SDK 管理账号，别名由用户定义 |
| `--config FILE` | 用户提供的 JSON，字段为 `pkey`、`heybox_id`、`protocol_mode` 等 |
| `--env` | `XHH_PKEY`、`XHH_HEYBOX_ID`，可带 `XHH_PROTOCOL_MODE`、`XHH_SIGNER_JAR`、`XHH_JAVA` |
| `--protocol web` | 当前调用使用内置 Python Web 签名器 |
| `--protocol app` | 当前调用使用用户提供的 App 签名器 |
| `--data-dir DIR` | 管理账号和写回执目录，优先于 `XHH_DATA_DIR` |

默认数据目录是 `~/.xiaoheihe-tools`。技能不搜索其他应用的数据目录。管理账号使用 Windows DPAPI；Linux 和 macOS 使用用户提供的配置文件或环境变量。

复用旧账号库时，在全局参数中明确提供其所在 `--data-dir`，并在该目录运行 `account list`、`account status <alias> --online`。默认目录中的状态不代表其他目录的登录状态。目录选择和在线核验成功后，后续读取、回复与回执查询使用同一目录和账号。

账号与签名器管理复用随技能提供的 `xhh_sdk`，返回 SDK 自身的 JSON 或 CLI 帮助。安装 Creator 登录所需的可选依赖后，在用户的交互浏览器中登录。

```console
python -m pip install "playwright>=1.50,<2"
python -m playwright install chromium
python -I -X utf8 "<skill>/scripts/xhh.py" account add <alias>
python -I -X utf8 "<skill>/scripts/xhh.py" account login <alias> --method creator --timeout 600 --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" account status <alias> --online
python -I -X utf8 "<skill>/scripts/xhh.py" account --help
```

Creator 登录使用官方创作者页面，用户自行扫码或输入验证码。登录候选通过身份核对后才保存。凭据留在用户提供的配置或本地账号库，输出脱敏。

Web 不需要 Java。App 需要独立配置，技能包不附带 JAR、APK 或用户凭据。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" signer install <local.jar> --sha256 <trusted-sha256> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" account configure <alias> --set signer_jar=managed:<sha256> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" account configure <alias> --set java=<java-path> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> doctor --offline
```

SDK 的管理签名器默认位于 `~/.xhh_sdk/signers`，可用 `XHH_SIGNER_HOME` 设置绝对目录。该目录只保存小黑盒签名器。显式外部文件路径仍依赖其位置。

## 读取

平台读取返回一行 JSON，字段为 `version`、`ok`、`kind`、`data`、`coverage`、`error`。账号管理和 doctor 返回 SDK 的格式。命令退出码 `0` 表示该次命令成功；参数解析错误使用标准 argparse 输出。

| 命令 | 返回范围 |
| --- | --- |
| `account-info` | 在线账号身份核对 |
| `feed TOPIC --offset N --limit N` | 指定社区的信息流；返回规范化条目、来源与分页覆盖 |
| `search QUERY --type link --offset N --limit N` | 帖子、游戏或用户搜索；支持分页并按类型返回规范化或平台条目 |
| `post LINK --page N --limit N` | 结构化正文、顺序块、媒体、来源与正文完整性 |
| `comments LINK --page N --limit N` | 独立评论节点，保留根楼层及精确回复关系 |
| `replies ROOT --cursor CURSOR` | 指定根楼层的游标页 |
| `inbox --offset N --limit N` | 平台消息页，不标记已读 |
| `my-posts --offset N --limit N` | 当前账号的帖子页 |
| `my-comments --offset N --limit N` | 当前账号的评论页 |
| `drafts --offset N --limit N` | 平台草稿页 |
| `creator-options --link-id LINK` | 编辑器板块、标签、作者计划选项 |
| `emojis` | 平台官方表情目录；服务器可能返回空组 |
| `operation ID` | 本地按真实 UID 隔离的发送状态及回执 |
| `interaction status POST [--peer UID] [--root CID]` | 本地参与、轮数、来信状态及未解决操作 |
| `interaction history POST [--peer UID] [--root CID] [--limit N] [--resume TOKEN]` | 本地完整原文、关系、来源和时间证据，输出版本 2 |
| `interaction sync POST [--peer UID] [--root CID]` | 核验 UID 后按预算补录收件箱、自己的评论和公开楼层，输出版本 2 |

收件箱使用平台返回的 `entries` 列表，缺少该字段时接受旧 `messages` 列表。平台字段、完整正文和未读数量保持原样；存在但形状错误的列表返回 `invalid_inbox_response`。`comment_a_id` 是来信 CID，`comment_b_id` 是被回复的 CID，`root_comment_id` 是根楼层，`linkid` 是帖子 ID。正文分别在 `comment_a_text` 和 `comment_b_text`。

`inbox` 和 `my-comments` 支持 `--requests 10 --seconds 30`，上限各 100。正常单页需要一次身份核验和一次数据读取。实际 UID 不匹配或身份字段相互矛盾时拒绝个人导入。`my-comments` 验证 SDK 接口的原始 `result` 列表，形状错误返回 `invalid_my_comments_response`，显式作者错配返回 `own_comment_uid_mismatch`。互动查询、同步预算、续读绑定和证据字段见 [互动参考](docs/INTERACTION.md)。

`replies` 的下一位置在 `coverage.next`，完整绑定账号、命令和查询的续读令牌在 `coverage.resume`；平台原始 `lastval` 只在 `--raw` 的原始页中保留。`coverage.has_more` 缺失时完成状态未知。收件箱的 `coverage.next_offset` 按本页实际消息数计算，仅提供下一读取位置，不能证明后续为空。板块 ID 由用户或平台查询选择，工具不内置兴趣板块。

收件箱的 offset 不是稳定快照游标。分页期间有新消息插入列表前方时，后续页可能重复先前消息。调用方按 `message_id` 或来信 CID跨页、跨轮去重；下一轮最新消息检查从 offset 0 开始。完整处置流程见 [CONVERSATION.md](CONVERSATION.md)。

## 写入

所有下列命令要求 `--confirm --operation-id ID`。正文文件使用 UTF-8，评论和回复最多 1000 字符。工具在写前在线核对账号，不要求调用方提供宿主会话证明。

| 命令 | 参数 |
| --- | --- |
| `comment LINK` | `--text-file FILE`，可选互斥 `--image-file FILE` 或 `--image-url HTTPS_URL` |
| `reply LINK ROOT REPLY` | `--text-file FILE`，`--page N` 为正的绝对页号；`--limit N` 默认 50，`--sort hot/time_aes/time_desc` 默认 hot；`--cursor CURSOR` 定位后续子评论页 |
| `like LINK` | 原平台 `like/combo` 接口，可能涉及充电，不保证免费 |
| `favourite LINK` | 收藏到平台默认收藏夹 |
| `unfavourite LINK` | 取消收藏 |
| `upload FILE` | PNG、JPEG 或 GIF，本地文件最多 10 MiB，返回小黑盒 CDN URL |
| `publish SPEC.json` | 默认保存平台草稿，加 `--public` 公开发布 |
| `delete-comment LINK CID` | 先核对评论归当前账号所有 |
| `delete-post LINK` | 先核对帖子归当前账号所有 |

回复使用纯文本。一级评论配图只依据调用方传入的文件或 URL，工具不会搜索本地图库，也不要求写出不配图的理由。上传使用小黑盒自身的 COS 流程。

回复定位使用与读取相同的根楼页号、每页大小和排序。首个子评论页使用空的输入游标，CLI 省略 `--cursor`。显式提供游标时，精确目标必须出现在对应的子评论页；根楼预览中存在目标不能代替所选游标页的核验。

发帖 JSON 使用 SDK 的 `Post` 字段。先单独上传本地图像，再把返回的 HTTPS URL 放入 `images`。下面的文件描述普通动态。

```json
{
  "title": "标题",
  "content": "正文",
  "content_format": "text",
  "topic_ids": ["真实板块ID"],
  "hashtags": ["相关话题"],
  "images": [],
  "post_type": "1",
  "original": false
}
```

`topic_ids` 是板块 ID，`hashtags` 是话题文本，两者不同。Web 公开发帖至少选择一个平台板块。作者计划以平台资格为准。`--public` 决定草稿或公开状态，JSON 中的 `draft` 不覆盖该标志。通过 `edit_link_id` 修改自己的既有帖子时会先核对所有权。

## 发送状态

写入日志保存操作类型、真实账号 UID、请求指纹和回执。评论与回复还在同一个 `operations.sqlite3` 中保存帖子、根楼和目标 CID，以及用于提交核对的 SHA-256 指纹和可恢复的互动事实。完整请求只在本次发送中使用，互动数据库不保存正文或图片。人格、情绪、运营偏好及调度仍由调用方管理。

| 状态 | 含义 |
| --- | --- |
| `reserved` | 发送前已落盘，进程中断时可能已经发送 |
| `returned` | API 回执已落盘；相同 ID 与相同请求返回已有回执，不再次发送 |
| `uncertain` | 已进入写调用但没有完整成功回执，保持关闭 |
| `operation_id_conflict` | 相同 UID 与 ID 使用了不同请求，被拒绝 |
| `receipt_persistence_failed` | 取得回执但保存失败，输出保留该回执，旧 reservation 保持关闭 |

日志使用 SQLite 原子 reservation，多个进程不能同时取得同一 UID 与 ID 的发送权。去重范围是调用方给出的操作 ID，不把永久禁止再次评论同一个帖子当作平台限制。新的独立用户操作使用新 ID；恢复结果未知的请求沿用旧 ID，并先人工核对平台。

单次写入没有自动重试。验证码、限流和会话失效会结束当前命令。`public_visibility=not_checked` 表示未验证公开可见；平台 `ok`、CID 或本地记录都不是公开展示证据。

`interaction_projection_complete=false` 表示回执已提交，但派生互动记录尚未完成。`returned` 回执仍保留并可重放，`interaction status`、`interaction history` 或相同操作的重放会尝试本地恢复，不发送 POST。旧日志的指纹不能还原未知目标；仅当当前完整请求匹配旧指纹时才绑定已知上下文，继续遵守原来的关闭或回执重放状态。

## Agent 浏览工具

浏览入口输出版本为 `2`。账号管理仍使用 SDK 格式。所有浏览命令都有 `--raw`、`--requests 10`、`--seconds 30`，最大各 100。原始 SDK 文件保持不变。

| 命令 | 数据与参数 |
| --- | --- |
| `community list` | 分类与社区条目，包含分类路径、ID、名称、图标和简介 |
| `community search QUERY` | 社区名称搜索；包含平台返回的相关社区，不保证都是精确命中 |
| `community info ID` | 社区详情 |
| `community children ID` | 子社区与分类 |
| `topics QUERY` | 编辑器话题搜索 |
| `hot` | 热点条目，支持 `--offset`、`--limit`、`--pages` |
| `feed ID` | 轻量帖子条目；`--content-type all/article/dynamic`、`--after UNIX`、`--min-comments N` |
| `feed ID --sort created/comments/charges` | 对当前返回范围排序，`sorting_scope=returned_items`；默认 `platform` 保留服务器顺序 |
| `feed ID --order recommended/newest/recent-replies` | 平台原生推荐、发布时间或回复时间排序；与返回范围内的 `--sort` 分开 |
| `post LINK --section all/body/metadata` | LINK 可为 ID、标准小黑盒 HTTPS 链接或含 ID 的 heybox 链接；不跟随缩短链接 |
| `post LINK --body-format text/blocks/both` | 控制正文输出，默认 `both`；未知平台块保留 `data` 并标记缺口 |
| `post LINK --reader auto/native/website` | 默认 auto 按会话选择 Web 网站身份或 App 原生身份；显式 website 要求 Web 会话，每次只选择一个入口 |
| `comments LINK --sort hot/time_aes/time_desc` | 单独读取评论，不重复整个正文 |
| `replies ROOT --cursor VALUE` | 读取根楼层的游标页；未提供或未核实帖子 ID 时节点的帖子归属未知 |
| `thread LINK ROOT --around CID` | 围绕精确 CID 读取祖先与后续节点，`--pages` 控制根楼层定位和深楼读取总页数 |
| `user UID` | 返回作者公开资料，过滤草稿箱和编辑器字段 |
| `user UID --view posts/followers/following` | 沿作者帖子和公开关系继续浏览 |
| `favourites folders` | 当前账号的收藏夹 |
| `favourites posts FOLDER` | 收藏夹条目，默认收藏夹 ID 可为 `0` |
| `asset HTTPS_CDN_URL` | 下载图片、保留原图并生成 PNG 预览；无账号要求，不发送 Cookie |
| `snapshot list/show/resume/diff` | 查询、继续或比较本地浏览观察 |

信息流统计的 `charges` 来自 `link_award_num`，不把服务器的占位 `up=0` 当成点赞数。缺失的统计值是 `null`。正文摘要与正文分开，`body_not_returned`、`invalid_body_json` 和未知块均有明确缺口。非空正文自身不证明平台已给出全文。

默认的 `post --reader auto` 在发起请求前按已选会话决定入口，只使用一次读取请求。`source.reader` 记录实际的 `website` 或 `native` 身份。平台错误、正文缺失或预算停止都不会触发另一个入口；显式 native 的结果也保持原生范围。独立 comments 和 thread 继续使用评论入口。

`thread.data.reply_location` 给出该次指定目标的回复位置，字段为 `page`、`limit`、`sort`、`cursor`。`page` 是根楼被观察到的绝对页号；`cursor` 是实际包含目标的子评论页的输入游标，首屏和根楼预览为 `null`。读取更多页后，该位置仍对应目标，独立于最后一页的 `coverage.next`。未观察到目标时位置为 `null`。保存的已观察目标位置可随快照续读保留；旧快照缺少位置时，需重新观察目标。位置仅用于定位，发送前仍核验目标。

分页的 `completion=complete` 需要平台终点或总页数证据。空数组、缺少游标或达到预算只能产生 `unknown` 或停止状态，不能证明全量读取结束。`coverage.resume` 绑定真实账号、协议、资源和筛选；换资源或账号时被拒绝。页内与本次多页之间按 ID 去重。

`--page` 是绝对页号，不受单次 `--pages` 最多 100 页的预算限制。多页信息流排序在汇总并去重后执行；`returned_total` 报告本次各页实际返回数之和。App 签名调用使用剩余时间，发起 HTTP 前再次检查截止时间。`thread --raw` 返回根楼层和子评论的全部已取得原始页。

时间预算覆盖网络建立和完整响应接收；超时会终止该网络读取并保留之前完成的页。空评论页如果仍有明确总页数证据会继续。`unknown_parent_ids` 和 `ancestry_complete` 描述精确父节点是否已知，根楼层归属不会被当作直接回复关系。影音块保留可播放地址与原始元数据，无法解析时报告缺口。评论中的内嵌图片也会保留，HTML 标题和表格单元格不会被拼成无分隔文字。

预算和页数停止后，可重复同一查询并提供 `--resume <value>`。`thread` 的 `coverage.continuation` 返回可继续的 comments 页或 replies 游标。错误发生在后续页时，已读条目保留，`ok=false`，停止原因是 `platform_error`；不自动换账号、协议或入口。

媒体依赖按需安装，核心读取和 Web 签名没有新增依赖。

```console
python -m pip install -r "<skill>/requirements-media.txt"
python -I -X utf8 "<skill>/scripts/xhh.py" asset "https://imgheybox.max-c.com/实际图片路径"
```

媒体来源必须为 HTTPS 的 `max-c.com` 或其子域，重定向也遵守该范围。下载上限 16 MiB、像素上限 4000 万，预览最长边 1600、最大 512 KiB。PNG、JPEG、GIF、WebP 可用于预览，动画标记 `first_frame`。按 URL 缓存，缓存源和预览摘要都会核对；`--refresh` 才重新下载。

正文数据保留平台返回的旧 HTTP 图片引用，但 `asset` 仍要求 HTTPS，不能把已知引用当成已成功下载。正文中作者写出的文件路径仅作为文本返回，不读取对应的宿主文件。

浏览来源的 `configured_uid` 是配置中的数据命名空间，`identity_verified` 表示该次读取是否已在网络中核对当前会话。浏览快照保存在独立的 `browsing/snapshots.sqlite3`，与发送日志分开。快照按配置 UID 分组，这属于数据命名空间，不是同一本地操作系统用户之间的身份权限边界。缓存目录和文件限制为本地所有者访问；Windows 使用受保护 DACL，POSIX 使用目录 0700、数据库 0600。离线查看不需要有效网络会话，也不能证明身份。新保存、`--since` 和网络续读会核对真实会话 UID，身份验证也计入请求预算，因此这些在线操作至少需要两个请求。`--since` 和 `snapshot diff` 只比较相同资源与筛选的查询。

`identity_verified_this_read` 明确描述当前读取，兼容字段 `identity_verified` 保持同一含义。曾经核验的互动命名空间由 `interaction.identity_established` 表示，普通公开读取的当前身份标签仍为 false。快照差异比较排除本地 `interaction` 摘要，参与状态变化不会被当作平台帖子编辑。

快照使用递增修订号拒绝并发陈旧更新，冲突时保留数据库与本次已读结果。续读既可以用 `snapshot resume`，也可以用原命令的 `--resume --snapshot`，均保留先前页。响应与下一断点在同一事务持久化，stdout 成功写出并刷新后才确认已交付；中断时下次调用先离线重放未交付响应，不跳过数据。原型期旧 `browsing.sqlite3` 路径不自动导入。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> comments <link-id> --pages 3 --requests 2 --snapshot discussion
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> snapshot show discussion
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> snapshot resume discussion
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> feed <topic-id> --snapshot baseline
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> feed <topic-id> --since baseline --snapshot latest
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> snapshot diff baseline latest
```

差异为 `new`、`changed`、`unchanged`、`not_observed`，始终 `deletion_inferred=false`。来源检查时间不制造内容变化。未知身份只能使用明确标记的内容指纹，不能据此推断作者或帖子归属。快照记录公开观察，不维护人格、兴趣或互动处置策略。
