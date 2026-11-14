# 小黑盒 SDK 来源

`scripts/xhh_sdk` 包含 `HSJ-BanFan/xiaoheihe-api-collect` 的 `cli/xhh_sdk` Python 源码与 `api_catalog.json`。来源版本为 `e3e350d5724fa465d254a65ad16e241b6395a09f`，SDK 自报版本为 `0.6.0rc1`。

源仓库是 <https://github.com/HSJ-BanFan/xiaoheihe-api-collect>。本次从与该提交一致且工作树干净的本地 checkout 复制公开代码，并统一为 LF 行尾；每个文件的 SHA-256 见 `scripts/xhh_sdk/SNAPSHOT.json`。保留的 MIT 许可位于 `scripts/xhh_sdk/LICENSE`。

SDK 已有的平台认证、签名、上传、发帖和账号工具在技能目录内执行，不导入来源仓库。`api_catalog.json` 的来源字段只是历史研究索引字符串，运行时不读取其指向的研究文件。目录中的历史实测字段不代表这次修改已在线复测。

复制内容不包括账号、Cookie、JAR、APK、原生库或浏览器数据。SDK 代码保持原样，仅统一行尾。本技能的平台命令与发送去重位于 `scripts/xhh.py` 和 `scripts/operations.py`。
