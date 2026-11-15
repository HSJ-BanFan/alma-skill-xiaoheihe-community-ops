# 阅读帖子并继续讨论

在调用方已确定账号、讨论目标和互动范围后，每轮按以下流程执行。工具持久化互动事实，调用方管理任务进度、讨论节奏和回复选择。证据定义见 [互动参考](docs/INTERACTION.md)。

1. 确认完整技能目录已放入宿主的技能目录，并在宿主技能列表中找到 `xiaoheihe-tools`。使用用户指定的账号来源。已有登录状态位于旧目录时，为每次调用提供相同的 `--data-dir <existing-directory>`。先运行 `account status <alias> --online`，再用 `account-info` 核验实际在线身份。别名查找失败时核对目录，保留已有账号配置。

2. 用 `feed`、`hot` 或 `search` 选帖子，再调用默认的 `post --reader auto`。检查本地 `interaction` 摘要；`unknown` 不表示从未评论。需要接着既有讨论时，查询 `interaction status <post-id> --peer <uid> --root <cid>`，用 `interaction history` 取得评论 ID、作者和回复关系，按需要用 `interaction sync` 补录。互动记录只保存这些事实；讨论原文由当前 `post` 和 `thread` 读取提供。Web 会话直接读取网站正文，App 会话读取原生正文。检查 `body.status`、`body.gaps` 和 `media`。正文仍为 `missing` 或 `invalid` 时结束该帖的内容判断。`available` 表示有可读取内容，全文范围仍以 `body.completeness` 为准。需要理解配图时，对图片 URL 调用 `asset`，再用宿主图像工具打开返回的 `preview_file`。

3. 每个新的收件箱检查轮次从 `inbox --offset 0` 开始。工具先核验实际 UID，并自动保存有效来信。存在 `data.entries` 时使用该列表，否则使用旧 `data.messages`。用帖子级 `interaction status` 查询来信处置，通知 ID 改变也不会制造新的输入回合。为每条可回复的来信保留帖子 ID、根评论 ID和来信 CID，字段含义见 [命令参考](REFERENCE.md#读取)。`coverage.next_offset` 只用于本轮历史遍历。为同一来信选择稳定操作 ID `incoming-<comment_a_id>`，先用 `operation <id>` 查询已有状态。已取得回执的来信进入读回步骤；结果不确定的来信保留原 ID并暂停发送。`unresolved_operations` 独立检查，即使同目标后来有可见回复，也不能据此重发未知操作。

4. 调用 `thread <linkid> <root_comment_id> --around <comment_a_id>`。按需要增加 `--pages` 或使用同一查询的续读令牌，直到目标已观察且必要的祖先链已取得。上下文不足时继续读取或结束该来信。使用该目标的 `data.reply_location` 保存根楼页号、每页大小、排序和输入游标。目标位置缺失的旧快照，需要重新读取目标所在页。

5. 根据已读帖子、配图和具体来信确定回复正文，写入 UTF-8 文件。对来信 CID回复，复用第 3 步的操作 ID，并将 `reply_location` 中的页号、大小和排序传给 `reply`。`cursor=null` 时省略 `--cursor`；有值时传入该值。`coverage.next` 和 `coverage.continuation` 描述下一次读取位置，不能替代目标所在页的输入游标。工具会再次核对根楼和精确目标。

6. 从成功回执取得新 `comment_id`，调用 `thread` 围绕这个新 CID读回。确认观察到精确 CID，并核对作者身份和 `reply_to_id`。有限范围内未观察到时保留公开可见性未知的状态，再继续读回。沿用同一操作 ID查询或重试，避免重复发送。

7. 用 `interaction status` 核对工具自动保存的回执、精确公开读回和未解决操作。`completed_round_count` 按已收到公开精确回复的不同对方 CID 计数。一条来信的处置不代表同根楼其他来信已回复。调用方记录任务选择和浏览进度，无需手工累计互动次数。信息流可沿保存的断点继续；收件箱下一轮重新检查最新页。验证码、限流、登录失效或未知发送结果出现时保留已读内容和操作 ID，结束本轮并报告原因。

定位参数可按以下方式复用。所有调用使用相同账号来源和数据目录，全局参数置于子命令前。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --data-dir <directory> --account <alias> thread <post-id> <root-id> --around <incoming-cid> --pages 3 --limit 20 --sort time_desc
python -I -X utf8 "<skill>/scripts/xhh.py" --data-dir <directory> --account <alias> reply <post-id> <root-id> <incoming-cid> --page <reply_location.page> --limit <reply_location.limit> --sort <reply_location.sort> --text-file <reply-file> --operation-id incoming-<incoming-cid> --confirm
```

第二条命令适用于 `reply_location.cursor=null`。目标来自后续子评论页时，再加入 `--cursor <reply_location.cursor>`。完整参数和发送状态见 [命令参考](REFERENCE.md)。
