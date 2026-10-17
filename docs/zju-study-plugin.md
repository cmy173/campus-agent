# 浙大学习插件使用说明

本次更新日期：2026-10-17，版本 `0.3.0-alpha.1`。源码位于 [`plugins/zju-study`](../plugins/zju-study)，用于向宿主 Agent 提供本地学习工具，同时带有可单独使用的网页工作台。

**接入边界：本仓库的 Campus Agent 主程序目前没有实现 MCP 插件自动发现和工具分发。** 这次提交发布的是可独立运行、供宿主接入的学习插件，并未把主程序改成已完成工具调用的 Agent。支持 MCP 的宿主可以使用下文配置；其他宿主需按工具清单接入 REST API。

## 能做什么

- 学在浙大：同步自己的课程、课件、作业文字及 PDF 等作业附件，下载后后台解析。
- 智云课堂：读取课程、回放目录及平台已有转写；选择一节课，通过所配置的 AI 生成简略或详细总结。
- 学习资料、任务和学习计划；本地教材上传与已核对习题关联。
- 结合课件、作业、备注及用户导入的历年卷生成复习卷。
- 复习卷页面、打印及 HTML 导出显示数学公式；Markdown 导出使用标准数学标记。
- 40 个 MCP 工具以及本地 REST API，供宿主读取资料、提交任务、轮询结果。

教务网教材自动读取、在线找书下载已移除。整节音频重新识别、扫描件 OCR、板书识别尚未完成；平台已有转写不等同于重新语音识别。生成内容需要用户核对。

## 环境

- 学习插件需要 Python **3.11+**，与主程序声明的 Python 3.10 要求不同。
- Node.js/npm：满足网页 Vite 的版本要求（Node 20.19+ 或 22.12+）。
- Windows 受控学校浏览器默认使用已安装的 Microsoft Edge。
- PPTX 原样离线预览还需要 LibreOffice；PDF 离线预览不需要它。

主程序与学习插件使用**独立的 Python 虚拟环境及数据库**，不需要把两边的 requirements.txt 混在一起安装。

## 方式一：从仓库运行

在仓库根目录打开 PowerShell：

```powershell
# 首次运行：初始化学习插件和 MCP 适配器依赖
./start-study-plugin.ps1 -Setup

# 日常运行：启动后自动打开网页和学校受控浏览器
./start-study-plugin.ps1
```

如果不能运行 PowerShell 脚本，可使用以下手动命令：

```powershell
cd plugins/zju-study
python tools/dev.py --setup
./apps/api/.venv/Scripts/python.exe -m pip install -e plugins/zju-study
./apps/api/.venv/Scripts/python.exe tools/dev.py --platforms xzzd zykt --auto-ports --open-browser --state-file var/runtime.json
```

等待“启动与网页代理检查通过”，使用终端显示的网页地址。在受控浏览器内自行完成学校登录或验证码；已保存的有效登录可能直接进入首页。不要向插件或 Agent 发送账号密码。

**端口注意：Campus Agent 主程序默认占用 8765，而学习插件的学在浙大连接器也默认使用 8765。** 上面的新启动入口已开启 `--auto-ports`，会选择其他空闲端口；不要照旧手动启动一套固定端口连接器。API 和网页端口变化时，以 `plugins/zju-study/var/runtime.json` 的 `ports` 与 `web_url` 为准。

同一台机器请只运行一套学习插件。重复打开同目录时复用现有服务；从其他目录升级运行前，先停止旧版本，避免争用受控浏览器配置。Ctrl+C 或启动器“停止”用于结束本次服务，不会退出学校账号。

## 方式二：Windows 使用包

下载仓库中的 [最新 Windows 包](../releases/zju-study-plugin-0.3.0-alpha.1-20261017-math-windows.zip)，完整解压后运行其中的 `浙大学习工作台-修复版.exe`。首次使用仍需 Python、Node/npm，并在启动器点击“初始化依赖”。

EXE 是相邻源码式启动器，不是内置全部环境的单文件软件，不能只复制 EXE。仓库根目录保留的 `20261012` 压缩包是旧版本，请使用以上新包。

## 第一次使用

1. 学校受控浏览器登录后，在工作台“同步中心”检查连接。
2. 在“课程与资源”导入课程，再同步所需资料；等待下载和解析任务完成。
3. 在“设置”配置 AI 服务的 OpenAI 兼容地址、模型和密钥，先测试连接。密钥只在本机保存。
4. 在课程的复习卷界面选择已解析课件、作业和附件，填写备注，可导入历年卷作为参考。题目在 PDF 作业附件里时，保持“包含附件正文”开启。
5. 若作业仅有教材上序号，请在备注中上传题目照片或教材电子版。当前图片/扫描 PDF 仍需先得到可读正文，不能假定已经实现 OCR。
6. 预览实际发送的材料，再生成试卷；后台任务受理不代表生成成功，需等待任务完成。
7. 已生成试卷可显示/隐藏答案、打印，或导出 Markdown、学生版 HTML、答案版 HTML。

公式支持本地显示，导出的 HTML 内嵌公式引擎和字体，可断网打开。Markdown 需使用支持数学扩展的阅读器；不支持数学的纯文本编辑器仍会显示 LaTeX 标记。排版正确不代表题目或答案一定正确。

## 接入支持 MCP 的宿主

先保持学习插件服务运行，再配置宿主的 MCP stdio 服务。参考根目录 [`mcp.zju-study.example.json`](../mcp.zju-study.example.json)，把 `C:/campus-agent` 改为实际克隆目录。

```json
{
  "mcpServers": {
    "zju-study": {
      "command": "C:/campus-agent/plugins/zju-study/apps/api/.venv/Scripts/python.exe",
      "args": ["C:/campus-agent/plugins/zju-study/tools/agent_plugin.py"],
      "env": {"ZJU_STUDY_API_URL": "http://127.0.0.1:8000/api/v1"}
    }
  }
}
```

`ZJU_STUDY_API_URL` 指向学习插件后端，**不是**主程序的 8765 地址，也不是网页的 5173 地址。若启动器自动更换 API 端口，使用 runtime.json 中的 `ports.api` 值修改该配置。

从学习插件根目录检查：

```powershell
./apps/api/.venv/Scripts/python.exe tools/agent_plugin.py --check
./apps/api/.venv/Scripts/python.exe tools/agent_plugin.py --list-tools
```

宿主应在提交同步、出卷、课堂总结等任务后轮询任务状态；不要重复调用生成接口当作轮询。AI 生成会使用用户配置的模型服务，并可能计费。登录、验证码和需要用户核对的内容不能由宿主伪造成功。

接口依据：

- [插件安装说明](../plugins/zju-study/plugins/zju-study/README.md)
- [宿主调用流程](../plugins/zju-study/plugins/zju-study/AGENT_GUIDE.md)
- [40 个工具的 JSON Schema](../plugins/zju-study/plugins/zju-study/tools.json)
- [调用参数示例](../plugins/zju-study/plugins/zju-study/examples.json)
- [REST OpenAPI](../plugins/zju-study/contracts/openapi/openapi.yaml)
- [插件描述 plugin.json](../plugins/zju-study/plugin.json)

## 文件大小和常见问题

“设置 → 文件大小”可分别调整平台下载、本地上传和文档解析限制。默认下载/上传各 1024 MiB，解析 256 MiB。大文档仍会占用内存，解析还有页数、时限和解压限制。

- **作业没有可读内容**：先检查附件是否同步并解析成功；只有题号不能代替题干，扫描件可能需要 OCR。
- **文件超过限制**：查看提示是下载、上传还是解析超限，在设置中调整对应值后重试。已保存但未解析的文件不必再次上传。
- **连接器不可达**：使用同一启动器停止后重新启动，勾选所需平台，不必手动运行 session_runner。
- **学校登录过期或验证码**：在受控浏览器自行完成登录/验证，再检查连接。平台变化或网络不可达也可能导致同步失败。
- **公式仍显示源码**：刷新工作台；旧服务须重启以载入新导出代码。外部 Markdown 阅读器须启用数学支持。
- **PPT 原样预览缺少依赖**：在学习插件根目录运行 `python tools/setup_preview.py`，按其说明安装预览依赖；PDF 可直接查看。

## 数据、升级与发布边界

学习插件运行数据在其根目录下的 `var/`，模型密钥在本机密钥库，学校会话位于独立用户数据目录。GitHub 源码与 Windows 包不包含个人数据库、学校登录、模型密钥、真实课件或个人试卷。

更新前先停止服务并备份原来的 `var/`；更新源码时保留个人数据，重新初始化依赖。不要将运行后的 `var/`、浏览器 profile、密钥、node_modules 或虚拟环境提交到 GitHub。仓库尚未指定开源许可证。

[10 月 17 日公式修复记录](../plugins/zju-study/docs/release/RELEASE-20261017.md) · [其余功能与边界](../plugins/zju-study/README.md)
