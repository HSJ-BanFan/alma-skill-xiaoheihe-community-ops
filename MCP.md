# MCP stdio 接入

`scripts/mcp_server.py` 提供与 CLI 相同的目录、内容构建、发送和读回实现。核心只需要 Python 3.10 或更高版本。图像预览和贴纸登记按需安装 `requirements-media.txt`。

宿主配置中的路径使用当前项目的实际绝对路径。

```json
{
  "mcpServers": {
    "xiaoheihe-tools": {
      "command": "python",
      "args": ["-I", "-X", "utf8", "<项目绝对路径>/scripts/mcp_server.py", "--data-dir", "<数据目录绝对路径>"]
    }
  }
}
```

可在服务启动参数中明确提供 `--account <alias>` 或 `--config <file>`。账号配置只在平台操作时读取。服务已绑定账号时，工具调用可以省略账号来源；未绑定时，平台工具要求 `account`、`config` 或 `env=true` 之一。参数中只放配置文件路径或别名。

`tools/list` 的输入 schema 来自同一 CLI 定义，输出 schema 描述共同结果封套及准备、读回结果。`xhh_describe` 可以查询单个工具的 schema。原生表情用 `xhh_emojis_search/get`，本地图片用 `xhh_stickers_import/get/search`，内容用 `xhh_compose/send/verify`。帖子、评论、楼层、信息流和收件箱读取也通过同一 CLI 提供。

`xhh_compose` 接收 `spec` 对象，`xhh_send` 接收完整的 `prepared` 对象。MCP 调用无需先在服务器创建 JSON 文件。发送要求 `confirm=true` 和稳定的 `operation_id`。生成预览的工具结果可包含标准图片内容；指定 `preview_file` 时，组合预览还以嵌入 HTML 资源返回，资源中的图片已嵌入。

服务支持 2025-03-26、2025-06-18、2025-11-25 的 `initialize` 流程，以及 2026-07-28 的每请求协议元数据和 `server/discover`。2026 请求在 `params._meta` 中提供 `io.modelcontextprotocol/protocolVersion` 与 `io.modelcontextprotocol/clientCapabilities`。协议不支持时返回可用版本，不执行工具。

stdio 使用每行一条 JSON-RPC 消息，stdout 只有协议结果。调用不会创建小黑盒原生表情，图片贴纸使用当前 agent 的图像工具生成后登记。公开效果以实际核验范围为准。
