# Campus Agent 核心试用版首轮验收

日期：2026-10-13（Asia/Shanghai）。范围：`goal-implementation-prompt.md` 的 M0–M2 与八项首轮验收。本文记录本机合成数据与受控模型验证，不代表真实校园账户、付费模型质量、学生试点或生产部署验收。

唯一实施目录：`C:/Users/mingxiang/.codex/worktrees/campus-agent-core/campus-agent/workbench`。父工作树分支 `codex/agent-core`，基线提交 `3a853560fcec1976ece2f72e0d90fc3b9e8307f0`。本轮源码仍为未提交修改，没有推送、合并或公开部署。

## 交付与运行

默认 `/chat` 对话入口；旧页面保留在辅助工作台。已实现 16 项领域工具，严格 schema 快照见 `agent-tool-schemas.json`，使用说明见 `agent-tools-implemented.md`，接口与启动说明见 `agent-api.md`。

固定官方 `@deepseek-ai/dsh` 和 `@deepseek-ai/dsh-sdk-client` 为 `0.2.0-rc.2`，MIT；npm lock、integrity 和许可证记录见 `agent-runtime-version.json`。Python 依赖固定在 `apps/api/requirements-lock.txt`。验证环境为 Python 3.12.10 / Node 24.19.0 / npm 11.17.0。

```powershell
python tools/dev.py --setup
python tools/dev.py --check
python tools/dev.py --platforms xzzd zykt
```

打开终端显示的本机网页，配置模型服务并接受向该服务发送本次对话及相关资料的范围。日常查询不要求先进入高级同步设置。没有导入数据时返回实际空结果与覆盖缺口。

确定性集成入口：`apps/api/.venv/Scripts/python.exe tools/verify_agent_runtime.py`，只创建独立合成数据库、本机受控模型与自有服务，不使用既有校园会话或模型密钥。浏览器验收模式为 `--serve --web-port 15174 --stop-file var/<本次唯一停止标记>`；结束时写停止标记协作关闭服务。

## 合同逐项核对

| 条目 | 当前实现与直接证据 |
| --- | --- |
| M0.1 保全原工作 | `agent-preservation-audit.json`：55项中53项字节一致，无缺失；两份设计文档按后续请求更新，旧版保留于备份 ZIP；patch/ZIP/manifest 均存在 |
| M0.2 可追溯隔离基线 | `agent-baseline.json`、`agent-package-audit.json`：发布 ZIP SHA-256 与408项包内清单重新核验匹配；独立 codex/agent-core 工作树 |
| M0.3 独立合成数据 | 集成脚本及 pytest 设置独立数据库/文件/journal/secrets；没有覆盖原工作区或拼接两套真实数据 |
| M0.4 重复安装启动测试 | `agent-clean-rebuild.json`：新目录排除安装依赖/数据/缓存，setup、构建和实际启动 smoke 成功；真实 Harness 集成复现成功 |
| M0.5 官方固定运行时 | package-lock、runtime-version、实际 runtime 事件；官方 SDK/agent loop，显式 campus profile，MIT；冷恢复适配使用公开 Cordis/agents.resume API |
| M1.1 真实桥接 | 实际 FastAPI→官方 Harness→模型→实际 Python 工具链；集成文件的 tool_result 与 runtime/model 请求记录 |
| M1.2 九类只读能力 | 连接/课程/作业/资料列表检索读取/任务/计划/job 已注册；16项完整集合在 model_visible_tools 和 schema 快照中 |
| M1.3 schema/来源/错误/测试 | 所有参数拒绝额外字段；统一9字段结果信封；来源、覆盖、观测时间、版本、分页与错误；`test_agent_tools.py` 及各写工具测试 |
| M1.4 可信身份和范围 | browser_owner/runtime_authority、冻结课程/连接范围、业务库 job 归属；用户/URL/路径不能由模型参数扩权；gateway 越权及凭据边界测试 |
| M1.5 会话与材料上下文恢复 | journal 保存轮次、工具及 accepted_action，官方 JSONL 保存模型历史；实际第二轮、取消后立即续问、API重启与浏览器刷新通过；可重新打开原文快照 |
| M1.6 对话主入口与旧页 | App 路由默认 chat，原工作台仍可访问；桌面与390px窄屏实际操作，草稿和课程上下文恢复 |
| M1 通过条件与歧义 | 事实来自合成业务行/正文；空结果、NO_EVIDENCE、旧版本、未知截止、未解析正文与缓存测试；课程重名返回 needs_user、提供学期/ID选择；失败页展示持久化具体模型原因 |
| M2.1 安排依赖工具 | availability.read、tasks.build/feedback、plans.read/preview/apply/revoke、jobs.read/cancel；实际领域服务及 worker 验证 |
| M2.2 本次约束 | temporary_availability 使用分离副本；unavailable_evenings 由服务端按本周星期/时区转换；不改长期偏好，预览明示范围，过去星期拒绝 |
| M2.3 方案差异与约束 | 保存候选/当前计划/added-removed-retained/任务标题/缺口；尊重锁定块、真实容量、已提交排除与输入版本；当前计划及候选内容变化拒绝应用 |
| M2.4 具体用户接受 | 接受入口只供浏览器所有者；模型 approved=true 拒绝；action_id 绑定可信轮次；按钮或精确文字接受后执行，不另弹重复确认 |
| M2.5 长任务、重试与撤销 | 真实 job 与进度轮询；running/cancel_requested 等待检查点；回执和业务变更同事务，重放不重复写入；异常回滚、worker重启取消恢复测试 |
| M2 通过条件 | 实际 Harness 完成本周→预览→接受→生效→明天调整→撤销恢复前版；容量不足保留 unassigned；浏览器同一对话本周方案/反馈/撤销通过 |

## 八项首轮门槛

| 门槛 | 证据 |
| --- | --- |
| 1 干净安装/构建/启动 | 独立 `D:/AI/campus-agent-rebuild-c72uzzk5`，锁定依赖重新安装；TypeScript/Vite构建、API/网页/迁移 smoke、实际 Harness 集成成功；文件SHA和修复记录见 clean-rebuild |
| 2 实际运行时/插件/工具 | 固定版本、显式 profile 的18项 runtime 插件记录、16项模型可见工具、实际 tool_result；模型和业务层分别保留合成请求证据 |
| 3 同入口三场景 | `assets/agent-browser-full.json`：同一会话作业/原文/本周安排；`assets/agent-browser-m2.json`：接受/应用/撤销与草稿恢复；集成中的明天调整与前版恢复 |
| 4 成功及异常覆盖 | test_agent_tools/gateway/constraints/plan_actions/job_actions/feedback：成功、空、无依据、非法、越权、旧版本、重复键、失败回滚、取消与恢复；API整套通过 |
| 5 确定性约束和事实精度 | 真实排程引擎与临时约束测试；锁定冲突拒绝；容量不足不超排；PD3已提交排除；未知/仅日期精度保留；学校提交状态与本地完成分开 |
| 6 实际浏览器桌面/窄屏 | browser-m1/m2/full JSON及截图；原文、日历、草稿、当前课程、刷新、取消、反馈和文字撤销；390px无横向溢出，控制台错误为空 |
| 7 原基线与新增回归 | API全量272通过（36.73秒）；前端全量19文件125通过（28.90秒）；干净目录构建通过；未删除旧断言，类型/环境问题已修复 |
| 8 文档和验收一致 | README、agent-api、tools-implemented、tool-schemas、runtime-version、clean-rebuild、preservation/package/dependency审计与本文；进度历史另存，当前摘要统一 |

主要运行证据：实施目录 `var/agent-integration-qbk_diui/evidence.json`，41次模型调用；干净目录 `var/agent-integration-icq_4a_j/evidence.json`，41次。每次运行生成单独目录，最终副本的再验证结果记入 clean-rebuild，不通过复制旧证据替代重建。

## 限制与后续

- 真实校园账户、真实付费模型措辞/推理质量、费用及普通学生试点未验证。受控模型只选择测试工具并基于其结果回答，不代表开放模型能稳定理解任意自然语言。
- 核心服务是单人本机工作区，单 API/单 worker；cookie 是会话所有权边界。原业务接口没有被改造成托管多租户鉴权系统，不公开部署。
- 当前模型传输为 Chat Completions；固定 provider 快照变更后需新建会话。材料检索为子串检索，解析器不保证扫描资料全文；覆盖和缓存缺口保留。
- 模糊“晚上”按18:00至次日00:00排除并在预览明示，可补精确时间。当天新方案留10分钟检查时间，执行前仍检查开始时间；锁定冲突需明确决定解锁，未自动解锁。
- 精确文字“按这个来”“撤销刚才安排”使用当前会话对应动作；其他表达由模型工具解释。可用方案按钮直接绑定具体对象；过期/已变化方案必须重新预览。
- `npm audit` 报13个依赖条目（6 high/7 moderate），根因是 MCP TypeScript OAuth 客户端和 fflate ZIP64 解析两类上游公告。固定包依赖含这些库；campus profile 未挂载 MCP/Office插件，模型工具集合不暴露其能力。漏洞未修补，不据此宣称依赖安全；完整报告见 agent-dependency-audit.json。后续升级固定上游版本并做回归，未使用 audit fix --force 改动锁定基线。
- 后续 M3–M4 的更多校园信息整合和复习产物不属于本次首轮；本轮不新增学校平台写入、自动报名、音视频转写、公共部署或通用 Shell。

所有变更留在实施目录，原有成果及备份继续保留。是否提交、推送或进入真实用户试点由后续任务决定。
