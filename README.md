# Campus Agent 个人校园助手

通过一个对话入口查询校园信息、阅读资料、生成复习产物和调整学习安排。
项目正在实施 V2：扩展 Harness 通用能力、迁移旧校园功能，并为后续 UI 改版拆分状态与展示。

主要启动器为 tools/dev.py，项目路径按当前仓库根目录解析。
旧版 Python 静态界面已退出默认启动链路；尚未迁移的教务、通知等实现暂时保留，逐项验收后删除。
本轮 Harness、文档导出和复习卷任务已通过独立验收；真实校园平台及完整 V2 的验证边界见 [进度记录](docs/agent-v2-progress.md)。

当前注册 32 项校园业务工具，另由 Harness 提供网页、文件与浏览器能力。执行链修复、验证结果、复现命令和已知限制见 [Harness 验收与提交说明](docs/audit/pr-validation.md)。

## 安装与启动

需要 Python 3.11+、Node.js 24 和 npm；校园登录及通用浏览器默认使用已安装的 Microsoft Edge。

```powershell
python tools/dev.py --setup
python tools/dev.py --check
python tools/dev.py --open-browser
```

默认进入 /chat。start.cmd、start.ps1 与 python launch.py 均采用同一启动器，原 --port 参数映射为网页端口。
不需要学校登录时可运行 python tools/dev.py --platforms --open-browser。
环境初始化需要网络；Ctrl+C 关闭本次启动的服务。

仓库不包含个人数据库或演示账号；首次启动在本机创建所需数据。
已有用户可保留旧数据库 data/campus.sqlite3，迁移后的个人数据在 var/personal.db。
个人数据、凭据、浏览器配置与依赖目录不得提交。真实学校连接需要在受控窗口登录。

聊天中的“浏览器”工作区可打开助手自己的浏览器窗口完成登录，查看待提交内容并确认一次。网页读取、下载与填写由 Harness 工具执行；下载附件自动进入“文件与成果”。浏览器配置独立于日常 Edge 配置，聊天轮次结束不会关闭它。具体用法和当前验证范围见工具说明。

## 源码结构

- apps/web：React 界面。
- apps/api：FastAPI 业务和数据。
- apps/agent-runtime：官方 DeepSeek Harness 接入。
- packages/learning：解析、排程和复习引擎。
- connectors：平台适配器。
- contracts：接口与数据契约。
- tools：启动、验证和迁移工具。

[V2 合同](docs/agent-design/goal-v2-personal-assistant.md) · [验收总表](docs/agent-v2-acceptance.md) · [工具使用说明](docs/agent-design/tools-v2.md) · [迁移台账](docs/agent-v2-migration.md) · [UI 改版指南](docs/agent-design/ui-redesign-v2.md) · [历史文档](docs/history/README.md)
