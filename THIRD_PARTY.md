# 小黑盒 SDK 来源

`scripts/xhh_sdk` 基于 `HSJ-BanFan/xiaoheihe-api-collect` 的 `cli/xhh_sdk` Python 源码与 `api_catalog.json`。来源版本为 `e3e350d5724fa465d254a65ad16e241b6395a09f`，SDK 自报版本为 `0.6.0rc1`。

源仓库是 <https://github.com/HSJ-BanFan/xiaoheihe-api-collect>。本次从与该提交一致且工作树干净的本地 checkout 复制公开代码，并统一为 LF 行尾。随包副本随后修改了会话选择、协议错误边界、传输状态处理和账号信息规范路由，因此它不是上游提交的逐字节副本。`scripts/xhh_sdk/SNAPSHOT.json` 冻结当前随技能发布版本的实际 SHA-256，供可移植验证核对。保留的 MIT 许可位于 `scripts/xhh_sdk/LICENSE`。

SDK 已有的平台认证、签名、上传、发帖和账号工具在技能目录内执行，不导入来源仓库。`api_catalog.json` 的来源字段只是历史研究索引字符串，运行时不读取其指向的研究文件。目录中的历史实测字段不代表这次修改已在线复测。

复制内容不包括账号、Cookie、JAR、APK、原生库或浏览器数据。SDK 的平台认证、签名、上传和基础客户端能力位于 `scripts/xhh_sdk`；Agent 浏览契约、发送去重与宿主无关的命令入口位于 `scripts/` 其他文件，不导入 Alma 或来源仓库的运行状态。
