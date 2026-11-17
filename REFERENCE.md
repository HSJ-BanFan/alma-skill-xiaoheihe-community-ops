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

账号与签名器管理复用随技能提供的 `xhh_sdk`，返回 SDK 自身的 JSON 或 CLI 帮助。Web 登录可以选择 Creator 页面、扫码或浏览器流程。App 阅读和写操作需要 App 会话；首次创建 App 会话时使用官方短信登录，之后命令直接复用已保存的会话。

```console
python -m pip install "playwright>=1.50,<2"
python -m playwright install chromium
python -I -X utf8 "<skill>/scripts/xhh.py" account add <alias>
python -I -X utf8 "<skill>/scripts/xhh.py" account login <alias> --method creator --timeout 600 --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" account status <alias> --online
python -I -X utf8 "<skill>/scripts/xhh.py" account --help
```

`account status --online` 优先使用账号配置的协议；该协议没有保存的会话时，使用账号实际拥有的 App 或 Web 会话。Web-only 账号不需要伪造 App 配置。

Creator 登录使用官方创作者页面，用户自行扫码或输入验证码。登录候选通过身份核对后才保存。凭据留在用户提供的配置或本地账号库，输出脱敏。

账号管理失败输出 `{"ok":false,"command":"account","error":{...}}`。调用方读取 `error.code`、`error.fix` 和 `error.retry_allowed`。未绑定身份使用 `account_identity_unbound`，缺少风险令牌使用 `risk_token_missing`，验证码和限流分别使用 `captcha_required` 与 `rate_limited`。

App 短信登录还要求四项输入。签名器为每次 App 请求生成平台签名。`x_xhh_tokenid` 风险令牌是登录接口使用的设备反滥用值，可由 `account risk-token` 在用户自己的浏览器中取得。数字 UID 把保存的会话绑定到预期账号。短信验证码只用于创建会话。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" account add <alias> --identity <uid>
python -I -X utf8 "<skill>/scripts/xhh.py" account configure <alias> --set signer_jar=<managed-or-local-jar> --set java=<java-path> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" account risk-token <alias> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" account login <alias> --method sms --phone <phone> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" account login <alias> --method sms --phone <phone> --code <code> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" account status <alias> --online
```

第一条 `account login` 请求平台发送短信。第二条用收到的验证码换取 App 会话。技能不把手机号、验证码、风险令牌或会话写入仓库输出。已保存的会话失效前不需要再次发送短信。公开 HTML 正文、媒体下载和离线快照查看都不要求账号或短信登录。

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
| `emojis list/search/get/refresh` | 验证过的官方目录、含义说明、图像预览与官方资源刷新；本地查询不需要账号 |
| `operation ID` | 本地按真实 UID 隔离的发送状态及回执 |
| `interaction status POST [--peer UID] [--root CID]` | 本地参与、轮数、来信状态及未解决操作 |
| `interaction history POST [--peer UID] [--root CID] [--limit N] [--resume TOKEN]` | 本地完整原文、关系、来源和时间证据，输出版本 2 |
| `interaction sync POST [--peer UID] [--root CID]` | 核验 UID 后按预算补录收件箱、自己的评论和公开楼层，输出版本 2 |

收件箱使用平台返回的 `entries` 列表，缺少该字段时接受旧 `messages` 列表。平台字段、完整正文和未读数量保持原样；存在但形状错误的列表返回 `invalid_inbox_response`。`comment_a_id` 是来信 CID，`comment_b_id` 是被回复的 CID，`root_comment_id` 是根楼层，`linkid` 是帖子 ID。正文分别在 `comment_a_text` 和 `comment_b_text`。

`inbox` 和 `my-comments` 支持 `--requests 10 --seconds 30`，上限各 100。正常单页需要一次身份核验和一次数据读取。实际 UID 不匹配或身份字段相互矛盾时拒绝个人导入。`my-comments` 验证 SDK 接口的原始 `result` 列表，形状错误返回 `invalid_my_comments_response`，显式作者错配返回 `own_comment_uid_mismatch`。互动查询、同步预算、续读绑定和证据字段见 [互动参考](docs/INTERACTION.md)。

`replies` 的下一位置在 `coverage.next`，完整绑定账号、命令和查询的续读令牌在 `coverage.resume`；平台原始 `lastval` 只在 `--raw` 的原始页中保留。`coverage.has_more` 缺失时完成状态未知。收件箱的 `coverage.next_offset` 按本页实际消息数计算，仅提供下一读取位置，不能证明后续为空。板块 ID 由用户或平台查询选择，工具不内置兴趣板块。

收件箱的 offset 不是稳定快照游标。分页期间有新消息插入列表前方时，后续页可能重复先前消息。调用方按 `message_id` 或来信 CID跨页、跨轮去重；下一轮最新消息检查从 offset 0 开始。完整处置流程见 [CONVERSATION.md](CONVERSATION.md)。

## 本地预览与正文格式

`preview-post SPEC.json [--public]` 在读取账号配置之前执行。它不读账号、不访问网络、不占用操作 ID、不写状态，不是写命令。默认使用 Web，旧 App 预览使用全局 `--protocol app`。输出的 `data` 为 `spec`、`payload`、`media`；`payload` 是相同协议发布时使用的字段，`media` 从该 payload 读回。规范 spec 可用 `Post(**spec).build(protocol=coverage["protocol"])` 重建。`coverage` 标记协议、离线、未核账号、未预留操作 ID、`public_visibility=not_checked`。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" preview-post post.json
python -I -X utf8 "<skill>/scripts/xhh.py" preview-post post.json --public
```

发帖 spec 首选 `post_format`。`image_text` 发送字面纯文本块和尾部图片块；`article` 才接受富 HTML 或 Markdown，并发射一个 HTML 块和图片元数据。`post_type` 的 `"1"`、`"3"` 是旧输入别名，新调用只用 `post_format`。当前适配器的 wire `post_type` 为 `"1"`（普通）或 `"2"`（选择了 `post_plan`）。`link_tag` 由工具派生，`article` 为 `11`、`image_text` 为 `27`；最终 `topic_ids` 字符串恰好为 `58144` 时分别为 `21`、`28`。无板块图文按当前编辑器使用动态分区 `58144`，显式选择的板块保持原值，文章不添加该默认值。

`content_format` 默认 `text`，可选 `text`、`html` 或 `markdown`。`text` 在 `article` 中逐行转换为转义后的 HTML，在 `image_text` 中保留为字面文本块；`html` 原样使用调用方 HTML；`markdown` 在预览和发布时自动转换为 HTML。`image_text` 只接受 `text`。富 HTML 和 Markdown 必须显式写 `post_format: "article"`，工具不会静默切换格式。把 Markdown 写进 `text` 或 `html` 时不会转换。

Markdown 转换需要可选依赖，固定版本写在 `requirements-formatting.txt`。该依赖只在 `markdown` 分支惰性导入，普通 `text`/`html` 预览、读取、帮助和 Web doctor 不需要它。

```console
python -m pip install -r "<skill>/requirements-formatting.txt"
```

Markdown 只用于 `article`。它保留作者写出的 `h1`-`h6` 层级，转义原始 HTML，解析表格和删除线，保留代码字符、缩进与换行。内联图片必须是绝对 HTTPS URL，按原位置和重复次数保留并带 `data-type="image"`、`data-original` 属性；`image_info` 可提供上传回执中的真实尺寸。工具不下载图片，也不会把内联图片复制到尾部 `images`。`article` 的 `images` 会追加到 HTML 末尾，`img` 元数据块不会重复成额外可见图片。平台编辑器再次导入可能降级格式，详见格式参考的当日观察。

正文、Markdown、上传、封面和媒体字段的完整说明见 [投稿格式与媒体](references/formatting.md)。评论限制和文章/图文/热点计划规则见 [评论与投稿规则](references/platform-rules.md)。

## 写入

结构化表情内容使用 `compose SPEC.json --out prepared.json`，然后用 `send prepared.json --confirm --operation-id ID`，最后用 `verify ID`。输入、输出 schema 与 CLI 参数由 `describe --command compose` 返回。素材选择、图片创建与错误恢复见 [表情内容流程](references/EMOJIS.md)。既有纯文本和 SDK 发帖命令仍可使用。

所有下列命令要求 `--confirm --operation-id ID`。正文文件使用 UTF-8。普通命令与结构化 `send` 均支持评论或回复其他用户的帖子；CLI 在发送前核验实际账号 UID、帖子存在与回复目标。操作绑定实际账号 UID 和帖子 ID，回复另绑定根楼层与精确目标 CID，并核验所选分页。编辑和删除帖子仍要求帖子归当前账号所有，删除评论仍要求评论归当前账号所有；SDK 的 canary 可见性探测仍只用于自己的帖子。平台评论配置当前返回 5000，本工具把评论和回复保守限制为最多 1000 个 Unicode 字符。工具在写前在线核对账号，不要求调用方提供宿主会话证明。

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

本工具的回复接受文字和原生表情短码，拒绝图片参数和图片贴纸；平台客户端是否允许楼中楼图片尚未验证。需要稳定配图时发到该帖的一级评论。一级评论配图只依据调用方传入的文件或 URL，工具不会搜索本地图库。上传使用小黑盒自身的 COS 流程。

回复定位使用与读取相同的根楼页号、每页大小和排序。首个子评论页使用空的输入游标，CLI 省略 `--cursor`。显式提供游标时，精确目标必须出现在对应的子评论页；根楼预览中存在目标不能代替所选游标页的核验。

发帖 JSON 使用 SDK 的 `Post` 字段。先单独上传本地图像，再把返回的 HTTPS URL 放入 `images`。下面的文件描述图文；需要富文本时显式写 `post_format: "article"`，再选择 `content_format: "html"` 或 `"markdown"`。

```json
{
  "title": "标题",
  "content": "正文",
  "content_format": "text",
  "post_format": "image_text",
  "topic_ids": ["真实板块ID"],
  "hashtags": ["相关话题"],
  "images": [],
  "authorship": "original",
  "declaration": 0
}
```

`topic_ids` 是板块 ID，`hashtags` 是话题文本，两者不同。本技能的 Web 公开发送要求调用方明确提供有效板块。作者计划以平台当前资格和审核为准。`--public` 决定草稿或公开状态，JSON 中的 `draft` 不覆盖该标志。通过 `edit_link_id` 修改自己的既有帖子时会先核对所有权。

作者身份使用 `authorship`（`original` 或 `repost`）和 `declaration`（`0` 未选择，`1`、`2` 按原创或转载解释）。显式 `authorship` 优先于旧 `original`。未写 `authorship` 时，旧 `original=true` 选择原创并默认 `declaration=0`；旧 `original=false` 不作作者身份声明，不会映射成转载。`source` 是来源字符串。`extra_declaration` 为 `1` AI 辅助、`2` 剧透、`3` 虚构演绎。显式作者身份可以和 AI 辅助声明同时使用。

未知字段、未知格式和错误类型在发送前拒绝。`images` 只接受已上传的绝对 HTTPS URL。Web 的 `cover_url` 两种格式都可使用，封面与正文图片分开；`visibility` 为 `"1"`、`"2"`、`"3"`，默认 `"1"`，`"0"` 无效。Web 选择计划时 wire `post_type` 为 `"2"`，否则为 `"1"`。旧 App 保留 `post_type=1/3`、HTML 与表情标签、前导 `#` 标签及原有可见性值；新作者声明字段在旧适配器中明确拒绝。预览和发送应使用相同协议，详细字段见 [投稿格式与媒体](references/formatting.md)。

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

SDK 返回已识别的 Web 发帖失败时，`write_uncertain` 的 `error.diagnostic` 使用闭合代码、固定路径、状态和消息。该诊断不改变未知发送状态，也不允许重新发送；保留原操作 ID，并核对平台结果。

## Agent 浏览工具

浏览入口输出版本为 `2`。账号管理仍使用 SDK 格式。所有浏览命令都有 `--raw`、`--requests 10`、`--seconds 30`，最大各 100。SDK 源码带有 `SNAPSHOT.json` 记录的下游补丁，来源、许可和补丁见 [THIRD_PARTY.md](THIRD_PARTY.md)。

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
| `post LINK --reader native/website/html/auto` | `native` 要求 App，`website` 要求 Web，`html` 读取无凭据的公开页面；`auto` 依次尝试可用 App、Web 和 HTML 读取器 |
| `comments LINK --sort hot/time_aes/time_desc` | 单独读取评论，不重复整个正文 |
| `replies ROOT --cursor VALUE` | 读取根楼层的游标页；未提供或未核实帖子 ID 时节点的帖子归属未知 |
| `thread LINK ROOT --around CID` | 围绕精确 CID 读取祖先与后续节点，`--pages` 控制根楼层定位和深楼读取总页数 |
| `user UID` | 返回作者公开资料，过滤草稿箱和编辑器字段 |
| `user UID --view posts/followers/following/relations` | 沿作者帖子继续浏览；`followers/following` 返回关系列表和总数，`relations` 返回平台 `count/users` 集合 |
| `favourites folders` | 当前账号的收藏夹 |
| `favourites posts FOLDER` | 收藏夹条目，默认收藏夹 ID 可为 `0` |
| `games info STEAM_APP_ID` | App 单资源游戏详情、社区、关系分组和菜单；请求 `appid` 为 Steam app ID |
| `asset HTTPS_CDN_URL` | 下载图片、保留原图并生成 PNG 预览；无账号要求，不发送 Cookie |
| `snapshot list/show/resume/diff` | 查询、继续或比较本地浏览观察 |

信息流统计的 `charges` 来自 `link_award_num`，不把服务器的占位 `up=0` 当成点赞数。缺失的统计值是 `null`。正文摘要与正文分开，`body_not_returned`、`invalid_body_json` 和未知块均有明确缺口。非空正文自身不证明平台已给出全文。

`user --view followers/following` 接受 App 顶层 `follow_list` 响应。`data.count` 是该视图的平台总数，`data.follow_cnt` 保留 `fan_num`、`follow_num` 和 `inter_follow_num`。平台可能忽略请求的 `limit` 并返回更多记录；`--limit` 仍由本地条目预算截断，并在同一页生成安全 `coverage.resume`。每次续读先消费已观察到的同页记录位置。

`user --view relations` 把平台 `/bbs/app/profile/relations` 的 `result.users` 映射到 `data.items`，把 `result.count` 映射到 `data.count`。默认输出还包含 `source` 和 `user_id`；原始响应通过 `--raw` 提供。该集合的业务含义尚未确认。空集合不表示当前账号与目标用户没有关注关系，用户列表也不证明共同关注、粉丝或互关。`count` 大于本次输出且平台没有 continuation 时，结果为 `partial/relation_collection_partial`，包含 `missing_count`，不生成 resume。

`user UID --view profile` 的 `data.relationship.current_user_follows_target` 只采用 App 资料响应中的 `user.follow_status`。只有目标非当前账号且原值是整数 `0` 或 `1` 时才返回 `false` 或 `true`；字符串、布尔值、缺失值、Web 响应和当前账号自己的资料返回 `null`。`target_follows_current` 与 `mutual` 没有可靠证据，保持 `null`。`evidence` 固定为 `profile.user.follow_status`。`relations` 集合不能代替这个判断。

分页命令用页级 continuation 续读。除 `user --view relations` 外，单资源命令一次返回的完整列表超过 `--limit` 时使用 `partial/item_budget`，在 `coverage.gaps` 中报告 `single_response_truncated`，在 `coverage.omitted` 中给出省略条数；这类结果没有平台 continuation，因此 `coverage.resume` 为 `null`。`user --view relations` 使用上一段的 `relation_collection_partial` 契约。

正文读取的 `reader_attempts` 和 `provenance` 分别记录每次读取及最终元数据、正文来源。`body_candidates` 对每个成功 reader 返回 `text_characters`、`block_count`、排序后的 `block_types`、`body_gaps` 和 `body_sha256`，不复制正文或块内容。`identity_verified` 属于实际完成会话核对的读取器；公开 HTML 不发送凭据，也不会继承 App/Web 的核验结果。显式 `--reader html` 如果同时传入 `--account`，工具会先验证该别名，只把账号的 UID 用作快照命名空间；GET 仍不携带账号凭据或签名器。公开 HTML 只从结构化元数据、文章标题或非通用页面标题提取标题，只从结构化作者或作者元数据提取作者；缺失时分别报告 `html_title_not_found` 和 `html_author_not_found`。公开 HTTP 401/403 返回 `http_access_denied`，超过 4 MiB 返回 `public_response_too_large`。除 `--section metadata` 外，所需正文为空会以 `body_missing` 失败，不会返回 `complete`。已有正文快照续读时，如果新一轮成功 reader 都没有可交付正文，工具保留旧正文、正文来源和候选摘要，并报告 `reader_body_missing_after_resume`。预算暂停仍返回安全 resume；reader 已耗尽时返回 `partial` 且不返回 resume。

`thread.data.reply_location` 给出该次指定目标的回复位置，字段为 `page`、`limit`、`sort`、`cursor`。`page` 是根楼被观察到的绝对页号；`cursor` 是实际包含目标的子评论页的输入游标，首屏和根楼预览为 `null`。读取更多页后，该位置仍对应目标，独立于最后一页的 `coverage.next`。未观察到目标时位置为 `null`。保存的已观察目标位置可随快照续读保留；旧快照缺少位置时，需重新观察目标。位置仅用于定位，发送前仍核验目标。

`post --reader native` 缺少保存的 App 会话时返回 `app_session_missing`。`post --reader website` 缺少保存的 Web 会话时返回 `web_session_missing`。显式 reader 在缺会话时不会借用另一协议。`post --reader auto` 按 App、Web、公开 HTML 的顺序尝试当前实际拥有的 reader，缺会话本身不会伪造一次网络尝试。

分页的 `completion=complete` 需要平台终点或总页数证据。空数组、缺少游标或达到预算只能产生 `unknown` 或停止状态，不能证明全量读取结束。`coverage.resume` 绑定真实账号、协议、资源和筛选；换资源或账号时被拒绝。页内与本次多页之间按 ID 去重。

验证码和限流错误的公开形状包含 `code`、`retry_allowed=false`、`cooldown_required=true` 和 `retry_after_seconds`。`retry_after_seconds` 来自 HTTP `Retry-After` 的数字或 HTTP-date；平台没有提供可信值时返回 `null`。遇到这两类错误时停止当前任务、reader fallback 和后续 query。

发现和搜索列表按记录规范化。无法解析的混合记录不会让整页丢弃可用条目；`coverage.invalid` 计数，`coverage.gaps` 加入对应的 `invalid_*_records`，`partial` 或 `unknown` 保留读取范围的不确定性。

`--page` 是绝对页号，不受单次 `--pages` 最多 100 页的预算限制。多页信息流排序在汇总并去重后执行；`returned_total` 报告本次各页实际返回数之和。App 签名调用使用剩余时间，发起 HTTP 前再次检查截止时间。`thread --raw` 返回根楼层和子评论的全部已取得原始页。

时间预算覆盖网络建立和完整响应接收；`--requests` 按实际发起的 HTTPS GET 计数，允许的同站或小黑盒子域重定向也消耗请求预算。超时会终止该网络读取并保留之前完成的页。空评论页如果仍有明确总页数证据会继续。`unknown_parent_ids` 和 `ancestry_complete` 描述精确父节点是否已知，根楼层归属不会被当作直接回复关系。影音块保留可播放地址与原始元数据，无法解析时报告缺口。评论中的内嵌图片也会保留，HTML 标题和表格单元格不会被拼成无分隔文字。

预算和页数停止后，可重复同一查询并提供 `--resume <value>`。正文读取的续读令牌保留下一个读取器位置，并绑定最初使用的 reader 方案；可用的 App 或 Web 会话改变后，旧令牌会被拒绝，调用方应重新读取。`thread` 的 `coverage.continuation` 返回可继续的 comments 页或 replies 游标。错误发生在后续页时，已读条目保留，`ok=false`，停止原因是 `platform_error`；验证码、限流、会话失效、公开 HTTP 访问失败和未知非成功状态都停止且不返回 resume，也不自动换账号、协议或入口。

依赖按需安装，核心读取和 Web 签名没有新增依赖。媒体预览需要 Pillow；文章投稿的 `markdown` 正文需要固定的 Markdown 解析器。两者互不替代，`text`/`html` 和普通读取都不需要它们。

```console
python -m pip install -r "<skill>/requirements-media.txt"
python -m pip install -r "<skill>/requirements-formatting.txt"
python -I -X utf8 "<skill>/scripts/xhh.py" asset "https://imgheybox.max-c.com/实际图片路径"
```

媒体来源必须为 HTTPS 的 `max-c.com` 或其子域，重定向也遵守该范围。下载上限 16 MiB、像素上限 4000 万，预览最长边 1600、最大 512 KiB。PNG、JPEG、GIF、WebP 可用于预览，动画标记 `first_frame`。按 URL 缓存，缓存源和预览摘要都会核对；`--refresh` 才重新下载。

正文数据保留平台返回的旧 HTTP 图片引用，但 `asset` 仍要求 HTTPS，不能把已知引用当成已成功下载。正文中作者写出的文件路径仅作为文本返回，不读取对应的宿主文件。

浏览来源的 `configured_uid` 是配置中的数据命名空间，`identity_verified` 表示该次读取是否已在网络中核对当前会话。浏览快照保存在独立的 `browsing/snapshots.sqlite3`，与发送日志分开。快照按配置 UID 分组，这属于数据命名空间，不是同一本地操作系统用户之间的身份权限边界。缓存目录和文件限制为本地所有者访问；Windows 使用受保护 DACL，POSIX 使用目录 0700、数据库 0600。离线查看不需要有效网络会话，也不能证明身份。新保存、`--since` 和网络续读会核对真实会话 UID，身份验证也计入请求预算，因此这些在线操作至少需要两个请求。`--since` 和 `snapshot diff` 只比较相同资源与筛选的查询。

`identity_verified_this_read` 明确描述当前读取，兼容字段 `identity_verified` 保持同一含义。曾经核验的互动命名空间由 `interaction.identity_established` 表示，普通公开读取的当前身份标签仍为 false。快照差异比较排除本地 `interaction` 摘要，参与状态变化不会被当作平台帖子编辑。

快照使用递增修订号拒绝并发陈旧更新，冲突时保留数据库与本次已读结果。续读既可以用 `snapshot resume`，也可以用原命令的 `--resume --snapshot`，均保留先前页。`snapshot resume NAME` 会先按名称查找未交付响应；仅当一个账号拥有该 pending 快照时，无需先加载账号配置即可离线重放。名称存在于多个账号时要求先用 `--account` 或 `--config` 选择账号。网络续读使用本次调用者选择的账号、配置、环境、协议和数据目录，不把凭据写入保存的请求参数。保存的请求参数逐项保留重复的 `--platform-filter KEY=VALUE`，值中可以包含另一个 `=`。响应与下一断点在同一事务持久化，stdout 成功写出并刷新后才确认已交付；中断时下次调用先离线重放未交付响应，不跳过数据。原型期旧 `browsing.sqlite3` 路径不自动导入。

同一查询的已有快照在续读或取得新页之前遇到不可重试错误时，工具保留原数据，将 coverage 原子更新为 `blocked`，清除 resume 和 pending，并递增 revision。该状态不会被误当作空结果，也不会保留下一次调用可复用的旧 token。可重试的传输或平台错误不修改已有快照，调用方可继续使用原续读状态。同名快照属于另一查询时保持原快照不变。首次读取失败且没有快照时不创建空快照。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> comments <link-id> --pages 3 --requests 2 --snapshot discussion
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> snapshot show discussion
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> snapshot resume discussion
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> feed <topic-id> --snapshot baseline
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> feed <topic-id> --since baseline --snapshot latest
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> snapshot diff baseline latest
```

差异为 `new`、`changed`、`unchanged`、`not_observed`，始终 `deletion_inferred=false`。来源检查时间不制造内容变化。未知身份只能使用明确标记的内容指纹，不能据此推断作者或帖子归属。快照记录公开观察，不维护人格、兴趣或互动处置策略。

## games info STEAM_APP_ID

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> --protocol app games info 570 --requests 2
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> --protocol app games info 570 --raw
```

此命令只读取 App GET `/game/get_game_detail?appid=570`，不自动读取社区 feed 或其他游戏路线。目标必须为正整数。响应中出现的每个 `steam_appid`、`appid` 都必须匹配目标；缺失、无效或不匹配会失败。`--pages > 1` 与 `--resume` 由单资源检查拒绝。`--offset` 和 `--limit` 不会作为详情请求参数发送，`--limit` 不截断嵌套关系。`--raw` 的 `data.raw_pages` 包含该次原始响应；如果身份校验或规范化在响应到达后失败，错误响应的 `data.raw_pages` 仍保留该原始响应。

`--requests` 按实际 wire request 计数，同站重定向也占额度。实际详情路线观察到一次重定向，因此 `--requests 1` 可能在取得正文前以 `partial`、`stop_reason=request_budget` 停止。单资源命令不返回 `coverage.resume`；提高请求预算后重新执行同一命令。默认 10 次请求足够覆盖这次观察到的成本，但这里不把重定向次数写成平台永久保证。

| 输出 | 合同 |
| --- | --- |
| `game` | 复用游戏规范化字段；`steam_app_id` 来自 `steam_appid`，缺失时为 `null`；简介回退到 `about_the_game`；提供 `navigation.info`、`navigation.similar_games` 及有论坛证据时的 `navigation.feed` |
| `community` | 仅接受 `topic_detail.topic_id`；无可用 ID 时为 `null`；包含 `id`、`name`、`icon_url`、`forum_available`、`game_ids` 和 `navigation.feed` |
| `related_games` | 固定按 `morelike`、`similar_games`、`publisher_games` 的顺序规范化；按游戏 ID 去重，后续记录补充可用元数据，`relations` 保留全部分组成员关系 |
| `relation_groups` | 三组始终存在，包含 `status`、`returned`、`valid`、`invalid`；有效计数按记录出现次数计算，不按去重后游戏数计算 |
| `menus` | 将 `menu_v2` 的对象条目投影为 `key`、`title`、`type`、`protocol`、`value`、`parameters`；标题支持 `title/name`，参数支持 `parameters/params` |
| `source` | 保留本次 fetch 的路由、协议、时间、配置 UID 与身份核验证据 |

`forum_available` 从 `has_bbs` 规范化为 `true`、`false` 或 `null`，优先使用详情的顶层字段，再兼容社区内字段；只有明确 `true` 才提供 feed 导航。`game_ids` 来自 `topic_detail.game.app_id`，接受单 ID 或数组，只保留有效 ID 并去重。详情读取不证明 feed 路线当次可用。

关系组的非空有效数组为 `observed`，空数组为 `observed_empty` 且不增加缺口，含无效记录或形状为 `observed_with_invalid`。未返回键为 `not_returned`，三个计数均为 `null`，缺口为 `<group>_not_returned`。不把缺少字段解释为不存在关系。非数组形状报告 `invalid_<group>_shape`，`returned=0`、`valid=0`、`invalid=1`，其中 1 表示一个无效形状，不是一个游戏记录。数组中的无效条目报告 `invalid_<group>_records`，跳过无效条目并保留其余游戏。

社区缺少键时报告 `community_not_returned`，已返回但缺少可用 ID 时报告 `invalid_community`。无效菜单形状或条目分别报告 `invalid_menu_v2_shape` 或 `invalid_menu_v2_records`。无效形状、社区或条目计入 `coverage.invalid` 和 `coverage.excluded`，经现有 finalization 返回 `partial`，不生成 resume。无效项以外的缺失字段仅报告缺口。`coverage.completion=complete` 只指单次详情资源读取完成，`full_history=false`；它不声称取得完整关系图。

证据分为 Observed detail response 与 Unverified routes。Observed 仅描述实际详情响应返回的字段和分组。`navigation` 及菜单协议是下一步候选，不继承详情请求的成功证据；`games similar` 的 `/game/get_similar_games`、`games recommended` 的 `/game/all_recommend/v2` 和由详情导出的社区 feed 需要独立实际调用才能更新各自证据。本功能实现与测试没有执行这些网络请求，离线夹具也不构成新的 Observed 网络记录。

## 有界 corpus

`corpus` 将 `feed`、帖子搜索和 hashtag 动态页收进一个可恢复 run。v1 只保存公开条目、查询成员关系和分页证据，不批量读取正文。正文使用 `post` 的 reader 单独读取。

Manifest 使用严格 JSON，不允许未知字段、重复键、非有限数字或凭据字段。一个完整示例：

```json
{
  "schema_version": 1,
  "run_id": "campus-savings",
  "account": {
    "alias": "fixture",
    "uid": "101"
  },
  "reader": {
    "protocol": "app",
    "page_size": 20
  },
  "limits": {
    "requests": 12,
    "seconds": 90,
    "unique_records": 100
  },
  "queries": [
    {
      "query_id": "campus-feed",
      "kind": "feed",
      "target": {
        "topic_id": "549999"
      },
      "filters": {
        "order": "newest"
      },
      "limits": {
        "pages": 3,
        "new_records": 40
      }
    },
    {
      "query_id": "savings-search",
      "kind": "search",
      "target": {
        "query": "存钱"
      },
      "filters": {
        "content_type": "all"
      },
      "limits": {
        "pages": 2,
        "new_records": 30
      }
    },
    {
      "query_id": "savings-hashtag",
      "kind": "hashtag_links",
      "target": {
        "hashtag_id": "23455",
        "hashtag_name": "存钱"
      },
      "filters": {},
      "limits": {
        "pages": 2,
        "new_records": 30
      }
    }
  ]
}
```

`reader.protocol` 为 `app` 或 `web`，`page_size` 为 1 到 100。`limits.requests` 和 `limits.unique_records` 是正整数，`limits.seconds` 大于 0 且不超过 120。每个 query 的 `pages` 和 `new_records` 也是正整数。相同 `run_id` 只能绑定同一 Manifest；修改配额后不能用旧 ID 覆盖原计划。

query 的 `kind`、`target` 和 `filters` 按类型约束。`feed` 的 target 为 `topic_id`。`search` 的 target 为 `query`。`hashtag_links` 的 target 必须同时提供 `hashtag_id` 和 `hashtag_name`。

`feed` 和 `search` 支持 `order`、`time_range`、`platform_filters`、`content_type`、`after`、`min_comments` 和 `sort`，`search` 还支持 `community`。`content_type` 为 `all`、`article` 或 `dynamic`。`sort` 为 `platform`、`created`、`comments` 或 `charges`。`hashtag_links` 的 `filters` 必须为空。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" corpus plan corpus.json
python -I -X utf8 "<skill>/scripts/xhh.py" corpus status campus-savings
python -I -X utf8 "<skill>/scripts/xhh.py" corpus run campus-savings --max-requests 4 --max-seconds 30 --max-new-records 20
python -I -X utf8 "<skill>/scripts/xhh.py" corpus resume campus-savings --max-requests 4 --max-seconds 30 --max-new-records 20
```

`corpus plan` 和 `corpus status` 不加载账号、会话或签名器。`corpus run` 从 Manifest 绑定账号和协议，全局参数仍可指定 `--data-dir` 或 `XHH_DATA_DIR`。`--max-requests`、`--max-seconds` 和 `--max-new-records` 只缩小本次 invocation，不能超过 Manifest 的固定配额。

每次 run 的输出 `data` 包含 `delivery_id`、`revision`、`run`、`queries`、`pages`、`new_items`、`updated_items`、`remainders` 和 `counts`。`pages` 保存位置与 coverage，`new_items` 和 `updated_items` 从去重后的 entity 引用投影生成。`counts.provider` 统计平台位置数，`counts.new` 和 `counts.updated` 统计去重实体数，`counts.deferred` 统计尚未消费的位置，`counts.invalid` 统计缺少可用实体的记录，`counts.excluded` 统计平台返回但被查询过滤的记录。SQLite 另存 query membership，记录帖子在哪些查询中出现。正文只保存一份在 entity 层，delivery 不复制正文。

run 状态为 `planned`、`running`、`paused_budget`、`paused_retryable`、`blocked_human`、`complete` 或 `failed`。query 还可处于 `pending`、`pending_remainder`、`paused_retryable` 和 `quota_reached`。`paused_budget` 在 Manifest 仍有配额时可以继续；`paused_retryable` 表示 HTTP 408、5xx、DNS、TLS、timeout 或连接重置等可重试传输错误，可直接 resume；`quota_reached` 只表示查询达到声明配额，不表示平台历史结束。

`limits.requests` 和 invocation 的 `--max-requests` 是 wire request 上限。每次持久化 `corpus_attempts` 行最多派发一次 HTTP 请求；身份核对、页面读取和 fallback 各使用独立 attempt。请求后进程退出时，reservation 保持已计费状态，避免把未知的网络副作用当成未发生。额度不足会在下一次 dispatch 前暂停。

当 request 配额为 0 但 checkpoint 中仍有同页 remainder 时，`corpus resume <run-id> --max-requests 0` 先交付并消费这些本地位置。只有同页余量耗尽且还有新页面要读时，run 才保持 `paused_budget`。

验证码、限流、会话失效和不可重试平台错误将 run 置为 `blocked_human`，同时停止后续 query、fallback 和自动重试。处理平台阻塞后显式执行：

```console
python -I -X utf8 "<skill>/scripts/xhh.py" corpus resume campus-savings --acknowledge-block
```

页面和 attempt 通过持久化 stage 一起保存。进程在请求成功、frame 提交或 delivery 确认之间退出时，后续进程先恢复已暂存页面，再读取新页面。pending delivery 只在一个 lease 持有者确认后转为已交付。一个查询的新记录上限耗尽时，同页余量优先在下次 run 中消费，之后才请求下一页。已提交页面的临时正文会从 stage 清除，正式实体内容仍只保留一份。
