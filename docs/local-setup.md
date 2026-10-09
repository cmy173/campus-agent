# 本地运行与 OpenAI 配置

登录入口见 [通行证与离线缓存](passport-login.md)：直接使用浙大通行证登录，程序自动获取教务信息；已有本机会话默认展示本人缓存。校园密码不保存，也不作为本地密码哈希；请勿通过 GitHub issue、配置文件或聊天发送给协作者。

适用版本：整合预览版。Python 3.10+；本次在 Windows / Python 3.12 验证。所有命令在解压后的项目根目录执行。

学业日历的程序校历读取需要安装更新后的 requirements.txt：PDF 渲染依赖 pypdfium2，Windows 本机识别依赖 PyWinRT 和系统可用的中文 OCR 语言。语言包不可用、非 Windows 或来源解析失败时，仍可使用同学期缓存或默认日期展示课表、今日课程与成绩概览；不会发送校历给外部模型。校历核对的适用范围见 [学业界面说明](academic-ui.md)。

ETA 基本信息适配新增 cryptography 依赖。已有环境更新后执行 `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`，再重启服务并刷新页面。

修改 Python 连接器后，需要在原服务终端 Ctrl+C，再运行 `.\.venv\Scripts\python.exe -m campus_agent.server`；只刷新网页不能重新加载后端。打开 `/api/health`，campus_connector_revision 为 `2026-10-10.timetable-cache` 表示已加载本次课表缓存保留与学期记录合并（包含此前登录与学籍修复）。缓存默认保留，重新登录更新后才会生成新同步记录；无需删除数据。

适用版本：v0.2.1。Python 3.10+；本次在 Windows / Python 3.12 验证。所有命令在解压后的项目根目录执行。

## 推荐启动方式

从 GitHub 下载 ZIP 后完整解压，再启动。Windows 双击根目录 `start.cmd`；macOS/Linux 执行 `sh start.sh`。先安装 Python 3.10+（推荐 3.12），无需预装 Git、Node 或开发工具。

启动器自动创建本项目 `.venv` 并安装固定版本依赖。成功后记录依赖清单指纹；依赖清单变化时重新安装。启动窗口需保持打开，Ctrl+C 停止。每台电脑使用自己的 data 目录和账号，校园通行证不是本地账号。

排错：
- 找不到 Python：安装 Python 并启用 PATH，重新打开终端。
- 依赖下载失败：检查网络，再次启动会重试，不会标记为安装成功。
- Linux 缺少 venv/ensurepip：通过发行版包管理器安装对应 Python 的 venv 支持。
- 环境损坏：先尝试 `python launch.py --repair`；若虚拟环境已失效，停止应用后删除 `.venv` 再启动，保留 `data`。
- 端口占用：`python launch.py --port 8766`；启动器不会擅自结束其他程序。
- 网页打不开：查看启动窗口错误；服务关闭后本地网址无法访问。
- PowerShell 脚本被限制：使用 `start.cmd`，不必修改系统执行策略。

升级前先停止程序并备份 data。Git 用户拉取更新后重新启动；ZIP 用户将新版本解压到新目录，再复制自己的 data（以及自行设置的配置）到新目录，不要拷贝旧 `.venv`。

## 手动安装（可选）

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m campus_agent.server
```

打开 http://127.0.0.1:8765 ，在通行证表单登录并按需手填验证码，登录时自动获取信息；下次打开优先显示本浏览器原账号的缓存。旧本地账号从“旧版本地资料登录”进入。后续可运行 `./start.ps1` 或最后一条 Python 命令；Ctrl+C 停止服务。端口冲突时追加 `--port 8766`，并打开对应地址。

macOS/Linux 对应使用 `python3 -m venv .venv` 和 `.venv/bin/python`。本次未在这两个系统实际运行。

前端无需 Node、npm 或单独构建，所有样式与脚本从本机加载。数据默认放在项目 `data/campus.sqlite3`，数据库包含文件原件、提取文字、账号和待办；整个 data 目录不要公开、提交或发给别人。自定义目录时在启动前设置 CAMPUS_DATA_DIR。

## 验证第一个闭环

1. 通行证登录或恢复已有缓存后，进入“我的资料”，导入演示资料。
2. 等待状态从“等待解析”变成“已整理”。
3. 在校园助手询问“实验报告截止时间”，点击引用查看第 2 行原文。
4. 进入“待办与截止”，核对时间后保存，或改为“已完成”。
5. 上传自己的授权材料。通知类型自动提取候选事项，课程/培养方案类型仅加入检索。
6. 单份删除或设置中的“清除我的全部资料”会级联删除派生内容。

## OpenAI：由你在本机配置

用户已选择预留 OpenAI API；本次没有使用真实密钥、发送真实模型请求或验证账户额度。你需要一个具有 Responses API 和 Structured Outputs 使用权限的模型 ID。模型由 OPENAI_MODEL 显式指定，本项目不猜测你能使用哪个模型。

以下方式避免将密钥直接写入命令历史。仅在本机 PowerShell 执行，不要把密钥发到聊天里：

```powershell
$campusSecret = Read-Host 'OpenAI API Key' -AsSecureString
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new('', $campusSecret).Password
$env:OPENAI_MODEL = Read-Host '模型 ID'
.\.venv\Scripts\python.exe -m campus_agent.server
# 停止服务后清除当前终端变量：
Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
```

`.env.example` 仅说明变量，应用不自动读取 `.env`。密钥保留在服务端进程环境，不下发浏览器，不写入数据库或应用日志。没有配置时 UI 禁用模型开关；配置并重启后仍需在每次会话中自行勾选“使用 OpenAI”。

启用模型后，应用只发送当前问题、最多 4 个匹配片段及片段位置，最多约 4,400 字资料；不发送整份文件和账号信息。无检索依据不调用模型。每次输出上限 1,600 token，网络超时 25 秒；当前进程每个账号最多 20 次模型请求/小时，重启后计数重置。这不是财务硬预算，应同时在供应商侧设置额度。

接口固定为 `https://api.openai.com/v1/responses`，不接受浏览器指定的 API URL，也不跟随重定向。请求使用 `text.format` 的严格 JSON schema，校验返回的来源 ID 属于当前检索集。该校验保证来源存在，不证明每句话的事实准确性。

设置 `store=false` 避免按默认响应存储行为保存响应；这不构成零数据保留承诺。使用前需确认材料可以交由外部模型处理，并核查账户数据政策。参考：[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)、[Responses 迁移说明](https://developers.openai.com/api/docs/guides/migrate-to-responses)。

请求失败、拒答、引用错误或超时时显示原因并回退本地原文，不伪造模型答案。不自动重试收费请求。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试仅使用合成资料与模拟模型响应，不消耗 API 额度。测试创建并清理 `.test-tmp/` 下的临时数据库，可用 CAMPUS_TEST_TMP 指定其他测试目录。

## 备份与恢复

当前单进程应用，未实现自动备份。先 Ctrl+C 正常停止服务，复制整个 data 目录到私有备份位置。恢复时同样先停服务，再恢复整个目录。不要运行两个服务实例读写同一数据目录。用户删除会让应用无法读取相关数据，但外部备份、磁盘残留及模型供应商侧保留不由此操作抹除。

应用只监听回环地址，拒绝外部 Host/客户端和跨来源变更请求，不能直接作为公网服务。若需多人远程试点，需另做 HTTPS、部署身份、文件解析沙箱、配额与备份方案。
