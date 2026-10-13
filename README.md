# Campus Agent 个人校园助手

通过一个对话入口查询校园信息、阅读资料、生成复习产物和调整学习安排。
项目正在实施 V2：扩展 Harness 通用能力、迁移旧校园功能，并为后续 UI 改版拆分状态与展示。

唯一开发目录 D:/AI/campus-agent，主要启动器 tools/dev.py。
旧版 Python 静态界面已退出默认启动链路；尚未迁移的教务、通知等实现暂时保留，逐项验收后删除。
V2 尚未完成验收，当前能力与下一步见 [进度记录](docs/agent-v2-progress.md)。

## 安装与启动

需要 Python 3.11+、Node.js 24 和 npm；校园登录默认使用 Microsoft Edge。

```powershell
python tools/dev.py --setup
python tools/dev.py --check
python tools/dev.py --open-browser
```

默认进入 /chat。start.cmd、start.ps1 与 python launch.py 均采用同一启动器，原 --port 参数映射为网页端口。
不需要学校登录时可运行 python tools/dev.py --platforms --open-browser。
环境初始化需要网络；Ctrl+C 关闭本次启动的服务。

个人数据库和必要本地文件已从首轮目录保全带入，不导入演示数据。
旧数据库 data/campus.sqlite3 保留，新个人数据在 var/personal.db。
个人数据、凭据、浏览器配置与依赖目录不得提交。真实学校连接需要在受控窗口登录。

## 源码结构

- apps/web：React 界面。
- apps/api：FastAPI 业务和数据。
- apps/agent-runtime：官方 DeepSeek Harness 接入。
- packages/learning：解析、排程和复习引擎。
- connectors：平台适配器。
- contracts：接口与数据契约。
- tools：启动、验证和迁移工具。

[V2 合同](docs/agent-design/goal-v2-personal-assistant.md) · [迁移台账](docs/agent-v2-migration.md) · [首轮验收](docs/agent-acceptance.md)
