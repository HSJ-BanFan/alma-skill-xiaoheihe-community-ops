# 投稿格式、媒体与本地预览

本文件说明 `post_format`、`content_format`、Markdown 转换、`preview-post`、上传、封面和媒体摘要。评论限制与文章、图文、热点计划门槛见 [评论与投稿规则](platform-rules.md)。

## 投稿形态与协议

本节的字段映射对应 `web` 协议的当前 Creator 表单。`preview-post` 默认使用 `web`，可用全局 `--protocol app` 准备旧 App 表单。发送时使用账号配置的协议；预览应选择相同协议。`coverage.protocol` 给出实际采用的模式。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --protocol app preview-post post.json
```

| 协议 | `post_type` | 正文和标签 | 封面与可见性 |
| --- | --- | --- | --- |
| `web` | 不选计划为 `1`，选计划为 `2` | 文章 HTML 与图片元数据；图文字面文本与尾部图片；hashtag 不带前导 `#` | 两种形态都可带封面；可见性 `1/2/3` |
| `app` 旧适配器 | 图文 `1`，文章 `3`；计划字段独立传递 | 保留 HTML、原生表情标签和图片块顺序；hashtag 保留前导 `#` | 封面和可见性仅支持显式文章；原样传递旧 `0/1/2/3` 值 |

旧 App 表单保留来源 SDK 的协议，不把当前 Web 前端映射套到 App 请求。旧可见性取值的当前含义需按 App 选项核实，不能直接套用 Web 的解释。旧 App 适配器不支持新 `authorship`、`declaration`、`source` 字段，工具在发送前明确拒绝；这不表示平台所有 App 版本都不支持它们。

底层 SDK 的 `Post` 保留旧 `content_format="html"` 和 `build(protocol="app")` 默认值。CLI spec 的 `content_format` 仍默认 `text`。`publish` 和 `preview-post` 显式传递实际协议，因此底层默认值不会改变 CLI 行为。

发帖 spec 首选 `post_format` 选择形态：

| 值 | 正文 wire 形态 | 适用内容 |
| --- | --- | --- |
| `image_text` | 一个字面 `text` 块，图片作为尾部 `img` 块 | 图文。正文只发字面纯文本，不支持富文本 |
| `article` | 一个 `html` 块，图片元数据另列为 `img` 块 | 文章。支持 HTML、Markdown 和就地图片 |

`post_type` 的字符串 `"1"` 和 `"3"` 是旧输入别名。不写 `post_format` 时，`"1"` 映射到 `image_text`，`"3"` 映射到 `article`。新调用只写 `post_format`。旧 `post_type="3"` 与 `post_format="image_text"` 冲突时工具拒绝，不会静默选择一种。

`post_type` 不是 wire 上的文章开关。当前文章和图文适配器的 wire `post_type` 为 `"1"`（普通投稿）或 `"2"`（选择了 `post_plan`）。`link_tag` 由工具派生。`article` 使用 `"11"`，`image_text` 使用 `"27"`。最终 `topic_ids` 字符串恰好为 `58144` 时，分别变成 `"21"` 和 `"28"`。

没有选择板块的 `image_text` 按 2026-11-16 当前编辑器源码使用动态分区 `topic_ids="58144"`、`link_tag="28"`。这不是个人默认社区。显式提供的板块始终保留，文章不会添加该默认值。本技能的 Web 公开发送仍要求调用方明确提供有效 `topic_ids`，这是发送前的工具校验。两种形态都支持独立封面。

## content_format

CLI spec 的 `content_format` 默认 `text`。它不选择投稿形态。以下行为描述 Web；旧 App 适配器保留图文 HTML 输入。

| 值 | 行为 | 可用形态 |
| --- | --- | --- |
| `text` | 字面纯文本。`article` 将每行包成 `<p>` 并转义 HTML；`image_text` 的 wire 保留字面文本块。 | 两种都可用 |
| `html` | 调用方提供的 HTML 原样进入一个 `html` 块。调用方负责标签合法性和转义。 | 仅 `article` |
| `markdown` | 由本工具自动转换为 HTML，再走同一 `Post.build()`。 | 仅 `article` |

Web 的 `image_text` 只接受 `text`。携带 `html` 或 `markdown` 时工具拒绝，不会把富文本静默降级成纯文本。App 的旧适配器额外接受 `image_text` 加 `html`，但仍不接受 Markdown。两种协议的 Markdown 都必须显式写 `post_format: "article"`。

把 Markdown 填进 `text` 或 `html` 时不会转换。`text` 会显示字面字符，`html` 也不会替你解析。需要自动转换时显式写 `"post_format": "article", "content_format": "markdown"`。

## 正文与图片

`article` 发射一个 `html` 块和从该 HTML 提取的 `img` 元数据块。Markdown 图片保留在原文位置、顺序、`alt`、`title` 和重复出现。`images` 数组里的图片追加到 HTML 末尾，与正文内联图片一起形成可见图片。工具不会把同一张图片复制成两份可见内容。

`img` 元数据块只描述 HTML 中已经出现的图片，不会新增可见图片。`preview-post` 的 `media` 从最终 payload 的 HTML 读回图片，因此不会把元数据重复计成额外图片。

`image_text` 发射一个字面 `text` 块，`images` 作为尾部 `img` 块。图片不能穿插在文字中间。需要交错的图文位置时使用 `article`。

`html` 格式的源 HTML 由调用方提供，`text` 和 Markdown 由工具转换成 HTML。工具不渲染富文本，不校验标签合法性，也不声称客户端显示效果。Markdown 中的原始 HTML 会被转义，不会变成活标签。

## Markdown 转换

当前适配器向小黑盒发送 HTML，不直接发送 Markdown。以下是本工具接受的写法，转换成功只证明 HTML 已生成。

| Markdown 输入 | 本地输出 |
| --- | --- |
| `# 标题` 到 `###### 标题` | `h1` 到 `h6`，保留原层级 |
| `**粗体**`、`*斜体*` | `strong`、`em` |
| `> 引文` | `blockquote` |
| `- 项目`、`1. 项目` | 无序或有序列表 |
| `[名称](https://example.com)` | 链接 |
| 三个反引号围起的代码 | `pre` 与 `code`，保留缩进和空行 |
| `![说明](上传返回的HTTPS地址)` | 原位置的图片与资源元数据 |
| `~~删除线~~`、管道表格 | 删除线和表格，客户端显示另行核验 |

转换使用可选依赖 `markdown-it-py==4.2.0` 的 `js-default` 预设，并固定：

- 原始 HTML 关闭并转义，`<script>` 等内容不会变成活标签。
- `linkify`、`typographer`、`breaks` 关闭，不额外改写链接或换行。
- 表格和删除线按该预设默认解析。
- 标题保留作者写出的 `<h1>` 到 `<h6>`，不压平也不降级。
- 代码块的字符、缩进和内部换行保留。尖括号和引号只做 HTML 转义。
- 段落、粗体、链接、引用和列表按 Markdown 语义输出 HTML。

表格、删除线和内联图片属性的**平台显示效果**没有用真实客户端验证。转换成功只证明本地 HTML 和 wire payload 已生成。

Markdown 图片会递归处理，要求解析成图片 token 的地址是绝对 HTTPS URL。输出保留原文中的位置、顺序、`alt`、`title` 和重复出现，使用当前编辑器的 `data-type="image"` 与 `data-original` 属性。工具不会下载图片，不会读取图片 URL 指向的本地文件，不会虚构宽高，也不会把 Markdown 内联图片再复制到尾部 `images`。相对路径、`http://` 和 `data:` 图片 token 会被拒绝。解析器不生成图片 token 的地址可能保留为字面文本，因此提交前要检查 `preview-post` 的媒体摘要。

把上传回执中的真实尺寸放入 `image_info`，例如 `[{"url":"上传返回的HTTPS地址","width":640,"height":360}]`。记录只含 `url`、`width`、`height`；尺寸必须为正整数。同一 URL 的记录可供多个正文引用和封面使用，不会增加图片数量。Markdown 转换会写入 `data-width`、`data-height`，发布图片元数据也使用这些实际尺寸。省略记录时工具保留已有 HTML 尺寸，未知尺寸不估算。

## 可选依赖

Markdown 分支才导入依赖。普通 `text`、`html` 预览、读取、帮助和 Web doctor 不需要它。

```console
python -m pip install -r "<skill>/requirements-formatting.txt"
```

`requirements-formatting.txt` 固定 `markdown-it-py==4.2.0`。缺少依赖时命令失败，并在错误信息中指出该文件的实际路径。失败不会创建发送台账或消耗操作 ID。媒体预览仍按需使用 `requirements-media.txt`。

## 本地预览

`preview-post` 在读取账号配置之前分派。它不读取账号、不访问网络、不占用 `--operation-id`、不写状态，也不属于写命令。输出只有一个 JSON 行：

```console
python -I -X utf8 "<skill>/scripts/xhh.py" preview-post post.json
python -I -X utf8 "<skill>/scripts/xhh.py" preview-post post.json --public
```

`data` 包含：

- `spec`：准备完成的 SDK `Post` 规格，包含默认字段和本次 `draft`。Markdown 正文已转换成 HTML，`content_format` 为 `html`，`post_format` 已解析。输入文件不被修改。
- `payload`：真实的 `Post.build()` wire payload，也就是实际发布时发送的字段。
- `media`：从该 payload 的 HTML 或图片块读出的图片顺序与属性，不是从源文本猜测。

`coverage` 明确标记 `offline=true`、`network=not_used`、`account_checked=false`、`operation_id_reserved=false`、`payload_stage=prepared` 和 `public_visibility=not_checked`。Markdown 预览还会记录实际渲染器版本。预览成功不代表平台接受、公开可见或计划审核通过。

### 预览与发布共用同一份 payload

`publish` 使用与预览相同的 `prepare_post` 和 SDK `Post.build()`。相同 spec、协议和草稿模式应产生逐字段相同的 payload。`spec` 仍是 SDK `Post` 字段，重建时用 `Post(**spec).build(protocol=coverage["protocol"])` 显式传入预览协议。

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> publish post.json --operation-id <id> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> publish post.json --public --operation-id <id> --confirm
```

`--public` 决定 `draft` 的 wire 值，JSON 里的 `draft` 会被该标志覆盖。实际发布仍要求 `--confirm`、唯一 `--operation-id`、账号加载和在线身份核对。

发布成功返回 SDK 回执，例如 `link_id`、URL 和 `draft` 状态。`coverage.public_visibility` 仍是 `not_checked`。回执只证明平台接受了请求，不证明公开可见、计划审核通过或正文渲染正确。失败时不会自动换账号或重试。结果未知时保留原操作 ID，并先在平台核对。

## 字段与 wire 取值

spec 使用 SDK `Post` 字段。未知字段、未知格式和错误类型会以配置错误拒绝，而不是静默忽略：

| 字段 | 说明 |
| --- | --- |
| `title` | 标题，默认空字符串。 |
| `content` | 正文，默认空字符串。Web 的 `image_text` 和 `article` 可以用非空 `images` 代替正文。只给图片的 Markdown 源文本无效，Markdown 分支要求源正文非空。本地预览通过不证明平台接受文章元数据或客户端显示。 |
| `content_format` | `text`、`html` 或 `markdown`，默认 `text`。 |
| `post_format` | `image_text` 或 `article`。首选字段。不写时由旧 `post_type` 推导，默认 `image_text`。 |
| `hashtags` | 话题名称数组。发送时去掉前导 `#` 并去重，名称取自当前平台选项。 |
| `image_info` | 上传图片的 `url`、`width`、`height` 记录数组。尺寸来自实际文件或上传回执。 |
| `topic_ids` | 板块 ID 数组，与 `hashtags` 不同。无板块图文按当前编辑器使用动态分区 `58144`；文章保持空。Web 公开发送要求调用方明确提供有效板块。 |
| `images` | 已上传的绝对 HTTPS 图片 URL。`image_text` 中作为尾部图片块，`article` 中追加到 HTML 末尾。 |
| `cover_url` | 独立封面 HTTPS URL，两种 `post_format` 都可使用，写入 `thumb`。不会改变正文格式或自动增加正文图片。 |
| `post_type` | 旧输入别名。不写 `post_format` 时，字符串 `"1"` 映射 `image_text`，`"3"` 映射 `article`。新调用改用 `post_format`。 |
| `visibility` | 访问权限字符串。`"1"` 所有人，`"2"` 关注者，`"3"` 仅自己。两种形态都可用，默认 `"1"`，`"0"` 无效。 |
| `authorship` | Web 作者身份接口。显式选择优先于旧 `original`。`"original"` 写 wire `original="1"`，`"repost"` 写 `original="0"`。 |
| `declaration` | 整数声明选项，`0` 未选择，`1` 和 `2` 的含义随 `authorship` 变化。原创时 `1` 为未经授权禁止转载或摘编，`2` 为转载请注明作者及出处；转载时 `1` 为已授权，`2` 为未授权。 |
| `original` | 旧布尔字段，默认 `true`。仅在未写 `authorship` 时生效。`true` 选择原创，`declaration` 默认 `0`；`false` 不写作者身份字段，也不表示转载。 |
| `source` | Web 来源字符串。调用方提供时原样写入 wire `source`。 |
| `post_plan` | 计划元数据键，需由当前 `creator-options` 确认。工具要求 1-64 个字母、数字、下划线或连字符，不要把编辑上下文名当作取值。选择后 wire `post_type="2"`。键存在不证明资格。 |
| `extra_declaration` | 整数声明选项：`1` AI 辅助创作，`2` 剧透，`3` 虚构演绎，仅供娱乐。没有自动资格推断，也不保证奖励。 |
| `draft` | 会被 CLI 的 `--public` 覆盖。 |
| `edit_link_id` | 修改自己的既有帖子，发送前核对所有权。 |

`cover_url` 和 `visibility` 都不会被静默降级。非法图片 URL、非法可见性都在发送前拒绝。普通图文不会因为封面字段变成文章，也不会丢掉调用方要求的访问限制。

`cover_url` 与正文图片是两个独立字段。封面写入 wire `thumb`，不会自动成为正文插图。两处都需要同一张图片时，分别设置正文引用与封面字段。

### 作者身份与声明

Web 的 `authorship` 可以和 `extra_declaration` 的 AI 辅助声明同时使用。示例：

```json
{
  "content": "正文",
  "post_format": "image_text",
  "authorship": "original",
  "declaration": 1,
  "extra_declaration": 1
}
```

显式 `authorship` 优先于旧 `original`。例如 `"authorship": "repost", "original": true` 仍写 `original="0"`，以 payload 为准。未写 `authorship` 时，旧 `original=true` 选择原创，`declaration` 默认 `0`；旧 `original=false` 只是不作作者身份声明，不会被解释为转载。需要明确转载时写 `"authorship": "repost"`。

`declaration` 需要作者身份语境。只写 `declaration`、`authorship` 为空且 `original=false` 时工具拒绝，避免把两种声明混在一起。

结构化 `compose` 发帖在未写 `original` 时按 `false` 处理，即不声明作者身份。需要原创或转载时显式写 `authorship`。

2026-11-15 的 `creator-options.extra_declaration_choice` 返回 1 为“包含AI辅助创作”、2 为“包含剧透”、3 为“虚构演绎，仅供娱乐”。AI 辅助稿如实使用当前声明。`original=false` 不替代 AI 声明，也不表示已满足创作计划要求。SDK 只有一个额外声明字段，多种声明同时适用时核实平台当前支持方式。

## 上传与封面步骤

先上传本地图片，再把返回的 HTTPS URL 放进 spec：

```console
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> upload cover.png --operation-id <upload-id> --confirm
python -I -X utf8 "<skill>/scripts/xhh.py" preview-post post.json
python -I -X utf8 "<skill>/scripts/xhh.py" --account <alias> publish post.json --operation-id <publish-id> --confirm
```

上传结果的图片地址在 `data.receipt.url`，真实宽高在 `data.receipt.width` 和 `data.receipt.height`。正文插图使用 Markdown 的 `![说明](上传返回的HTTPS地址)`，图片保留在正文指定位置。`images` 数组表达尾部图片，不能表达交错的图文位置。

下面的文章示例使用 Markdown。把示例 URL 替换为实际上传返回的地址。公开发帖时另从当前平台选择 `topic_ids`。

```json
{
  "title": "图片说明",
  "content": "## 说明\n\n图前文字。\n\n![截图](https://imgheybox.max-c.com/替换为实际上传路径.png)\n\n图后文字。",
  "post_format": "article",
  "content_format": "markdown",
  "images": [],
  "authorship": "original",
  "declaration": 0
}
```

封面地址写入 `cover_url`，正文格式由 `post_format` 独立选择。文章公开投稿时按当前编辑器核实封面、板块和声明要求，以及对应计划资格。

`upload` 和评论图片只接受按文件魔数识别的 PNG、JPEG、GIF，本地文件上限是 **10 MiB**；这两项都是本工具限制。扩展名不能决定格式，WebP、BMP 及其他格式会在请求平台前被拒绝。上传回执给出实际 URL 和图片宽高。需要宽高时只使用真实文件或回执中的值，不要估算。

本工具不裁剪图片，也不规定统一比例。所需封面尺寸和比例取自当前编辑器，不能把未核实的尺寸写成平台硬限制。实际编辑器可能裁剪或转换格式，需以平台回执和客户端显示为准。

## 媒体摘要与可见性

`preview-post` 的 `media.images` 按 payload 顺序给出 `ordinal`、`placement`（`inline` 或 `trailing`）、`url`，以及正文和图片块中真实存在的 `alt`、`title`、`width`、`height`。重复图片保留多个条目。Web 文章的图片元数据尾部不重复计数，旧 App 的尾部图库仍保留。`media.cover_url` 单独报告，不与正文图混在一起。

摘要从最终 payload 读回。`text` 格式里被转义的 `<img>` 字样不会计为图片。`article` 的 `img` 元数据块不会重复计数。存在图片块或预览条目都不证明平台公开显示。

## 评论配图与贴纸

本工具的一级评论可附一张图片。本工具的楼中楼接口接受文字和原生表情短码，拒绝图片参数和图片贴纸；平台客户端是否允许楼中楼图片尚未验证。需要稳定配图时改放该帖的一级评论。参数和限制见 [评论与投稿规则](platform-rules.md)。

原生表情使用 `emojis` 返回的已验证目录 ID 和短码，默认使用随技能提供的官方资源快照。需要更新时运行 `emojis refresh`。自制图片贴纸通过 `stickers import` 登记，详见 [表情内容流程](EMOJIS.md)。

## 按日期观察

以下是旧编辑器或页面观察，只作为提示，不是转换规则：

- 早期排版建议标题使用二级标题样式。本工具不把作者的 `#` 或 `###` 改成 `h2`。标题层级由作者决定。
- 旧观察提到只有尾部图片块时正文图片可能不显示。当前 wire contract 对 `image_text` 仍使用尾部图片块，对 `article` 把 `images` 追加到 HTML。两者都需要真实平台回读才能证明显示效果。
- 2026-11-16 的创作端导入观察显示，同时提供 `src` 和 `data-original` 会重复导入同一图片。本工具生成原生图片时只写 `data-original`，并标记 `data-type="image"`。
- 同日编辑器导入会将部分标题改为段落，去掉斜体、删除线和普通链接标记，展开表格并压平代码块。不要用编辑器的再次导入证明完整 Markdown 保真；API 保存与回读是另一项验证。
- 本工具保留完整 Markdown 对应的本地 HTML。平台保存和客户端渲染的支持范围需按当前回读核实，不能从本地转换成功推断。
- 同日的测试账号能完成在线身份核验、编辑器元数据读取、草稿列表读取和图片上传，但 Creator Web 新建草稿在 SDK 与平台自身编辑器中都返回 `缺失参数`。因此本地 payload、图片回执和编辑器合同证据不能宣称该账号已成功保存新文章或图文；后续真实回读成功时才更新这一结论。

2026-11-15 的编辑器长度、标题和图片数量观察见 [评论与投稿规则](platform-rules.md#编辑器元数据观察2026-11-15)。那些数字不是本工具的强制限制，也不是官方计划建议字数。当前 Web 表单的公开代码、日期和摘要见 [来源说明](../THIRD_PARTY.md)。平台页面、编辑器和客户端行为优先于这些按日期记录。
