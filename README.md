# 小黑盒独立工具技能

把本目录命名为 `xiaoheihe-tools` 并放入支持 `SKILL.md` 的 agent 技能目录，或直接通过 Python CLI 调用。Python 3.10 或更高版本即可运行核心工具，无需安装 Python 包。文章投稿的 Markdown 转换需要 `requirements-formatting.txt` 中的固定依赖；登录浏览器和媒体预览的可选依赖见 [命令参考](REFERENCE.md)。

```console
python -I -X utf8 scripts/xhh.py --help
python -I -X utf8 scripts/xhh.py --protocol web doctor --offline
python -I -X utf8 scripts/xhh.py describe
python -I -X utf8 scripts/xhh.py preview-post post.json
```

`preview-post` 不需要账号和网络。它输出规范 spec、真实发布 payload 和从 payload 读回的媒体摘要，便于在提交前核对格式、封面和图片。预览默认 Web，可用全局 `--protocol app` 选择旧 App 表单；发送使用账号配置的协议。Web 发帖 spec 首选 `post_format`，`image_text` 发字面文本和尾部图片，`article` 接受 HTML/Markdown 和就地图片，两种都支持独立封面。评论限制、文章/图文/热点计划和投稿格式分别见 [规则参考](references/platform-rules.md) 与 [格式参考](references/formatting.md)。

技能只接收明确的账号、目标和内容，并提供小黑盒读取与写入操作。说明入口为 [SKILL.md](SKILL.md)，命令与认证参考为 [REFERENCE.md](REFERENCE.md)。

安装时保留完整目录和支持文件，并在宿主技能列表中确认 `xiaoheihe-tools` 已加载。仅将仓库放入开发目录不会自动注册技能。复用已经登录的旧账号时，为调用提供其所在 `--data-dir`，先核验在线身份。连续浏览和回复来信使用 [CONVERSATION.md](CONVERSATION.md) 中的每轮流程。

这次改造删除了 Alma 路径、宿主运行信封、原生工具输出证据、梗词库、表情推荐服务、兴趣画像、第三方图床和跨仓库 CLI。旧 `community.py scan/read/act` 由 `scripts/xhh.py` 的直接平台命令替代，是一次不兼容变更。调用方维护任务队列、执行节奏和内容策略。互动事实及来信的发送证据由本地存储维护。

浏览工具提供结构化正文、完整性证据、社区发现、评论上下文、预算续读、作者与收藏导航以及账号隔离的公开观察快照。媒体预览按需安装 `requirements-media.txt`。读取输出版本为 `2`，`post/comments` 数据结构已更新；原始平台响应通过 `--raw` 按需取得。

`corpus` 命令把多个社区、搜索词和话题纳入一个可恢复 run。Manifest 固定账号、协议、查询、过滤和配额，请求、时间、页数和唯一记录都有硬上限。每页内容、同页余量、实体去重、查询 membership 和待交付引用在同一事务保存。`corpus status` 不需要账号。验证码或限流会让整个 run 停在 `blocked_human`，恢复需要显式 `--acknowledge-block`。Corpus v1 收集公开条目，不批量抓取正文。正文仍由 `post` 的 `native`、`website`、`html` 或 `auto` reader 单独读取。

原生表情工具提供官方创作者目录快照、关键词与含义检索、原图裁剪预览，以及已知标签和短码的读取归一化。`compose` 接收文字、原生表情 ID 和登记过的图片贴纸 ID，生成准备结果与自包含 HTML 预览。`send` 复用原有账号核对和操作 ID 去重，`verify` 分别报告读回一致性、公开可见性与图标渲染范围。流程见 [表情内容](references/EMOJIS.md)。

`describe` 返回精简命令目录，`describe --command compose` 返回输入和输出 schema、真实 CLI 参数及其编码方式；`describe --command "emojis get"` 查询子命令。说明从当前 CLI 参数定义生成，agent 可以先读技能流程，再按需查询命令契约。目录查询和本地准备不需要账号，也不调用特定模型或宿主服务。图片贴纸由调用方创建文件后登记，项目不会创建新的官方原生表情。

互动历史共用 `operations.sqlite3`，按实际 UID 与 CID 隔离，自动记录评论目标、回复关系、回执、时间和可信来源。持久化记录不包含帖子正文、评论全文、图片或原始通知。`interaction status`、`interaction history` 是本地事实查询，`interaction sync` 按请求、时间和页数预算补录；`post`、`feed` 和 `hot` 自动附加轻量本地摘要。讨论原文按需要从 `post` 和 `thread` 当前读取取得。完整契约见 [互动参考](docs/INTERACTION.md)，SQLite 及字段选择见 [存储说明](docs/INTERACTION_STORAGE.md)。历史可信 UID 与当前读取的在线核验分别标记，单请求 post 保持原预算。

实现顺序、行为测试和真实只读范围见 [浏览验证记录](docs/BROWSER_VERIFICATION.md)。没有把测试通过或 API 接受当作全量正文与公开效果证明。

旧宿主账号库与发送台账不会自动导入。配置原来账号时显式指定其所在 `--data-dir`，或使用用户提供的配置文件。本工具已有的 `operations.sqlite3` 使用原子版本迁移，保留原 operations 表、状态和回执；未知历史目标不会根据哈希猜测。工具与旧宿主台账不共享发送锁。切换前结束旧宿主的在途写入任务，并对未知发送结果先在平台核对。

SDK 的 Python 源码和平台数据目录随技能提供，源码带有 `SNAPSHOT.json` 记录的下游补丁，许可与来源见 [THIRD_PARTY.md](THIRD_PARTY.md)。Web 使用内置 Python 签名器。App 签名器与 Java 是平台协议依赖，由用户显式配置。

验证命令使用隔离测试数据，不登录真实账号，也不向平台写入。

```console
python -m pip install -r requirements-test.txt -r requirements-formatting.txt
python -m unittest discover -s tests -v
python tests/verify_portable.py
```

测试依赖包含图像与 JSON Schema 验证库，不是核心运行依赖。所有发帖、评论、图片上传和读回测试使用合成响应。真实官方资源只用于核对目录和本地图像预览，API 回执与本地渲染不冒充线上公开效果。
