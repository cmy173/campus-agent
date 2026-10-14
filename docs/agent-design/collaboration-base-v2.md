# V2 协作基础交接

日期：2026-10-14。源码基础提交：e164b52（完整SHA在交付清单）。本地交付分支：codex/v2-collaboration-base。该基础可用于并行开发，V2完整验收仍在进行。

## 领取与启动

协调者提供Git bundle和源码ZIP，两者均从已提交稳定版本生成，运行中的未提交增量不混入。Git bundle包含分支历史；ZIP用于无Git环境阅读。文件均不包含本机个人数据库、密钥、浏览器登录配置或node_modules/.venv。

```powershell
git clone --branch codex/v2-collaboration-base campus-agent-v2-base.bundle campus-agent-v2
cd campus-agent-v2
python tools/dev.py --setup
python tools/dev.py --check
python tools/dev.py --platforms --open-browser
```

需要Python3.12、Node24/npm和Microsoft Edge。最后一行只启动新应用，不启动学校专用登录连接器；需要学在浙大连接器时用README中的默认启动命令。默认主入口/chat。首次配置模型通过聊天“模型设置”，密钥本机保存；基础包不包含用户账号。

## 已验证可用的基础

- 唯一主要启动器tools/dev.py；React/FastAPI/Harness正常源码已提升主目录。
- 固定Harness/SDK0.2.0-rc.2；官方provider代码路径与受控Messages/SSE协议验证。
- 通用文件/PowerShell/Python、Skills、真实本地MCP发现/执行/断连恢复、双子任务和选择性停止。
- 对话附件上传、Markdown/PDF/XLSX真实生成、成果副本/下载及格式预览。
- 应用自有持久Edge、页面读取、下载自动交付、表单填写、一次确认/回执防重；登录Cookie持久性在受控页面验证。
- root停止等待自有Windows进程组、浏览器和在途API收尾，区分stopping/已停止。
- 请求/会话状态/事件投影/展示/主题已有分层，未知工具可展示通用结果。

当前默认70项工具（示例计算MCP后71）；数量不代表所有场景成功率。具体范围见tools-v2.md。

基础验证：API294项、前端25文件142项、TypeScript/Vite构建，完整实际API/SDK集成71次受控模型调用；证据索引见agent-v2-progress.md和runtime-lifecycle-v2.md。原证据目录var没有纳入交付，可用仓库内verify脚本重新生成。模型受控，不能据此宣称真实DeepSeek或学校账号通过。

## 明确未交付完成的部分

持久Goal产品接入、调度、跨轮子任务、校园旧能力与不可再获取数据迁移、旧源码彻底退役、UI主导航收敛/换布局例子、性能对照、真实模型三场景与ZJU账号验收、最终全新目录重建。旧源码仍在树中等待逐项迁移，新基础不依赖旧server作为主入口。CI现有工作流仍偏旧系统，需后续迁移；不能把该基础称为V2最终发布。

协调者在主开发分支继续完善Goal，并用parallel-work-v2.md的文件边界协调UI、校园迁移、调度三项。协作者从交付分支独立开发，提供补丁或分支，不需要等待完整Goal。

## 接口与修改规则

- 稳定HTTP/事件：contracts/openapi/agent-openapi.yaml；兼容新增事件由通用展示承接。
- Python领域：apps/api/app/agent_bridge/tools.py；校园连接器：connectors。
- Harness适配：apps/agent-runtime；不要重建第二Agent循环，不修改上游私有源码。
- UI：apps/web/src/features/agent；布局tokens在agent-theme.css。
- 现有修改边界与直接可用提示词见parallel-work-v2.md。

测试先设置隔离数据库/文件/凭据/Harness home；禁止在个人数据库上运行pytest。真实模型验证和开发额度分别30元/100元，仅在配置可核对的费用控制后使用。
