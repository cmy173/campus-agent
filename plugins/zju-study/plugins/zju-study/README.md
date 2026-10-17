# ZJU Study Agent Plugin

可被宿主 Agent 调用的 MCP stdio 工具服务，复用仓库中的本地 REST 后端。不是独立主 Agent，不内置账号、模型密钥或个人资料。

## 安装

在仓库根目录执行：

```powershell
python tools/dev.py --setup
apps/api/.venv/Scripts/python.exe -m pip install -e plugins/zju-study
```

先启动后端和学校连接器（使用现有 EXE 或以下命令）：

```powershell
python tools/dev.py --platforms xzzd zykt
```

网页负责配置模型 API、受控网页登录和可视化确认。然后将根目录 `.mcp.json.example` 中 PROJECT_ROOT 替换为实际解压目录，加入宿主的 MCP 配置。Windows JSON 路径可用正斜杠。端口自动变化时修改 ZJU_STUDY_API_URL。

检查与工具清单：

```powershell
apps/api/.venv/Scripts/python.exe tools/agent_plugin.py --check
apps/api/.venv/Scripts/python.exe tools/agent_plugin.py --list-tools
```

MCP 的进程启动入口是 `tools/agent_plugin.py`；不带参数时只在 stdout 输出协议消息，诊断走 stderr。也可将本目录作为 Python 包安装后运行 `zju-study-plugin`。CLI 不负责偷偷启动多个后台或浏览器实例。

## 接入其他 Agent

- 支持 MCP：配置 stdio server，宿主通过 tools/list 和 tools/call 发现并调用工具。
- 不支持 MCP：`tools.json` 提供真实工具 JSON Schema，可转换为宿主 function calling 定义；`catalogue.py` 给出每个工具的 REST 路由映射。宿主执行器仍需自行接到本地 API，不能只把 schema 发给模型就算已执行。
- `plugin.json` 是本项目的通用能力描述，附带 schema；未冒充任何商业 Agent 或 Harness 的专用插件清单格式。
- 参数 `request` 对应 REST 请求体，遵循 `contracts/openapi/openapi.yaml`。无任意 URL 代理或读写密钥的工具。

工具完整列表见 `tools.json`；调用工作流见 [AGENT_GUIDE.md](AGENT_GUIDE.md)，示例见 [examples.json](examples.json)。

SDK 使用 [MCP 官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)，本次验证版本 1.30.0。MCP 适配器不依赖后端内部模型，宿主只通过本机 API 访问。
