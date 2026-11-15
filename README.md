# 小黑盒独立工具技能

把本目录命名为 `xiaoheihe-tools` 并放入支持 `SKILL.md` 的 agent 技能目录，或直接通过 Python CLI 调用。Python 3.10 或更高版本即可运行核心工具，无需安装 Python 包。登录浏览器的可选依赖见 [命令参考](REFERENCE.md)。

```console
python -I -X utf8 scripts/xhh.py --help
python -I -X utf8 scripts/xhh.py --protocol web doctor --offline
```

技能只接收明确的账号、目标和内容，并提供小黑盒读取与写入操作。说明入口为 [SKILL.md](SKILL.md)，命令与认证参考为 [REFERENCE.md](REFERENCE.md)。

安装时保留完整目录和支持文件，并在宿主技能列表中确认 `xiaoheihe-tools` 已加载。仅将仓库放入开发目录不会自动注册技能。复用已经登录的旧账号时，为调用提供其所在 `--data-dir`，先核验在线身份。连续浏览和回复来信使用 [CONVERSATION.md](CONVERSATION.md) 中的每轮流程。

这次改造删除了 Alma 路径、宿主运行信封、原生工具输出证据、梗词库、表情推荐服务、兴趣画像、第三方图床和跨仓库 CLI。旧 `community.py scan/read/act` 由 `scripts/xhh.py` 的直接平台命令替代，是一次不兼容变更。调用方自行维护任务队列、回复处置、执行节奏和内容策略。

浏览工具提供结构化正文、完整性证据、社区发现、评论上下文、预算续读、作者与收藏导航以及账号隔离的公开观察快照。媒体预览按需安装 `requirements-media.txt`。读取输出版本为 `2`，`post/comments` 数据结构已更新；原始平台响应通过 `--raw` 按需取得。

实现顺序、行为测试和真实只读范围见 [浏览验证记录](docs/BROWSER_VERIFICATION.md)。没有把测试通过或 API 接受当作全量正文与公开效果证明。

旧账号库与发送台账不会自动导入。配置原来账号时显式指定其所在 `--data-dir`，或使用用户提供的配置文件。新 SQLite 日志按操作 ID 去重，与旧台账并不共享发送锁。切换前结束旧的在途写入任务，并对旧的未知发送结果先在平台核对。

SDK 的 Python 源码和平台数据目录随技能提供，许可与来源见 [THIRD_PARTY.md](THIRD_PARTY.md)。Web 使用内置 Python 签名器。App 签名器与 Java 是平台协议依赖，由用户显式配置。

验证命令使用隔离测试数据，不登录真实账号，也不向平台写入。

```console
python -m unittest discover -s tests -v
python tests/verify_portable.py
```
