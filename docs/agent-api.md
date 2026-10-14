# 本地 Agent API 与验证

当前已实现对话网页与 M0–M2 核心链路；验收证据及剩余审计见 agent-acceptance.md。业务数据库仍是单人工作区，不能以浏览器会话隔离代替多租户数据鉴权。

## 安装与运行

根目录执行 `python tools/dev.py --setup`，安装锁定的 Python、网页与 Agent npm 依赖。Node 要求 24 或以上。随后 `python tools/dev.py` 启动本机 API、网页和既有后台 worker；自定义端口会同步传给 Agent bridge 与网页来源允许列表。原有业务工作台仍可使用。

Agent journal 默认位于 `var/agent/journal.sqlite`，官方 Harness 日志按会话保存在 `var/agent/harness/`。可用 `CAMPUS_AGENT_HOME` 指定独立测试目录。journal 独立存储；启动器的业务库迁移新增计划动作、任务生成和反馈回执表。开发验证只使用隔离合成数据库，未迁移真实学生数据。仅使用一个 API 服务进程，不支持多个服务同时操作同一 journal。

## 对话接口

接口前缀 `/api/v1/agent`，请求 JSON 严格拒绝额外字段。浏览器使用 HttpOnly / SameSite=Strict cookie 保存本机对话所有权，不把该 cookie 交给模型。对话 API 限制本机 Host 与明确配置的网页 Origin；开发端口由启动器设置。

| 方法与路径 | 用法 |
| --- | --- |
| POST /conversations | `provider_id`、`send_to_provider:true`、可选 `course_id`；必须由用户确认向所选模型发送消息和相关工具返回的本地资料 |
| GET /conversations | 列出本浏览器最近的 100 个会话 |
| GET /conversations/{id} | 恢复会话、当前课程和历史轮次 |
| POST /conversations/{id}/turns | `text`、可选 `course_id`、`clear_course_context`、已接受的 `action_id`；必需 Idempotency-Key；返回 202 和实际轮次 |
| GET /conversations/{id}/actions | 读取最近100个本会话具体动作与回执 |
| POST /conversations/{id}/actions/{action_id}/accept | `operation:apply/revoke`，必需 Idempotency-Key；仅浏览器所有者可以接受具体动作 |
| GET /turns/{id}?after=0 | 返回轮次状态和最多 100 项事件，events.next_cursor 用于后续读取 |
| POST /turns/{id}/cancel | 停止本轮 SDK；终态不会被晚到的模型结果覆盖；业务后台 job 必须另行取消 |

消息上限 8000 字符；一轮最多 12 次模型请求、32 次工具执行、180 秒。相同请求键与相同消息返回原轮次；相同键不同消息返回 IDEMPOTENCY_CONFLICT。每个会话只运行一轮，其他新消息返回 TURN_IN_PROGRESS。

模型配置沿用原 `/ai/providers`。会话绑定经用户确认的地址、协议、模型、超时及更新时间；配置变更或删除后返回 PROVIDER_CHANGED，需要新建会话并重新确认范围。密钥仍只由 Python 从原密钥存储按需读取，Node 环境只包含 OS 必需项及本轮 loopback 能力授权，不继承校园或模型凭据。

## 内部调用

`POST /internal/schemas`、`/internal/tool`、`/internal/model` 仅接受服务端创建的本轮能力令牌。数据库只保存其 hash，结束、取消和重启即失效。它不能由模型参数选择会话身份；其他会话 ID 被拒绝。

模型请求中的工具清单和 HTTP attribution headers 不作为授权或运输配置。服务端重新构建工具清单，仅允许校园工具；返回的调用也必须属于该清单。Python 适配器执行严格参数与对象范围校验，普通领域错误作为工具错误信封返回，便于模型说明缺口。

官方 `0.2.0-rc.2` SDK 冷启动只调用 agents.create，不能直接恢复相同 ID。`campus-sdk.mjs` 使用公开 Cordis context.extend，在 SDK 自己的创建入口选择公开 agents.resume；JSON-RPC、Agent loop、会话事件和关闭机制均为固定版上游实现，没有复制其循环或修改私有方法。

## 验证入口与当前证据

实际集成：`apps/api/.venv/Scripts/python.exe tools/verify_agent_runtime.py`。脚本创建独立合成数据库与受控本机模型服务，启动真实 FastAPI 与官方 Node Harness；领域事实从实际工具返回生成，未调用校园账户或付费模型。验证查询/检索/原文读取、第二轮历史、重复发送、取消后继续、API 重启恢复。输出 `var/agent-integration-*/evidence.json` 与合成 model-receipts.json。

最近通过证据：`var/agent-integration-qbk_diui/evidence.json`，41次模型调用、16项领域工具，包含同一会话本周/明天预览、用户接受后应用、撤销恢复与回放、真实worker任务生成/取消、任务版本读取后反馈。干净目录复现证据为 `D:/AI/campus-agent-rebuild-c72uzzk5/var/agent-integration-icq_4a_j/evidence.json`。实际校园 profile 与工具集合在 runtime 事件和 model_visible_tools 字段记录；无 Shell/MCP/任意文件工具。

桌面与窄屏证据：`docs/assets/agent-browser-full.json` 及相邻截图；原文、日历、草稿、刷新、后台取消和反馈均有实际浏览器记录。

API 回归：在 `apps/api` 下使用独立 STUDY_DATABASE_URL / CAMPUS_AGENT_HOME，执行 `.venv/Scripts/python.exe -m pytest tests -q --tb=short`。新增 gateway 测试覆盖浏览器隔离、跨站拒绝、严格输入、请求/工具幂等、scope、取消后授权失效、持久恢复、provider 快照、消息投影、密钥边界、非法模型回复与调用预算。

未验证：真实校园账户、付费模型回答质量、普通学生试点及公开多用户部署。依赖审计与运行限制见 agent-acceptance.md；本轮不宣称生产就绪。

干净目录重建步骤及文件SHA见 agent-clean-rebuild.json。复制源码时排除 .venv/node_modules/var/dist/.git 和缓存，再依次执行 setup、前端 npm run build、tools/dev.py --platforms --api-port 18001 --web-port 15175 --smoke-test 及 tools/verify_agent_runtime.py；端口冲突时选空闲端口。
