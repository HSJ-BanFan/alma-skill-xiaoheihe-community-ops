# 小黑盒 SDK 来源

`scripts/xhh_sdk` 基于 `HSJ-BanFan/xiaoheihe-api-collect` 的 `cli/xhh_sdk` Python 源码与 `api_catalog.json`。来源版本为 `e3e350d5724fa465d254a65ad16e241b6395a09f`，SDK 自报版本为 `0.6.0rc1`。

源仓库是 <https://github.com/HSJ-BanFan/xiaoheihe-api-collect>。初始代码复制自与该提交一致且工作树干净的 checkout，并统一为 LF 行尾。SDK 的来源提交和 MIT 许可保留在技能目录内，许可位于 `scripts/xhh_sdk/LICENSE`。技能目录内的文件不是上游字节的完整副本，本技能保留并维护以下下游补丁。`SNAPSHOT.json` 记录来源修订、各文件的 SHA-256 和补丁说明。

- `client.py` 拒绝缺少目录对象的表情接口响应。Web 发帖接受顶层或 `result` 中的有效 `link_id`，空值表示该位置没有 ID，拒绝其他无效或冲突的 ID；App 继续只使用嵌套回执。上传按文件魔数只接受 PNG、JPEG 和 GIF，把真实尺寸传入投稿元数据，并统一使用本技能的 10 MiB 上限。
- `payload.py` 用独立适配器区分当前 Creator Web 与旧 App 表单。Web 区分 `article` 与 `image_text`，派生 `link_tag` 和计划模式，使用数字作者身份与声明；App 保留旧 HTML、标签和字段默认值。正文图片元数据、真实宽高和原生表情不丢失。
- `config.py` 的 Web 会话使用来源 SDK 的 `user_pkey`、`user_heybox_id` 名称，并在存在时携带已存设备 Cookie；App 保留原始名和 `x_` 前缀名。设备凭证校验 Cookie 字符并隐藏在对象显示中。
- `accounts.py` 将选定账号已加密保存的设备凭证传入请求配置，不迁移或改写账号存储。
- `interaction.py` 的普通评论和回复通过公开的 `post_state` 核验帖子存在，允许在其他用户的帖子下参与；回复保留首个楼层页的精确根楼层和目标核验，编辑、删除的所有权核验保留。公开可见性的异步行为说明保留。
- `cli.py` 的输出与错误文本同时脱敏会话和设备凭证，评论与回复帮助对应普通参与范围；canary 在调用评论前显式核验帖子归当前账号所有。
- `api_catalog.json` 的评论和回复命令说明对应当前参与范围；历史实测与未复测标记保留。
- `transport.py` 的 Web 签名请求（包括 Web 会话上传）使用 RFC3986 表单编码并声明 `application/x-www-form-urlencoded;charset=UTF-8`。仅在规范化的 `/bbs/app/api/link/post` Web POST 上发送现有签名结果的 `_rnd`，并把该请求的精确 `failed / 缺失参数` 和 `failed / 发布功能暂不可用` 映射为闭合诊断。Web 签名器和其他协议请求保留。
- `exceptions.py` 为 `XhhAPIError` 增加可选的 `SDKDiagnostic` 枚举。诊断只投影固定的代码、路径、状态和消息，不携带上游文本。

Web 协议继续使用内置 Python 签名器。2026-11-16 对当前公开创作者前端编码器的直接核对显示，签名、版本和查询身份与 SDK 一致。该签名核对没有进行平台写入。字段映射来自 [Creator 编辑器代码](https://static.max-c.com/static/heybox/webapp/@heybox-spa/creator/app/index-Dnuc1Jlq.js)，当日重新下载得到 SHA-256 `12f11584916f82147463249cdaba6f4ec6013109411d355e3f14a9e3343d645f`，与观察副本一致。编码器来自 [Creator 主包](https://static.max-c.com/static/heybox/webapp/@heybox-spa/creator/app/index-BIeKgP-u.js)，观察副本 SHA-256 为 `b1eb89a07fc086cf93e218ea6dc9366ee7a7625dd94dbff829fb1f15ef91a265`。这些证据确认源码行为，不能替代账号的真实草稿验收和客户端显示验证。

SDK 已有的平台认证、签名、上传、发帖和账号工具在技能目录内执行，不导入来源仓库。`api_catalog.json` 的来源字段只是历史研究索引字符串，运行时不读取其指向的研究文件。目录中的历史实测字段不代表这次修改已在线复测。

复制内容不包括账号、Cookie、JAR、APK、原生库或浏览器数据。本技能的平台命令与发送去重位于 `scripts/xhh.py` 和 `scripts/operations.py`。

发帖诊断只说明失败条件，不证明平台接受发帖、返回成功回执或公开可见。CLI 在不确定发送时临时输出诊断，不写入操作数据库，也不允许自动重发。

## 官方表情目录资料

`scripts/data/native_emojis.json` 保存官方创作者网页公开资源中的表情名称、ID、字体代号、贴图地址和布局，不包含平台图片二进制。来源 JavaScript 与 CSS 的地址、观察时间和摘要在该文件的 `source` 中。`emojis refresh` 从公开官方资源重新提取目录，不执行远端 JavaScript。

部分条目附带 agent 查看官方贴图后写出的画面说明与使用建议，标记为 `annotation_status=suggested`。未标注条目只保留官方名称，不把推测当作视觉核验。图片素材的权利归相应权利人，工具按平台资源地址获取并在本地预览。

## 可选 Markdown 依赖

`post_format: "article"` 配合 `content_format: "markdown"` 时使用 [markdown-it-py](https://github.com/executablebooks/markdown-it-py) **4.2.0**，通过 `requirements-formatting.txt` 固定版本。该库以 MIT 许可证发布，本仓库不复制其源码，只作为可选运行时依赖安装。普通 `text`/`html` 正文、读取、帮助和 Web doctor 不需要它。

转换由本仓库的 `scripts/postspec.py` 调用，平台 I/O 仍由随技能提供的 SDK 完成。

## 公开规则来源

`references/platform-rules.md` 中标记为“官方指引 2026-11-15”的内容来自以下公开创作计划介绍页，抓取日期为 2026-11-15：

- <https://web.xiaoheihe.cn/bbs/creation_plan_intro?type=article>
- <https://web.xiaoheihe.cn/bbs/creation_plan_intro?type=pt_link>
- <https://web.xiaoheihe.cn/bbs/creation_plan_intro?type=hotspot>

计划上下文来自 2026-11-15 的当前编辑器元数据观察，只证明名称和上下文存在。同日观察到的正文、标题、图片数量和 `new_limit` 也只描述编辑器元数据，不是官方计划建议，也不是本技能代码强制的限制。其余格式与排版说明是按日期观察或平台决定，正文中已分别标注。它们不是平台官方承诺，也不包含个人账号、兴趣或本地路径。
