# 小黑盒独立工具技能

把本目录命名为 `xiaoheihe-tools` 并放入支持 `SKILL.md` 的 agent 技能目录，或直接通过 Python CLI 调用。Python 3.10 或更高版本即可运行核心工具，无需安装 Python 包。登录浏览器的可选依赖见 [命令参考](REFERENCE.md)。

```console
python -I -X utf8 scripts/xhh.py --help
python -I -X utf8 scripts/xhh.py --protocol web doctor --offline
```

技能只接收明确的账号、目标和内容，并提供小黑盒读取与写入操作。说明入口为 [SKILL.md](SKILL.md)，命令与认证参考为 [REFERENCE.md](REFERENCE.md)。

这次改造删除了 Alma 路径、宿主运行信封、原生工具输出证据、梗词库、表情推荐服务、兴趣画像、第三方图床和跨仓库 CLI。旧 `community.py scan/read/act` 由 `scripts/xhh.py` 的直接平台命令替代，是一次不兼容变更。调用方自行维护任务队列、回复处置、执行节奏和内容策略。

旧账号库与发送台账不会自动导入。配置原来账号时显式指定其所在 `--data-dir`，或使用用户提供的配置文件。新 SQLite 日志按操作 ID 去重，与旧台账并不共享发送锁。切换前结束旧的在途写入任务，并对旧的未知发送结果先在平台核对。

SDK 的 Python 源码和平台数据目录随技能提供，许可与来源见 [THIRD_PARTY.md](THIRD_PARTY.md)。Web 使用内置 Python 签名器。App 签名器与 Java 是平台协议依赖，由用户显式配置。

验证命令使用隔离测试数据，不登录真实账号，也不向平台写入。

```console
python -m unittest discover -s tests -v
python tests/verify_portable.py
```
