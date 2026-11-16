# 创建和使用带表情的内容

以下命令中的 `<skill>` 是当前项目目录，`<data>` 是本次任务使用的同一数据目录。用 `describe --command compose` 或 `describe --command "emojis get"` 查询当前命令契约。

返回的 `argv` 是子命令序列，`input_schema` 与 `output_schema` 描述输入和 JSON 结果，`parameters` 给出字段对应的真实参数。`global=true` 的参数放在子命令前；`flags=[]` 的字段是位置参数，`encoding=json` 的字段传入 JSON 文本，`repeatable=true` 的参数可重复。文件内容优先保存为 UTF-8 JSON 再通过路径传入。以 `-` 开头的位置参数放在 `--` 后，选项值可使用 `--option=value`。

## 选择原生表情

按表达意图和使用位置查候选。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --data-dir "<data>" emojis search "无奈但不嘲讽" --usage post --avoid sarcasm --limit 3
python -I -X utf8 "<skill>/scripts/xhh.py" --data-dir "<data>" emojis get cube:43 --preview
```

返回项有官方名称、短码、用法、含义说明、注意事项和来源。`annotation_status=suggested` 表示用法说明仍是建议；`unannotated` 表示只有官方名称。需要确认图意时查看实际预览。`rank_score` 不表示选择正确的概率。`--offset` 和 `next_offset` 用于继续目录查询。

原生表情使用工具返回的 ID。使用位置为帖子正文、一级评论或楼中楼，工具分别生成官方标签或短码。目录不创建新的官方原生表情。

## 构建帖子

将所选 ID 放入 UTF-8 JSON 文件。下面用目录中的叹气表情展示结构。

```json
{
  "schema_version": 1,
  "kind": "post",
  "content": [
    {"type": "text", "text": "今天这关还是没过去"},
    {"type": "native_emoji", "id": "cube:43"}
  ]
}
```

本地准备可以省略社区。公开发帖前，通过已有 `community` 或 `creator-options` 工具读取实际社区 ID，并写入 `topic_ids`。`topic_ids` 是社区，`hashtags` 是话题。新结构化流程默认不添加原创声明，调用方可明确选择 `original`。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --data-dir "<data>" compose spec.json --dry-run --out prepared.json --preview-file preview.html
```

`compose` 没有平台写入。它输出准备结果和文字、结构预览。HTML 预览把官方贴图嵌入文件，首次获取贴图需要网络和 Pillow，生成后可离线查看。只有原生表情的帖子也有有效内容。`--protocol app` 可明确准备 App 协议内容；发送时使用相同协议。

## 登记自制图片贴纸

当前 agent 使用宿主绘图工具创建图片，再登记明确的文件。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --data-dir "<data>" stickers import drawing.png --caption "画面主体与图中文字" --meaning "表达的态度" --tag 无奈
python -I -X utf8 "<skill>/scripts/xhh.py" --data-dir "<data>" stickers search 无奈 --limit 5
```

登记返回 `sticker:<摘要>` ID、原图和首帧预览、文件摘要与调用方提供的说明。图像格式为 PNG、JPEG 或 GIF，最多 10 MiB。说明的来源是调用方，不自动成为视觉核验。

在内容中使用 `{"type":"image_sticker","id":"<登记返回的ID>"}`。帖子支持图文混排。一级评论有一个图片附件，放在文字和原生表情之后。楼中楼只使用文字和原生表情。图片贴纸的上传在发送阶段完成，登记不上传平台。

## 构建评论或回复

一级评论使用 `kind=comment`，`target` 只包含 `link_id`。楼中楼使用 `kind=reply`，提供读取所得的精确目标。

```json
{
  "kind": "reply",
  "target": {
    "link_id": "<帖子ID>",
    "root_id": "<根楼层ID>",
    "reply_id": "<被回复CID>",
    "page": 1,
    "limit": 50,
    "sort": "hot"
  },
  "content": [
    {"type": "text", "text": "明白了，谢谢"},
    {"type": "native_emoji", "id": "cube:41"}
  ]
}
```

`thread --around <CID>` 的 `reply_location` 给出页号、每页大小、排序和输入游标。后续子评论页把该游标填入 `target.cursor`。准备时保留同一次读取的目标，不通过用户名猜 CID。

## 发送与核对

当用户已经授权本次发送时，沿用明确的账号与数据目录。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --data-dir "<data>" --account <alias> send prepared.json --operation-id <本次操作ID> --confirm --public
python -I -X utf8 "<skill>/scripts/xhh.py" --data-dir "<data>" --account <alias> verify <本次操作ID>
```

`--public` 只用于公开帖子，省略则保存帖子草稿。评论和回复不带此参数。准备结果被改动、目录版本或所用表情的 CSS 布局变化、文件摘要不一致或协议不同，会停止发送并提示重新准备。

`returned` 只表示回执已保存。`verify.status=matched` 表示目标、作者和本次读取范围中的内容一致，`native_render.status=unknown` 表示还没有客户端图标显示证据，`public_visibility=not_checked` 表示未核验公开可见。目标或作者未观察到、正文有缺口时返回 `unverified`。

`uncertain`、`write_uncertain` 或回执保存失败时，先读取同一操作 ID 和已记录的目标。API 可能已经发送。恢复同一次操作继续使用原 ID；不同的新用户操作才使用新 ID。

Web 发帖的 `write_uncertain` 可在 `error.diagnostic` 中附带安全诊断。`web_post_missing_parameter` 对应精确的 `failed / 缺失参数`，`web_post_publishing_unavailable` 对应精确的 `failed / 发布功能暂不可用`。诊断的代码、路径、状态和消息均为固定字段；未知失败不附带诊断，也不输出其上游消息。

这两种诊断都是失败证据。它们不证明成功回执、公开可见、原生图标显示或图片贴纸送达。操作仍为 `uncertain`，回执仍为空，`retry_allowed=false`。诊断不写入数据库，后续 `operation` 查询不返回诊断。继续核对同一操作 ID；不要为绕过不确定状态创建新 ID 重发。
