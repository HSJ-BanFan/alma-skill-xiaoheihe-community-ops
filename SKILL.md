---
name: xiaoheihe-tools
description: 读取小黑盒帖子、评论、收件箱和互动历史，查询与预览官方原生表情，登记用户提供的图片贴纸，构建带表情的内容并按明确账号、目标和操作ID发送及读回核对。用户需要浏览或使用小黑盒时使用。
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
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> inbox --limit 20
```

账号显式使用 `--account <alias>`、`--config <file>` 或 `--env` 之一。全局参数放在子命令前。状态目录使用 `--data-dir <directory>` 或 `XHH_DATA_DIR`，默认 `~/.xiaoheihe-tools`。

首次登录、凭据配置与 App 签名器见 [REFERENCE.md](REFERENCE.md)。Web 协议使用内置 Python 签名器；App 协议需要用户提供签名器和 Java。协议由账号配置或 `--protocol` 明确选择，失败后保持该协议。

用户要求逛论坛、回复收件箱来信或继续多轮讨论时，先读取 [CONVERSATION.md](CONVERSATION.md)。按其中的每轮流程补读正文、定位目标、复用操作 ID并读回新评论。

准备继续既有讨论、核对来信处置或恢复发送结果时，读取 [互动参考](docs/INTERACTION.md)。`interaction status <post-id>` 查询参与、轮数和待处理状态，`interaction history <post-id>` 返回评论 ID、作者、回复关系和时间；`--peer <uid>`、`--root <cid>` 缩小讨论范围。互动存储只保存结构化事实，原文和配图按需要用 `post`、`thread` 当前读取取得。`interaction sync <post-id>` 在预算内核验账号并补录事实，调用方决定是否回复及回复风格。

`completed_round_count` 是 `public_verified_lower_bound`，零轮表示当前尚无已验证完成回合。`own_reply_relations_complete=false` 或 `unattributed_own_comment_count>0` 表示自己的旧回复还缺直接关系；运行 `interaction sync <post-id>`，工具优先补读已验证评论列表指向的根楼，预算不足时使用 `coverage.resume`。`discussion_status=confirmed` 表示已知与其他 UID 有直接互动。根楼上下文和全量历史的完整性分别读取 `ancestry_complete` 与 `history_complete`。

## 原生表情与图片贴纸

选择表情、创建带表情的帖子或使用自制贴纸时，读取 [表情内容流程](references/EMOJIS.md)。按表达意图调用 `emojis search`，用返回的 ID 查详情和预览。目录中的含义说明标记来源与核验状态，排名分数是关键词匹配结果。

`describe` 返回精简命令目录，`describe --command compose` 返回结构化内容的输入、输出 schema 和对应 CLI 参数。`describe --command "emojis get"` 可查询子命令。内容节点使用 `text`、`native_emoji` 或 `image_sticker`。原生表情 ID 来自目录；图片贴纸 ID 来自 `stickers import`。创建新图片使用当前宿主的绘图能力，再登记文件。

`compose` 只做本地校验和准备，可用 `--out` 保存准备结果，用 `--preview-file` 生成自包含 HTML。生成图片预览需要可选 Pillow 和首次读取官方贴图。准备结果绑定内容、素材摘要、目录版本与协议。

发送已获授权的内容时，用 `send` 提交准备结果，提供明确账号、稳定操作 ID 与 `--confirm`。帖子默认保存草稿，`--public` 才公开发布。`verify` 读回并核对内容，分别报告回执、内容一致性、公开可见性与图标渲染证据。恢复同一次操作沿用其 ID。

## 平台数据

`post` 返回结构化正文，`comments` 返回独立评论节点。正文按顺序保留文字、图片和链接，未知块保留平台数据并报告缺口。`body.status` 区分可用、缺失和解析失败；`body.completeness` 只有在明确证据存在时才为 `complete`。摘要留在 `excerpt`，不会替代正文。

用 `community list/search/info/children` 找社区，`topics` 搜话题，`feed` 和 `hot` 浏览轻量条目，`search` 搜帖子、游戏或用户。信息流的创建时间、评论数和充电数排序只作用于本次返回的条目，不能当作全站排序。评论可选 `--sort hot/time_aes/time_desc`。

`feed --order newest/recommended/recent-replies` 选择平台原生发布时间、推荐或回复时间排序。`--sort` 只对已取得条目排序，两个选择的作用范围不同。

`thread <post-id> <root-id> --around <comment-id>` 读取根楼层、回复对象、祖先链和当前可取得的后续回复。已观察目标的 `reply_location` 给出回复所需的根楼页号、每页大小、排序和目标所在页的输入游标。`target_observed=false` 和 `missing_ancestor_ids` 表示上下文仍不足，不生成缺失楼层。

分页读取可设置 `--pages`、`--requests`、`--seconds`。`coverage.completion` 区分确认结束、还有更多和未知；预算停止时保留已读内容与 `coverage.resume`。相同资源、筛选和账号可用 `--resume` 续读。平台错误会停止且保留已取得的数据。

`user <uid>` 读取作者公开资料，`--view posts/followers/following` 继续导航。`favourites folders/posts` 读取当前账号的收藏夹和内容。`inbox` 保留平台消息字段，读取 `entries` 或旧 `messages` 列表，不改已读状态。

`asset <CDN-URL>` 下载平台图片并生成可用本地图像工具查看的 `preview_file`，需要可选 Pillow 依赖。源图与预览都有完整摘要；动画预览只展示首帧，原文件保留。`emojis list/search/get` 使用验证过的官方目录快照，`emojis refresh` 从官方创作者资源更新。缓存异常在 `saved_error` 报告，失效刷新保留原缓存。

读取时原生短码、官方 `data-emoji` 标签和可唯一识别的字体标签归一化为 `native_emoji`，保留 ID、短码及位置。未知或冲突的身份保留原始数据并报告缺口。

`post` 默认使用 `--reader auto`，Web 会话读取网站正文，App 会话读取原生正文。`--reader native` 和 `--reader website` 可显式选择身份，网站方式需要 Web 会话。每次读取选择一个入口，不在失败后切换协议。`--section` 和 `--body-format` 可减少不需要的输出，`--raw` 仅用于检查原始响应。

普通浏览的 `source.configured_uid` 是配置中的数据命名空间，`source.identity_verified=false` 只表示本次没有核验在线会话身份。需要持续浏览时用 `--snapshot <name>` 保存公开观察与断点，`--since <name>` 查看同一查询的新增、变化和未再次观察到的条目。保存、`--since` 和网络续读会先核验在线 UID；离线 `snapshot show/resume/diff/list` 按配置 UID 隔离，不表示身份已核验。缺失条目不推断为删除。

`coverage.full_history=false` 表示有限范围。空单页不能证明全量历史已查完。平台接口缺少预期消息字段时返回错误，不把协议异常当作空收件箱。

`post.data.interaction` 和 `feed`、`hot` 各条目的 `interaction` 是本地轻量摘要，不增加 HTTP 请求。`unknown` 和已知零条都不能证明从未参与。`identity_established` 表示该 UID 曾在本地历史中被核验；`source.identity_verified_this_read` 表示本次读取核验。普通公开读取可以为已有可信 UID 补充公开事实，仍保留本次未核验的标签。仅配置 UID 且没有可信历史时，公开读取不会建立个人历史。`inbox` 和 `my-comments` 每次先核验实际 UID，验证请求计入 `--requests` 和 `--seconds`。

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
