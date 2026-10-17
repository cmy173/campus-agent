# 浙大学习插件 · ZJU Study

这是供宿主 Agent 调用的本地学习能力插件，同时保留网页配置界面和 Windows EXE 启动器。宿主负责对话与决策，本插件负责读取学习资料、运行任务和返回结果。

当前发布：`0.3.0-alpha.1`。属于可试用版本。

- 复习卷支持数学公式显示与打印；Markdown 规范化公式标记，学生版/答案版 HTML 可离线显示公式。

## 功能

- 学在浙大课程、课件、作业同步；智云课堂课程与回放目录、已有语音转写读取。
- 本地资料库、文档解析、PPT 离线预览、任务和学习计划。
- 自定义 AI API，结合课件、作业、备注与用户上传的历年卷生成复习卷。
- 独立课堂总结，支持简略/详细、历史保存、复制与 Markdown 导出。
- 本地教材库、准确版本关联、题号与实际习题正文匹配、可选许可声明检查。
- MCP stdio 工具接口和现有 REST API，供宿主 Agent 接入。

## 快速开始

需要 Python 3.11+、Node.js/npm；Windows 学校连接器默认使用 Microsoft Edge。

```powershell
python tools/dev.py --setup
python tools/dev.py --platforms xzzd zykt
```

打开终端显示的网页，在受控浏览器登录学校网站。模型地址与密钥在网页设置中填写。持久登录与 SSO 可复用，不必主动退出账号。

Windows 使用包可双击根目录 `浙大学习工作台-修复版.exe`。EXE 依赖旁边源码与首次安装的环境，不能单独复制使用。源码仓库默认不提交 EXE；需要时运行 `tools/build_launcher.ps1`。

## 接入主 Agent

```powershell
apps/api/.venv/Scripts/python.exe -m pip install -e plugins/zju-study
apps/api/.venv/Scripts/python.exe tools/agent_plugin.py --check
```

将 `.mcp.json.example` 中 PROJECT_ROOT 改为解压目录，加入宿主 MCP 配置。先启动本地后端，再启动 MCP 适配器。自动换端口后，要同步修改 ZJU_STUDY_API_URL。

[插件安装与接入](plugins/zju-study/README.md) · [宿主调用工作流](plugins/zju-study/AGENT_GUIDE.md) · [工具 JSON Schema](plugins/zju-study/tools.json) · [REST OpenAPI](contracts/openapi/openapi.yaml)

`plugin.json` 为本项目的通用描述格式；没有声称兼容某个未指定 Harness 的私有安装规范。不支持 MCP 的 Agent 可以按工具映射调用 REST API。

## 当前验收边界

- 简略课堂总结已完成真实课堂端到端测试；详细版分支已通过自动化测试，未单独完成真实模型验收。
- 智云同课程连续同步实测 8.50 秒 / 0.26 秒，后者命中进程内缓存，不代表所有首次同步耗时。
- 已移除教务网教材自动读取与在线找书下载；教材通过本地文件导入。
- 扫描教材 OCR、整节音频重新识别、板书识别未完成；试验脚本不属于发布功能。
- 平台页面可能变化；登录、验证码和权限问题会明确返回，未读取材料不作为已读内容。AI 内容需核对。

## 数据与开发

运行数据默认保存在 `var/personal.db`、`var/personal-files`；模型密钥在本地密钥库，学校会话在独立受控浏览器 profile。发布包不包含这些数据。

目录：`apps/api` 后端；`apps/web` 配置网页；`connectors` 学校适配；`packages/learning` 学习引擎；`plugins/zju-study` Agent 插件；`contracts` API 契约；`tools` 启动与打包。

测试必须使用独立数据库，不能对个人库执行 pytest 建表或 app.seed。插件测试和发布说明见 [发布说明](docs/release/RELEASE-20261016.md)。尚未指定开源许可证。
