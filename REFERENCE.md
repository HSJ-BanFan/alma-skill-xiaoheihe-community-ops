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
| `feed TOPIC --offset N --limit N` | 指定板块单页原始数据 |
| `search QUERY --type link --offset N --limit N` | 帖子、游戏或用户搜索单页 |
| `post LINK --page N --limit N` | 帖子正文、图片与评论的完整单页 |
| `comments LINK --page N --limit N` | 同一帖子树，保留根楼层及回复关系 |
| `replies ROOT --cursor CURSOR` | 指定根楼层的游标页 |
| `inbox --offset N --limit N` | 平台消息页，不标记已读 |
| `my-posts --offset N --limit N` | 当前账号的帖子页 |
| `my-comments --offset N --limit N` | 当前账号的评论页 |
| `drafts --offset N --limit N` | 平台草稿页 |
| `creator-options --link-id LINK` | 编辑器板块、标签、作者计划选项 |
| `emojis` | 平台官方表情目录；服务器可能返回空组 |
| `operation ID` | 本地按真实 UID 隔离的发送状态及回执 |

收件箱的 `comment_a_id` 是来信 CID，`comment_b_id` 是被回复的 CID，`root_comment_id` 是根楼层，`linkid` 是帖子 ID。正文分别在 `comment_a_text` 和 `comment_b_text`，工具保持原始内容。

`replies` 的续读位置取 `data.lastval`。`coverage.has_more` 缺失时完成状态未知。`inbox.next_offset` 仅提供下一读取位置，不能证明后续为空。板块 ID 由用户或平台查询选择，工具不内置兴趣板块。

## 写入

所有下列命令要求 `--confirm --operation-id ID`。正文文件使用 UTF-8，评论和回复最多 1000 字符。工具在写前在线核对账号，不要求调用方提供宿主会话证明。

| 命令 | 参数 |
| --- | --- |
| `comment LINK` | `--text-file FILE`，可选互斥 `--image-file FILE` 或 `--image-url HTTPS_URL` |
| `reply LINK ROOT REPLY` | `--text-file FILE`，可选 `--page N`、`--cursor CURSOR` 定位深楼 |
| `like LINK` | 原平台 `like/combo` 接口，可能涉及充电，不保证免费 |
| `favourite LINK` | 收藏到平台默认收藏夹 |
| `unfavourite LINK` | 取消收藏 |
| `upload FILE` | PNG、JPEG 或 GIF，本地文件最多 10 MiB，返回小黑盒 CDN URL |
| `publish SPEC.json` | 默认保存平台草稿，加 `--public` 公开发布 |
| `delete-comment LINK CID` | 先核对评论归当前账号所有 |
| `delete-post LINK` | 先核对帖子归当前账号所有 |

回复使用纯文本。一级评论配图只依据调用方传入的文件或 URL，工具不会搜索本地图库，也不要求写出不配图的理由。上传使用小黑盒自身的 COS 流程。

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

写入日志只保存操作类型、真实账号 UID、请求指纹和回执，不包含人格、情绪、运营偏好或调度信息。

| 状态 | 含义 |
| --- | --- |
| `reserved` | 发送前已落盘，进程中断时可能已经发送 |
| `returned` | API 回执已落盘；相同 ID 与相同请求返回已有回执，不再次发送 |
| `uncertain` | 已进入写调用但没有完整成功回执，保持关闭 |
| `operation_id_conflict` | 相同 UID 与 ID 使用了不同请求，被拒绝 |
| `receipt_persistence_failed` | 取得回执但保存失败，输出保留该回执，旧 reservation 保持关闭 |

日志使用 SQLite 原子 reservation，多个进程不能同时取得同一 UID 与 ID 的发送权。去重范围是调用方给出的操作 ID，不把永久禁止再次评论同一个帖子当作平台限制。新的独立用户操作使用新 ID；恢复结果未知的请求沿用旧 ID，并先人工核对平台。

单次写入没有自动重试。验证码、限流和会话失效会结束当前命令。`public_visibility=not_checked` 表示未验证公开可见；平台 `ok`、CID 或本地记录都不是公开展示证据。
