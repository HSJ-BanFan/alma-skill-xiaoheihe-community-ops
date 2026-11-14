---
name: xiaoheihe-tools
description: 提供小黑盒账号、帖子、评论楼层、收件箱、搜索、图片上传、评论回复、点赞收藏和发帖工具。用户需要读取或操作小黑盒时使用。
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
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> inbox --limit 20
```

账号显式使用 `--account <alias>`、`--config <file>` 或 `--env` 之一。全局参数放在子命令前。状态目录使用 `--data-dir <directory>` 或 `XHH_DATA_DIR`，默认 `~/.xiaoheihe-tools`。

首次登录、凭据配置与 App 签名器见 [REFERENCE.md](REFERENCE.md)。Web 协议使用内置 Python 签名器；App 协议需要用户提供签名器和 Java。协议由账号配置或 `--protocol` 明确选择，失败后保持该协议。

## 平台数据

`post` 返回结构化正文，`comments` 返回独立评论节点。正文按顺序保留文字、图片和链接，未知块保留平台数据并报告缺口。`body.status` 区分可用、缺失和解析失败；`body.completeness` 只有在明确证据存在时才为 `complete`。摘要留在 `excerpt`，不会替代正文。

用 `community list/search/info/children` 找社区，`topics` 搜话题，`feed` 和 `hot` 浏览轻量条目，`search` 搜帖子、游戏或用户。信息流的创建时间、评论数和充电数排序只作用于本次返回的条目，不能当作全站排序。评论可选 `--sort hot/time_aes/time_desc`。

`feed --order newest/recommended/recent-replies` 选择平台原生发布时间、推荐或回复时间排序。`--sort` 只对已取得条目排序，两个选择的作用范围不同。

`thread <post-id> <root-id> --around <comment-id>` 读取根楼层、回复对象、祖先链和当前可取得的后续回复。`target_observed=false` 和 `missing_ancestor_ids` 表示上下文仍不足，不生成缺失楼层。

分页读取可设置 `--pages`、`--requests`、`--seconds`。`coverage.completion` 区分确认结束、还有更多和未知；预算停止时保留已读内容与 `coverage.resume`。相同资源、筛选和账号可用 `--resume` 续读。平台错误会停止且保留已取得的数据。

`user <uid>` 读取作者公开资料，`--view posts/followers/following` 继续导航。`favourites folders/posts` 读取当前账号的收藏夹和内容。`inbox` 保留平台消息字段，不改已读状态。

`asset <CDN-URL>` 下载平台图片并生成可用本地图像工具查看的 `preview_file`，需要可选 Pillow 依赖。源图与预览都有完整摘要；动画预览只展示首帧，原文件保留。官方表情使用 `emojis` 返回的平台目录。

`post --reader website` 显式选择网站正文读取方式，需要 Web 会话。该入口不会在失败后切换协议。`--section` 和 `--body-format` 可减少不需要的输出，`--raw` 仅用于检查原始响应。

普通浏览的 `source.configured_uid` 是配置中的数据命名空间，`source.identity_verified=false` 只表示本次没有核验在线会话身份。需要持续浏览时用 `--snapshot <name>` 保存公开观察与断点，`--since <name>` 查看同一查询的新增、变化和未再次观察到的条目。保存、`--since` 和网络续读会先核验在线 UID；离线 `snapshot show/resume/diff/list` 按配置 UID 隔离，不表示身份已核验。缺失条目不推断为删除。

`coverage.full_history=false` 表示有限范围。空单页不能证明全量历史已查完。平台接口缺少预期消息字段时返回错误，不把协议异常当作空收件箱。

## 写操作

调用方先确认实际账号与精确目标，读取相关帖子和楼层，再提交已经确定的正文或图片。每个写操作提供 `--confirm` 和唯一 `--operation-id`。同一次用户操作的后续查询或重试沿用该 ID。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> comment <link-id> --text-file <utf8-file> --operation-id <id> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> reply <link-id> <root-id> <reply-id> --text-file <utf8-file> --operation-id <id> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> operation <id>
```

一级评论可附 `--image-file` 或 `--image-url`。楼中楼使用纯文本，图片参数由 CLI 拒绝。回复前工具核对目标确实出现在该帖、根楼层与所选分页中。

`returned` 表示取得 API 回执，公开可见性仍需读回核对。`write_uncertain`、`reserved` 或回执持久化失败可能已经发送，保留操作 ID并到平台核对。更换操作 ID不能证明旧请求未发送。验证码、限流或登录失效时停止该任务，交用户处理。

登录、发帖、上传、收藏和删除的参数与输出见 [REFERENCE.md](REFERENCE.md)。
