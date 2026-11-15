# 阅读帖子并继续讨论

在调用方已确定账号、讨论目标和互动范围后，每轮按以下流程执行。调用方维护浏览进度、来信处置和讨论节奏。

1. 确认完整技能目录已放入宿主的技能目录，并在宿主技能列表中找到 `xiaoheihe-tools`。使用用户指定的账号来源。已有登录状态位于旧目录时，为每次调用提供相同的 `--data-dir <existing-directory>`。先运行 `account status <alias> --online`，再用 `account-info` 核验实际在线身份。别名查找失败时核对目录，保留已有账号配置。

2. 用 `feed`、`hot` 或 `search` 选帖子，再调用默认的 `post --reader auto`。Web 会话直接读取网站正文，App 会话读取原生正文。检查 `body.status`、`body.gaps` 和 `media`。若此前显式选择了 native 且 Web 正文缺失，改用 `post --reader website` 补读。正文仍为 `missing` 或 `invalid` 时结束该帖的内容判断。`available` 表示有可读取内容，全文范围仍以 `body.completeness` 为准。需要理解配图时，对图片 URL 调用 `asset`，再用宿主图像工具打开返回的 `preview_file`。

3. 每个新的收件箱检查轮次从 `inbox --offset 0` 开始。存在 `data.entries` 时使用该列表，否则使用旧 `data.messages`。在同一账号的进度中跨页、跨轮保存已观察消息的键，优先使用 `message_id`，缺少时使用来信 `comment_a_id`，过滤重叠页中的重复消息。为每条可回复的来信保留其帖子 ID、根评论 ID和来信 CID，字段含义见 [命令参考](REFERENCE.md#读取)。`coverage.next_offset` 只用于本轮历史遍历。为来信 CID选择稳定的操作 ID，例如 `incoming-<comment_a_id>`，先用 `operation <id>` 查询已有状态。已取得回执的来信进入读回步骤；结果不确定的来信保留原 ID并暂停发送。

4. 调用 `thread <linkid> <root_comment_id> --around <comment_a_id>`。按需要增加 `--pages` 或使用同一查询的续读令牌，直到目标已观察且必要的祖先链已取得。上下文不足时继续读取或结束该来信。使用该目标的 `data.reply_location` 保存根楼页号、每页大小、排序和输入游标。目标位置缺失的旧快照，需要重新读取目标所在页。

5. 根据已读帖子、配图和具体来信确定回复正文，写入 UTF-8 文件。对来信 CID回复，复用第 3 步的操作 ID，并将 `reply_location` 中的页号、大小和排序传给 `reply`。`cursor=null` 时省略 `--cursor`；有值时传入该值。`coverage.next` 和 `coverage.continuation` 描述下一次读取位置，不能替代目标所在页的输入游标。工具会再次核对根楼和精确目标。

6. 从成功回执取得新 `comment_id`，调用 `thread` 围绕这个新 CID读回。确认观察到精确 CID，并核对作者身份和 `reply_to_id`。有限范围内未观察到时保留公开可见性未知的状态，再继续读回。沿用同一操作 ID查询或重试，避免重复发送。

7. 将已观察消息键、该来信的 CID、操作 ID、回执和读回状态记入调用方的讨论进度。一条来信的处置不代表同根楼其他来信已回复。信息流可沿保存的断点继续；收件箱下一轮重新检查最新页，并保留去重集合。验证码、限流、登录失效或未知发送结果出现时保留已读内容和操作 ID，结束本轮并报告原因。

定位参数可按以下方式复用。所有调用使用相同账号来源和数据目录，全局参数置于子命令前。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --data-dir <directory> --account <alias> thread <post-id> <root-id> --around <incoming-cid> --pages 3 --limit 20 --sort time_desc
python -I -X utf8 "<skill>/scripts/xhh.py" --data-dir <directory> --account <alias> reply <post-id> <root-id> <incoming-cid> --page <reply_location.page> --limit <reply_location.limit> --sort <reply_location.sort> --text-file <reply-file> --operation-id incoming-<incoming-cid> --confirm
```

第二条命令适用于 `reply_location.cursor=null`。目标来自后续子评论页时，再加入 `--cursor <reply_location.cursor>`。完整参数和发送状态见 [命令参考](REFERENCE.md)。
