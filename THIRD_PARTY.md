# 小黑盒 SDK 来源

`scripts/xhh_sdk` 基于 `HSJ-BanFan/xiaoheihe-api-collect` 的 `cli/xhh_sdk` Python 源码与 `api_catalog.json`。来源版本为 `e3e350d5724fa465d254a65ad16e241b6395a09f`，SDK 自报版本为 `0.6.0rc1`。

源仓库是 <https://github.com/HSJ-BanFan/xiaoheihe-api-collect>。本次从与该提交一致且工作树干净的本地 checkout 复制公开代码，并统一为 LF 行尾。随包副本随后修改了会话选择、协议错误边界、传输状态处理和账号信息规范路由，因此它不是上游提交的逐字节副本。`scripts/xhh_sdk/SNAPSHOT.json` 冻结当前随技能发布版本的实际 SHA-256，供可移植验证核对。保留的 MIT 许可位于 `scripts/xhh_sdk/LICENSE`。

SDK 已有的平台认证、签名、上传、发帖和账号工具在技能目录内执行，不导入来源仓库。`api_catalog.json` 的来源字段只是历史研究索引字符串，运行时不读取其指向的研究文件。目录中的历史实测字段不代表这次修改已在线复测。

复制内容不包括账号、Cookie、JAR、APK、原生库或浏览器数据。除下列下游修复外，SDK 源码保持原样，保留原始来源提交与许可。`SNAPSHOT.json` 同时记录本地文件摘要和下游补丁说明。SDK 的平台认证、签名、上传和基础客户端能力位于 `scripts/xhh_sdk`；Agent 浏览、互动、表情、发送去重与宿主无关的命令入口位于 `scripts/` 其他文件，不导入 Alma 或来源仓库的运行状态。

- `client.py` 拒绝缺少目录对象的表情接口响应。Web 发帖接受顶层或 `result` 中的有效 `link_id`，空值表示该位置没有 ID，拒绝其他无效或冲突的 ID；App 继续只使用嵌套回执。
- `transport.py` 仅在规范化的 `/bbs/app/api/link/post` Web POST 上发送现有签名结果的 `_rnd`，并将该请求的精确 `failed / 缺失参数` 和 `failed / 发布功能暂不可用` 映射为闭合诊断枚举。请求正文、Web GET、其他 Web 请求及 App 请求保持现有契约。
- `exceptions.py` 为 `XhhAPIError` 增加可选的 `SDKDiagnostic` 枚举。诊断只投影固定的代码、路径、状态和消息，不携带上游文本。
- `payload.py` 保留 `data-emoji` 标签的短码用于摘要与字数，并支持非空的纯图片正文。

发帖诊断只说明失败条件，不证明平台接受发帖、返回成功回执或公开可见。CLI 在不确定发送时临时输出诊断，不写入操作数据库，也不允许自动重发。

## 官方表情目录资料

`scripts/data/native_emojis.json` 保存官方创作者网页公开资源中的表情名称、ID、字体代号、贴图地址和布局，不包含平台图片二进制。来源 JavaScript 与 CSS 的地址、观察时间和摘要在该文件的 `source` 中。`emojis refresh` 从公开官方资源重新提取目录，不执行远端 JavaScript。

部分条目附带 agent 查看官方贴图后写出的画面说明与使用建议，标记为 `annotation_status=suggested`。未标注条目只保留官方名称，不把推测当作视觉核验。图片素材的权利归相应权利人，工具按平台资源地址获取并在本地预览。
