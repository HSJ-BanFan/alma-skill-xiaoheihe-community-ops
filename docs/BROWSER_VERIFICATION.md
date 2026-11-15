# 社区浏览增强验证

本文把离线行为、真实平台观察和尚未验证的能力分开记录。测试通过只证明本地合同，真实请求成功只证明当次平台响应，二者都不能证明正文完整、历史已穷尽或公开可见。

## 2026-11-15 集成版

### 离线验证

- 完整 `unittest` 为 `282/282` 通过。覆盖 App/Web 会话隔离、reader provenance、请求和条目预算、验证码与限流停止、快照恢复、corpus 事务、跨查询去重、同页 remainder、fresh-process replay、旧数据迁移和持久化引用完整性。
- `scripts/` 与 `tests/` 的全部 Python 文件通过 `py_compile`，`git diff --check` 无输出。
- `tests/verify_portable.py` 在无关工作目录和隔离解释器中验证 21 个 SDK 文件摘要，输出 `ok=true`、`offline_state_writes=0`。Web offline doctor 不依赖 Java，夹具测试通过。
- 当前 vendored SDK 与上游来源提交的差异已在 `THIRD_PARTY.md` 说明。`SNAPSHOT.json` 冻结实际随包哈希，不表示与上游逐字节一致。

### 真实只读观察

使用已保存的 App 会话完成以下请求。没有发送短信，没有重新登录，也没有执行评论、发布、删除、点赞或收藏。

- `account-info` 读取在线身份成功。App 身份路径使用 `/account/info/`，不再先经过 `/account/info` 的 301。
- `feed --pages 1`、`hashtags links --name 存钱` 和 `stories` 均返回条目，退出码为 0，coverage 为 `partial`，完成范围保持 `unknown`。
- `banners` 返回 13 条记录；`recommended-users` 返回空列表并明确报告推荐缺口。搜索类型 `game` 的 `原神` 查询返回 10 条有效游戏记录。
- `post --reader html` 在没有账号和签名器的条件下只执行公开 HTTPS GET。所测公开帖可取得页面但正文为空，结果为 `body_missing`，不伪装成成功正文读取。
- `post --reader auto --requests 1` 在没有账号和签名器时只尝试公开 HTML，仍以 `body_missing` 结束，没有返回 `account_selection_required`，也没有借用会话。
- `post --reader website --protocol web` 在没有 Web 会话时返回 `web_session_missing`，没有泛化为 `configuration_error`，也没有借用 App 会话。
- `post --reader native --raw` 返回结构化正文、reader provenance 和成功 reader 的原始响应。平台只给出部分正文证据时，`body.completeness` 保持 `partial`。
- 一次真实 corpus run 使用 2 次 request 完成身份核对和一页 feed。该页返回 5 个 provider 位置，接纳 1 条新实体并保留 4 个同页位置；query 达到声明的 1 条新记录配额后为 `quota_reached`，run 为 `complete`，总 request 使用量保持 `2/3`。
- 进程退出后，`corpus status` 在没有账号、会话或签名器的条件下读回同一 `complete/quota_reached` checkpoint，request 使用量仍为 `2/3`。
- 这些真实请求没有出现 `show_captcha` 或限流响应。原始响应、会话修订和账号数据只保存在不提交的 `.audit/live-*` 文件中。

### 尚未验证

- 当前工作区只有 App 会话。没有用真实 Web 会话证明 `website` 能取得完整正文。
- 公开 HTML 所测帖子没有正文，因此只验证了无凭据传输、元数据缺口和 `body_missing`，没有证明公开 HTML 能恢复任意长文章。
- 混合无效搜索记录已有离线响应形状测试。真实游戏搜索本次没有产生无效记录，因此未取得生产响应中的 mixed-invalid 实例。
- corpus 的真实 `search` 与 `hashtag_links` query 尚未单独执行。它们复用已测试的 `Browser.page()` 边界，不据此声称真实平台行为已经验证。
- 平台接口、字段和风控策略会变化。任何新观察都需要重新检查 `coverage.completion`、`source`、`reader_attempts` 和稳定错误码。

## games info STEAM_APP_ID

此分支新增 App GET `/game/get_game_detail?appid=<id>` 的单资源适配。`tests/test_discovery_shapes.py` 使用带 `steam_appid`、`about_the_game`、`has_bbs`、`topic_detail.game.app_id`、三组关系和 `menu_v2` 的详情响应形状夹具。账号和图片地址为合成值，所有请求由测试 client 替代，不读取真实凭据。

离线测试验证一次 App fetch、返回身份核对、社区 feed 的正向论坛证据、跨组游戏去重与元数据补充、保留所有 `relations`、空组与未返回组的区别、无效形状和混合记录保留有效项、顶层结果形状或嵌套字段规范化失败时保留原始响应、显式空菜单参数对象、纯适配器输入不变，以及发起请求前拒绝页数、resume、无效目标和 Web 协议。预算停止且没有取得详情时不写入空快照。缺少关系键为 `not_returned` 并带缺口，不能据此宣称没有关系。单资源完成不表示取得全量历史或关系图。

定向 `test_discovery_shapes.py` 为 `34/34` 通过，其中新增 17 个游戏详情测试。完整 `unittest` 使用已装 Pillow 的本地 OCR Python 环境，`282/282` 通过。`scripts/` 与 `tests/` 的 53 个 Python 文件通过 `py_compile`，Ruff F 检查和 `git diff --check` 通过。portable verifier 验证 21 个 SDK 文件摘要，输出 `ok=true`、`offline_state_writes=0`、`fixture_tests=passed`，并明确记录 `live_platform=not_tested`。技能校验结果为 `Skill is valid!`。

2026-11-15 又对 Steam app ID `730` 做了只读调用。请求预算为 1 时，平台路线在完成一次 wire request 后仍需同站重定向，结果以 `partial/request_budget` 停止，没有伪造详情。修复后再次以 1 次请求复测，结果保持 `partial`，`data=null`，`used_requests=1`，`coverage.resume=null`，且没有写入空快照。把预算设为 2 时，真实响应返回 CS2、社区 ID `43`、`has_bbs=1`、5 个 `morelike` 游戏、6 个菜单，`similar_games` 与 `publisher_games` 未返回，`used_requests=2`。这证明当次详情解析和游戏到社区的导航数据可用，也证明请求预算必须把重定向计入。两次复测没有出现 `show_captcha` 或限流。

Observed detail response 的证据只归属于真实 `/game/get_game_detail` 响应实际出现的内容。`/game/get_similar_games`、`/game/all_recommend/v2`、详情菜单的目标协议和由详情导出的 feed 路线仍保持 Unverified；导航字段与这次详情响应不证明这些后续入口当次可用。已有历史 feed 观察也不证明每个游戏社区均有可用论坛。

## 历史真实只读记录

2026-11-14 曾使用隔离测试账号完成社区分类、社区搜索、社区详情、话题、热点、信息流、正文、评论、根楼层、作者帖子、收藏夹和图片预览的只读调用。该记录证明当时的部分平台路径可返回数据，不用于证明当前 reader、请求预算或 corpus 修复。

同一轮曾观察到 native 正文缺失而 website 可返回正文块。由于平台没有全文标记，即使 website 返回非空内容，完整性仍只能报告 `unknown`。

## 证据边界

API 接受、结构化解析、正文可用、读取范围、公开效果和写入生效是不同事实。空列表不能证明全量历史结束，非空正文不能证明全文完整，未再次观察到的条目不推断删除。

账号 UID 是本地数据命名空间，不是操作系统多租户边界。快照与 corpus 数据库限制为本地所有者访问；`identity_verified` 只描述具体在线读取，离线查看不会核验身份。

验证码、限流、会话失效和不可重试平台错误会停止当前任务。corpus 将整个 run 置为 `blocked_human`，不执行 fallback，也不生成可自动续跑的 token。恢复需要用户先处理平台状态，再显式传入 `--acknowledge-block`。HTTP 408、5xx、DNS、TLS、timeout 和连接重置等可重试传输错误将 run 置为 `paused_retryable`，不要求确认阻塞，可直接 resume。
