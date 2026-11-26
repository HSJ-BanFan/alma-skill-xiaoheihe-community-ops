# 协议层从 xiaoheihe-api-collect 独立 Fork 演进

`scripts/xhh_sdk` 最初拷贝自 `xiaoheihe-api-collect`，但已在客户端、载荷、配置、传输与账号管理 5 个核心模块累计大量面向智能体运营场景的下游改动。我们决定彻底断开与上游的双向同步，由本仓库独立拥有并演进完整的协议层与运营层，以消除跨仓补丁漂移并直接掌控 Web 发布排查及缺口接口迭代节奏。

## Consequences

- `scripts/xhh_sdk/SNAPSHOT.json` 仅作为历史来源记录保留，不再作为上游同步契约。
- 后续 Web 发布定性（Issue #12）及点赞、充电、用户关注、创作者数据等缺口接口的实证验证与实现，均在本仓库独立交付。
