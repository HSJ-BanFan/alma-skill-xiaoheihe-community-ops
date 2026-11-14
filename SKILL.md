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

`post` 和 `comments` 返回平台完整单页数据，保留正文、图片地址、楼层 ID 和回复关系。需要更多内容时用 `--page` 或 `replies <root-id> --cursor <lastval>` 读取下一页。`inbox` 返回 `result.messages` 的完整消息字段，不改消息已读状态。

图片通过返回的 URL 或文件，用当前 agent 的图片读取能力查看。官方表情使用 `emojis` 返回的平台目录。内容理解和图片选择由调用方完成。

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
