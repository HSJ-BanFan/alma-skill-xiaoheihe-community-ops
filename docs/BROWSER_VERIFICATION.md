# 社区浏览增强验证

本文把离线行为、真实平台观察和尚未验证的能力分开记录。测试通过只证明本地合同，真实请求成功只证明当次平台响应，二者都不能证明正文完整、历史已穷尽或公开可见。

## 2026-11-16 final reader integrity pass

### 离线验证

- 完整 `unittest` 为 `465/465` 通过。新增回归覆盖 HTTP-date `Retry-After`、bounded worker 的等待时间传递、短信登录验证码与平台 HTTP 429、结构化账号错误、未绑定身份、正文候选摘要、App 资料的单向关注状态、发送与不确定状态双写失败，以及已有快照在首轮续读被不可重试错误阻断后保留数据、清除 resume 与 pending、递增 revision。可重试平台错误与属于另一查询的同名快照都保持不变。
- 合并主分支的最新消息与互动能力后，回归覆盖原生表情目录、搜索和预览，预发送组合、发送去重和读回核对，图片贴纸，结构化 `describe`，以及 `interaction status/history/sync`；`post`、`feed` 和 `hot` 的互动摘要与原生表情归一化不增加 HTTP 请求。
- `scripts/` 与 `tests/` 通过 `compileall`。Ruff F 检查和 `git diff --check` 无错误。更新的 `SNAPSHOT.json` 通过 portable verifier，验证随包 SDK 摘要和隔离目录下的夹具测试。Skill 官方验证器返回 `Skill is valid!`。
- 回归把 `body_candidates` 限制为每个成功 reader 的摘要，不复制正文。`reader_attempts` 仍记录失败、预算停止、协议和 session revision。
- 回归只在 App、目标非本人且原值为整数 `0` 或 `1` 时设置 `current_user_follows_target`。字符串、布尔值、Web 响应和本人资料保持 `null`；反向关注与互关没有证据时也保持 `null`。
- 回归验证 `captcha_required` 和 `rate_limited` 都清除 resume。HTTP-header 或平台 envelope 的冷却错误传播 `retry_after_seconds`，没有可信等待时间时为 `null`。
- 回归验证正文快照续读不会用空 reader 覆盖已有可用正文。旧正文和来源保留，正文候选按摘要去重；一轮 reader 耗尽时返回 `partial` 和 `reader_body_missing_after_resume`，预算暂停时保留安全 resume。旧快照缺少 `provenance` 时从 `source` 恢复正文来源。
- 回归验证 managed App/Web 账号的 `snapshot resume` 恢复调用者选择的账号、配置、环境、协议和数据目录。重复的 `--platform-filter` 可以往返保存，筛选值包含另一个 `=` 时只按第一个 `=` 拆分。
- 回归验证 `interaction sync` 只有在预算停止、存储失败或可重试平台错误时返回 resume。验证码、HTTP 429、登录失效及其他不可重试错误保留已导入事实和 `coverage.continuation`，但清除 resume。

### 真实只读观察

这些请求使用已保存的 App 会话。没有发送短信、重新登录、复用迟到的验证码或执行写操作。

- 公开用户 `44117215` 的资料返回 `current_user_follows_target=false`，`target_follows_current=null`，`mutual=null`，证据为 `profile.user.follow_status`。
- 当前账号关注列表的首条记录动态选出目标 `63003884`。该目标资料返回 `current_user_follows_target=true`。这一步使用 1 次关注列表请求和 1 次资料请求。
- App 信息流、hashtag 动态页和 stories 各返回 5 条，均以退出码 0 停在 `partial/item_budget`，并提供安全续读。
- `post 196926795 --reader html --section body --body-format text --requests 1` 在没有账号和签名器时完成 1 次公开 GET。页面没有文章正文时返回 `body_missing`，正文来源仍为 HTML，且 `identity_verified=false`。
- 缺少 Web 会话时，`post --reader website` 在网络请求前返回 `web_session_missing`，不会借用已保存的 App 会话。
- `post 196926795 --reader native --section body --body-format text --raw --requests 3` 使用 1 次请求返回 1 条 `native` 候选和 1 个原始响应。正文状态为 `available`，完整性为 `partial`，包含 3 个块。
- 合并后的真实只读复测中，同一 `post` 以退出码 0 返回 `body.status=available`、`body.completeness=partial` 和 1 个原始响应。缺少可信 UID 命名空间时，互动投影明确返回 `participation=unknown`、`reason=no_trusted_uid_namespace`，不会把它写成“从未参与”。随后 `feed 549999 --pages 1 --limit 5` 以退出码 0 交付 5 条记录，每条都带本地互动摘要，coverage 为 `partial/item_budget`。两次请求均未出现验证码或限流。
- 本轮真实请求没有出现 `show_captcha` 或限流响应。

### 仍未验证

- 当前账号没有保存的 Web 会话，因此 website 正文读取仍只验证了缺会话错误，没有验证真实 Web 正文。
- 本地夹具验证了验证码、429、`Retry-After` 和原子快照阻断，但本轮没有主动制造这些平台状态。
- 当前只读证据支持“当前账号是否关注目标用户”。目标用户是否关注当前账号、双方是否互关，以及 `/profile/relations` 集合的业务含义仍为 Unverified。

## 2026-11-16 user graph and discovery follow-up

### 离线验证

- 完整 `unittest` 为 `299/299` 通过。新增回归使用真实 App 响应外层形状覆盖 `follow_list`、`follow_cnt`、关注列表忽略请求页大小、同页 remainder、`relations` 的 `count/users` 集合，以及单资源被 `--limit` 截断时的安全部分结果。
- 粉丝和关注视图兼容真实顶层 `follow_list` 与旧 `result.users`、`result.followers` 形状。平台返回 `has_more` 或 `has_more_floors` 时以其续读证据为准；两者都缺失时才由对应总数推导 `has_more`。硬条目上限仍优先。
- `relations` 不再因缺少 `items` 产生 traceback，也不推断当前账号与目标用户的关注或互关状态。集合数量大于可交付记录时返回 `partial/relation_collection_partial` 和 `missing_count`。
- 除 `user --view relations` 外，单资源完整列表大于 `--limit` 时返回 `partial/item_budget`、`single_response_truncated` 和 `omitted`，不再误报 `unsafe_item_continuation`，也不伪造 resume。`relations` 使用单独的 `partial/relation_collection_partial` 和 `missing_count`。
- 定向 `test_discussion_regressions.py` 为 `23/23`，`test_discovery_shapes.py` 为 `36/36`。Ruff F 和 `git diff --check` 通过。
- `scripts/` 与 `tests/` 通过 `compileall`。`tests/verify_portable.py` 验证 21 个 SDK 文件摘要，输出 `ok=true`、`offline_state_writes=0`、`fixture_tests=passed` 和 `live_platform=not_tested`。Skill 官方验证器返回 `Skill is valid!`。

### 真实只读观察

这些请求使用已保存的 App 会话，未发送短信、重新登录或执行写操作。

- 公开用户 `44117215` 的资料读取成功。
- 同一用户的 `followers` 返回总数 12，使用 `--limit 1` 时交付 1 条、`partial/item_budget`，并给出同页 resume。
- `following` 返回总数 30；平台一次给出超过 `--limit 1` 的记录，工具交付 1 条、保留同页 resume，没有跳过后续位置。
- `relations` 返回 `count=0/users=[]` 的成功集合。这个结果只证明该次平台的集合为空，不证明当前账号与目标用户没有关注、粉丝或互关关系。
- `feed 549999 --order create --requests 1 --pages 1 --limit 1` 返回 1 条，coverage 为 `partial/item_budget`，保留平台排序键 `create/hot-rank/reply` 和 1 个过滤器。
- `banners --requests 1 --pages 1 --limit 5` 从 13 条平台记录交付 5 条，退出码为 0，coverage 为 `partial/item_budget`，`omitted=8`，缺口为 `single_response_truncated`，resume 为 `null`。
- `topics 存钱 --limit 1` 从平台返回的 10 条记录交付 1 条，退出码为 0，coverage 为 `partial/item_budget`，缺口为 `single_response_truncated`，resume 为 `null`。
- 同一用户的 `followers --limit 10` 交付 10 条并保留总数 12、`completion=more` 和安全 resume；显式 `has_more` 与计数冲突的两种方向由离线回归覆盖。
- 本轮没有出现 `show_captcha` 或限流响应。

### 仍未验证

- `/bbs/app/profile/relations` 的 `users` 具体业务含义未确认。资料中的 `follow_status` 已支持当前账号是否关注目标用户，且只接受 App 响应中的整数 `0` 或 `1`。粉丝方向、互关方向和该集合的业务含义没有当前响应证据。
- 推荐用户当次返回空集合和 `recommendations_empty`。这不证明账号长期没有推荐，也没有验证推荐算法或跨页稳定性。

## 2026-11-16 reader follow-up

### 离线验证

- 完整 `unittest` 为 `294/294` 通过。新增回归覆盖自动 reader 在验证码或 HTTP 429 后停止、无正文文档时仍保留稳定阻断码、旧 pending 状态中的阻断错误优先于 `partial`、公开 HTML 在 void 元素后结束文章边界、显式 HTML 账号别名先于网络校验，以及旧 App-only 配置上的显式 website 读取返回 `web_session_missing`。
- 旧 pending 的独立非预算错误现在按 `retry_allowed` 重新投影。不可重试错误转为 `blocked` 并清除 resume；可重试错误转为 `retryable_failure` 并保留已有安全 resume。回放在同一事务中写入投影后的 coverage、清除 pending 并递增 revision，后续账号范围续读不会重新取得旧危险令牌。
- `scripts/` 与 `tests/` 通过 `compileall`，Ruff F 检查和 `git diff --check` 无错误。
- `tests/verify_portable.py` 验证 21 个 SDK 文件摘要，输出 `ok=true`、`offline_state_writes=0`、`fixture_tests=passed` 和 `live_platform=not_tested`。
- Skill 官方验证器返回 `Skill is valid!`。

### 真实只读观察

本轮继续使用已保存的 App 会话，没有发送短信、重新登录或执行任何写操作。每个命令都设置了明确的请求、页面和条目上限。

- `feed 549999 --pages 1 --limit 5`、`hashtags links 23455 --name 存钱 --pages 1 --limit 5` 和 `stories --pages 1 --limit 5` 均返回 5 条，退出码为 0，coverage 为 `partial`，停止原因为条目预算。
- `post 196926795 --reader html --requests 1` 在没有账号和签名器时只执行一次公开 HTTPS GET。该帖公开页面没有文章正文，因此返回 `body_missing` 和明确的 HTML 元数据缺口。
- `post 196926795 --reader website --protocol web` 在只有 App 会话时返回 `web_session_missing`，没有借用 App 会话，也没有发出替代请求。
- `post 196926795 --reader native --raw` 返回 `body.completeness=partial`、`unsupported_body_block` 缺口和成功 reader 的 `raw_responses`。
- `post 196926795 --reader auto --requests 2` 依次尝试 App 和公开 HTML，最终选择 `native`，保留两次 reader attempt，正文仍为 `partial`。没有把公开 HTML 的正文缺失覆盖到 App 正文证据上。
- 一个两查询 corpus 使用 3 次请求完成身份核对、帖子搜索 `存钱` 和 hashtag `存钱` 的单页读取，run 状态为 `complete`，请求上限为 `4`。搜索页返回 2 个 provider 位置，保留 1 条有效记录，并对另 1 条记录报告 `invalid_link_records`；hashtag 页返回 30 个位置，接纳 2 条新记录并保留 28 条同页余量。两个 query 都因声明的新记录配额以 `quota_reached` 结束。
- 本轮真实请求没有出现 `show_captcha` 或限流响应。

### 仍未验证

- 当前账号仍没有 Web 会话，因此真实 website 正文读取尚未验证。
- 所测公开 HTML 页面没有文章正文，因此没有证明该 reader 能恢复任意公开长文。
- 本地回归证明验证码、限流和旧 pending 阻断状态会清除 resume，但本轮没有主动制造这些平台状态。

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
