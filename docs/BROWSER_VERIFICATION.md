# 社区浏览增强验证

本文把离线行为、真实平台观察和尚未验证的能力分开记录。测试通过只证明本地合同，真实请求成功只证明当次平台响应，二者都不能证明正文完整、历史已穷尽或公开可见。

## 2026-11-15 集成版

### 离线验证

- 完整 `unittest` 为 `240/240` 通过。覆盖 App/Web 会话隔离、reader provenance、请求和条目预算、验证码与限流停止、快照恢复、corpus 事务、跨查询去重、同页 remainder、fresh-process replay、旧数据迁移和持久化引用完整性。
- `scripts/` 与 `tests/` 的全部 Python 文件通过 `py_compile`，`git diff --check` 无输出。
- `tests/verify_portable.py` 在无关工作目录和隔离解释器中验证 21 个 SDK 文件摘要，输出 `ok=true`、`offline_state_writes=0`。Web offline doctor 不依赖 Java，夹具测试通过。
- 当前 vendored SDK 与上游来源提交的差异已在 `THIRD_PARTY.md` 说明。`SNAPSHOT.json` 冻结实际随包哈希，不表示与上游逐字节一致。

### 真实只读观察

使用已保存的 App 会话完成以下请求。没有发送短信，没有重新登录，也没有执行评论、发布、删除、点赞或收藏。

- `account-info` 读取在线身份成功。App 身份路径使用 `/account/info/`，不再先经过 `/account/info` 的 301。
- `feed --pages 1`、`hashtags links` 和 `stories` 均返回条目，退出码为 0，coverage 为 `partial`，完成范围保持 `unknown`。
- `post --reader html` 在没有账号和签名器的条件下只执行公开 HTTPS GET。所测公开帖可取得页面但正文为空，结果为 `body_missing`，不伪装成成功正文读取。
- `post --reader website --protocol web` 在没有 Web 会话时返回 `web_session_missing`，没有泛化为 `configuration_error`，也没有借用 App 会话。
- `post --reader native --raw` 返回结构化正文、reader provenance 和成功 reader 的原始响应。平台只给出部分正文证据时，`body.completeness` 保持 `partial`。
- 一次真实 corpus run 将 Manifest 的 3 次 request 配额全部计入身份核对和一页 feed。该页返回 5 个 provider 位置，接纳 1 条新实体并保留 4 条同页 remainder。
- 在 request 已为 `3/3` 后再次 resume，执行过程没有发起新的平台页面请求，先消费本地 remainder，最终状态为 `complete`，query 状态为 `quota_reached`，request 使用量仍为 `3/3`。
- 这些真实请求没有出现 `show_captcha` 或限流响应。原始响应、会话修订和账号数据只保存在不提交的 `.audit/live-*` 文件中。

### 尚未验证

- 当前工作区只有 App 会话。没有用真实 Web 会话证明 `website` 能取得完整正文。
- 公开 HTML 所测帖子没有正文，因此只验证了无凭据传输、元数据缺口和 `body_missing`，没有证明公开 HTML 能恢复任意长文章。
- banners、recommended users 和混合无效搜索记录已有离线响应形状测试，但本轮没有重新执行对应的真实 App 请求。
- corpus 的真实 `search` 与 `hashtag_links` query 尚未单独执行。它们复用已测试的 `Browser.page()` 边界，不据此声称真实平台行为已经验证。
- 平台接口、字段和风控策略会变化。任何新观察都需要重新检查 `coverage.completion`、`source`、`reader_attempts` 和稳定错误码。

## 历史真实只读记录

2026-11-14 曾使用隔离测试账号完成社区分类、社区搜索、社区详情、话题、热点、信息流、正文、评论、根楼层、作者帖子、收藏夹和图片预览的只读调用。该记录证明当时的部分平台路径可返回数据，不用于证明当前 reader、请求预算或 corpus 修复。

同一轮曾观察到 native 正文缺失而 website 可返回正文块。由于平台没有全文标记，即使 website 返回非空内容，完整性仍只能报告 `unknown`。

## 证据边界

API 接受、结构化解析、正文可用、读取范围、公开效果和写入生效是不同事实。空列表不能证明全量历史结束，非空正文不能证明全文完整，未再次观察到的条目不推断删除。

账号 UID 是本地数据命名空间，不是操作系统多租户边界。快照与 corpus 数据库限制为本地所有者访问；`identity_verified` 只描述具体在线读取，离线查看不会核验身份。

验证码、限流、会话失效和不可重试平台错误会停止当前任务。corpus 将整个 run 置为 `blocked_human`，不执行 fallback，也不生成可自动续跑的 token。恢复需要用户先处理平台状态，再显式传入 `--acknowledge-block`。HTTP 408、5xx 等可重试平台错误将 run 置为 `paused_retryable`，不要求确认阻塞，可直接 resume。
