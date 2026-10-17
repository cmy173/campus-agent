# 校园只读任务闭环与认证恢复：实施与自测记录

状态：**完成实施与自测，待 Codex 独立验收**。本文件不判定"通过最终验收"，也不替代独立验收。
对应任务书 `docs/agent-design/harness-campus-readonly-task.md`，验收合同
`docs/audit/harness-campus-readonly-acceptance.md`。

- 基线 HEAD：`650a9348e859ee24544c85fce1a3f397ae3e2345`（分支 `codex/personal-assistant-v2`，已有 PR #6）。
  工作区开始时除既有未跟踪审计材料外无改动；本轮未 commit、未 push、未合并 PR。
- 原始证据目录：`var/harness-campus-readonly-2b0480ed/`（`var/` 已 gitignore，不进入公共 PR）。
- 本机网关测试 31 项、API 700/5、root+connectors 606+87 subtests、前端 193、Node 85 等历史数字**未被照抄**为本轮结果；
  本轮实测见 §5。

## 1. M0—M3 状态

| 里程碑 | 状态 | 说明 |
| --- | --- | --- |
| M0 冻结行为基线 | **完成** | HEAD/哈希/依赖/工具 schema/暴露分组/既有失败与 C01–C16 覆盖图，见 `m0/` |
| M1 缓存查询与显式同步边界 | **完成** | 唯一已证实阻塞缺陷（缓存读隐式重登+远端 sync）已修，8 个旧测试 + 2 处描述 + 3 份文档同步修正 |
| M2 三条业务链 | 见 §4 | 离线确定性链式证据 + 真实模型四项会话（M3）分开报告 |
| M3 网络与真实验证 | 见 §5、§6 | 显式 `--public-live`/`--model-live`/`--campus-live`，默认离线；真人校园按外部条件如实标注 |

### 修改文件（`git diff --stat` 的 10 个已跟踪文件，另有 2 个未跟踪审计文档/新增文件）

下述 `git diff --stat` 只覆盖**已跟踪**文件；`docs/audit/transition-tool-audit.md` 本轮被修改但本来就是未跟踪文件，
故不在 diff 统计里（其改动为 27→32、campus.query 能力描述修正）。

```
apps/api/app/campus_workspace.py                 | 51 +++++++---   产品：读/同步边界拆分
apps/api/app/agent_bridge/runtime.py             | 18 +++--        产品：可信恢复探针重映射
apps/api/app/agent_bridge/tools.py               |  4 +-           产品：两条工具描述与语义一致
apps/api/app/campus_schedule_constraints.py      |  5 +-           产品：docstring 更新（行为未变）
apps/api/tests/test_campus_browser_bridge.py     | 62 ++++++---     测试：2 例改写（语义迁移）
apps/api/tests/test_campus_credentials.py        | 33 +++++--      测试：2 例改写（语义迁移）
apps/api/tests/test_campus_persistent_grants.py  | 72 +++++++---    测试：5 例改写（语义迁移）
apps/api/tests/test_agent_auth_resume.py         |  6 +-           测试：探针工具名断言
docs/agent-design/campus-migration-v2.md         |  4 +-           文档：query/sync 边界
docs/agent-design/tools-v2.md                    |  2 +-           文档：工具清单计数修正
docs/audit/transition-tool-audit.md              | 6 +++---        文档：27→32、campus.query 能力修正
?? apps/api/tests/test_campus_readonly_boundary.py  新增 8 例 C01/C02 边界回归
?? tools/verify_campus_chains.py                    新增 M2 链式验证入口
?? tools/verify_campus_readonly_live.py             新增 M3 真实模型四项会话入口
```

## 2. M1：缓存查询与显式同步边界（唯一已证实阻塞缺陷）

### 2.1 修改前复现（保留首次失败）

基线工作树（HEAD，未打补丁）用独立 worktree 运行同一探针，外部边界全部合成替换，学校网络请求为零：

| 运行 | `query(refresh=False)` 的计数 | 返回 | 结论 |
| --- | --- | --- | --- |
| **基线 HEAD**（`baseline-pre-fix/probe-baseline.json`） | cache_read=1, **auto_relogin=1, browser_provider=1, remote_sync=1** | `error=null`、records 来自 sync、`freshness=fresh` | 一次"缓存读"实际提交了密码、读了浏览器凭据并发起远端同步，还把远端新结果当成普通读成功返回 |
| **修复后工作树**（`baseline-pre-fix/probe-current.json`） | cache_read=1, auto_relogin=0, browser_provider=0, remote_sync=0 | 保留缓存时间戳、结构化 `AUTH_REQUIRED`、`freshness` 不谎报 | 缓存读不再触达学校 |

同一探针作为回归测试运行（`apps/api/tests/test_campus_readonly_boundary.py`，12 例）：基线 HEAD
`6 failed, 6 passed`（`baseline-pre-fix-run.txt`），修复后 `12 passed`。

两个触发条件（都真实存在，不是推测）：
1. 缓存里已持久化 AUTH 错误行（`CampusService._sync_locked` 在失败时也会 `repository.write`）→ 纯缓存读永远读到旧错误；
2. 本进程没有活跃会话（如 API 重启后），而缓存数据与 `fetched_at` 完好 → 由本地 `connector_session` 注入 AUTH_REQUIRED。

### 2.2 根因层次

- 层次一（真正根因）：`CampusWorkspace.query(refresh=False)` 在返回缓存后继续做授权判断与恢复；`CampusService.query` 本身确实是纯缓存读。
- 层次二：恢复能力**只**挂在隐式路径上。若只把隐式刷新删掉，"缓存中已持久化的 AUTH 错误"将无法清除 → 用户显式 sync 也永远恢复不了（任务书明确禁止这一结果）。
- 层次三：会话/权限时序——一次普通只读调用会提交密码、读取浏览器凭据，与"缓存查询不暗中登录"契约冲突，且调用成本不可预测。
- 层次四：契约漂移——8 个既有测试、2 条工具描述、3 份审计文档都把隐式恢复当预期，其中
  `docs/audit/tool-system.md:58` 早已写 `campus.query … 不联网`，即**文档是对的、实现是错的**。

### 2.3 最小修复

1. `apps/api/app/campus_workspace.py`
   - `query(refresh=False)` → `_cache_read_result()`：只做缓存读 + 本地内存会话状态判断（不打开连接、不读浏览器凭据），
     返回缓存 records/`fetched_at`/`freshness` 与结构化连接错误；**不调用** `auto_relogin_locked`、不读浏览器 Cookie、不 sync。
   - `query(refresh=True)` → `_resync_result()`：显式同步；仅在返回 AUTH_REQUIRED/AUTH_EXPIRED/SESSION_EXPIRED 时
     执行**最多一次**已授权恢复（存证件自动重登，或复用已授权浏览器会话），随后**最多一次**重试。
     冷却（`CREDENTIAL_COOLDOWN_SECONDS=300`、浏览器 30 s）、身份校验、`BEGIN IMMEDIATE` 授权复核、撤销语义全部保留。
2. `apps/api/app/agent_bridge/runtime.py`
   - 新增 `RECOVERY_REFRESH_TOOL = {'campus.query': 'campus.sync'}`：可信恢复探针把"待恢复的缓存读"以**同 capability/query/platform**
     的显式只读刷新执行一次；仍要求目标工具 `effects == read`，写入类一律拒绝（`RECOVERY_READ_ONLY`）。
   - 这使"缓存读不再联网"与"用户显式 sync / 认证后恢复仍能成功"同时成立。
3. `apps/api/app/agent_bridge/tools.py`：`campus.query` 描述改为"纯缓存读取…不因读取而登录"，`campus.sync` 明确
   "唯一会访问学校远端的校园读取入口 + 最多一次自动重登（验证码/MFA/拒绝/限流后冷却，不切通道重投密码）"。

调用方覆盖（已核实）：agent 工具 `campus.query`/`campus.sync`（描述已同步）；HTTP `POST /campus/query`、`POST /campus/sync`；
可信恢复探针（已重映射）；`campus_schedule_constraints.read_campus_evidence()` 直接调 `service.query`，本就离线、行为不变。
前端**没有**任何 `/campus/query` 或 `/campus/sync` 调用（只调用 status/connect-browser/credentials/test-login），故无 UI 语义回归。

### 2.4 修改后同一案例

- 独立探针：`cache_read=1`，auth/browser/sync 全为 0，结构化 `AUTH_REQUIRED` 保留（`probe-current.json`）。
- 显式入口仍能恢复：`test_explicit_sync_is_the_only_entry_that_recovers_once`（1 次自动重登尝试 + 1 次浏览器再采纳 + 1 次重试）、
  `test_captcha_or_rejection_cools_down_without_resubmitting_the_password`（冷却内不再提交）、
  `test_auto_relogin_disabled_never_reaches_the_password_submission`（设置关闭时 `AUTO_RELOGIN_DISABLED`，`prepare/login` 零调用）。
- 恢复链未退化：`test_agent_auth_resume.py`（含探针重映射断言）与
  `test_restart_waiting_checkpoint_probes_then_resumes_without_user_resend` 全通过；
  `test_revocation_during_provider_read_never_resurrects_grant` 仍在**显式**路径上真实触发撤销竞争并拒绝复活授权。
- 24 处改动全部落在 8 个既有测试的入口点（`refresh=False` → `refresh=True`）与新增断言，**未删除任何**安全/隔离/冷却/撤销断言。

### 2.5 回滚

`git checkout HEAD -- apps/api/app/campus_workspace.py apps/api/app/agent_bridge/runtime.py apps/api/app/agent_bridge/tools.py apps/api/app/campus_schedule_constraints.py`
并删除新增测试文件即可回到基线行为；本轮不涉及 alembic 迁移（head 未变），也不改数据/缓存结构。
基线源码哈希见 `m0/baseline.json: source_hashes_sha256_12`（本轮结束时重新冻结；campus_workspace.py、runtime.py、tools.py 等）。

## 3. 工具 schema 与暴露分组（M0）

- 领域工具 **32** 项（写效果 12 项），其中 `campus.*` 8 项；模型名 `campus_` + 点转下划线（`campus.status` → `campus_campus_status`）。
- 常驻可见：全部 32 项领域工具 + 20 项基础工具（含 `publish_file`/`load_tools`/5×`schedule_*`）；延迟分组：browser 21 项白名单、
  jobs 3 项、planning 1 项；MCP 资源 3 项被能力策略拒绝；`bash`/`pwsh`/`workflow`/`run_code`/`ralph` 在产品 profile 中禁用。
- 权威清单：`var/harness-campus-readonly-2b0480ed/m0/baseline.json`（由真实 `tools.schemas()` 导出）。
- 过时快照修正：`docs/audit/transition-tool-schemas.json` 由项目自身工具重新生成为 32（旧快照留档
  `m0/transition-tool-schemas.pre-round.json`）；`docs/audit/transition-tool-audit.md` 的 27→32 与 campus.query 能力描述已修正；
  `docs/agent-design/tools-v2.md` 的注册数已按实测重写。

### 至少两个内部调用示例（服务端构造上下文，无密码/Cookie/ticket/签名 URL）

1. 只读缓存查（模型侧 `campus_campus_query`）：
   `POST /api/v1/agent/internal/tool` → `{"name":"campus.query","arguments":{"capability":"notices","query":{},"platform":"zdbk_public"}}`
   服务端 `ToolContext('local', <conversation_id>, …)`；返回 `status/freshness/records/evidence[]`，无凭据字段。
2. 显式同步（模型侧 `campus_campus_sync`）：
   `POST /api/v1/agent/internal/tool` → `{"name":"campus.sync","arguments":{"capability":"college_notices","query":{}}}`；
   服务端选定平台与 scope，失败保留旧缓存并返回结构化错误，不返回任何会话材料。
3. 可信恢复探针（不由模型触发）：
   内部 `Runtime.probe_authenticated_read(checkpoint)` → 以 `request_key='auth-probe:<id>'` 与检查点窄 scope 执行
   `campus.sync`（同 capability/query/platform），只读、无浏览器、无模型调用。

## 4. M2：三条业务链

入口：`tools/verify_campus_chains.py`（默认全离线）。命令与原始输出见 §7，证据
`var/harness-campus-readonly-2b0480ed/m2/chains-final.json`（实施方复跑）与 `chains-local.json`（作者复跑）。

三条链均通过**真实产品表面**（真实 `CampusService`/适配器/缓存库/仓储/任务表/worker/解析/计划引擎），
只在**学校传输边界**用纯函数桩替换，模型完全不参与（"禁止脚本替模型预先规划工具顺序"因此不适用：
这里的工具顺序由验证脚本按契约固定，用于证明链路本身可用；自主选工具的断言在 §5 的真实模型会话里做）。

| 链 | 验收用例 | 工具链（真实调用） | 检查 | 断言要点 |
| --- | --- | --- | --- | --- |
| A 通知 | C11 | `campus.sync` → `campus.notice.read` → `campus.sync`(第二来源) | **12/12** | 列表页只是受限摘要（`partial`）；正文确实来自被取回的详情页；摘要≠正文；附件被列为未读且其内容不在正文；仅竺可桢学院来源时**明确不声称全校覆盖**并输出来源受限警告 |
| B 课程资料/作业 | C12 | `connections.courses` → `connections.courses.import` ×2 → `connections.sync` → 真实 worker `handle_sync`/`handle_parse` → `jobs.read` → `materials.list` → `materials.read` → `assignments.list` | **17/17** | 第 1 页导入**不会**静默吞掉第 2 页（游标被真实使用）；sync 先返回持久 job 且状态 `queued`（"只受理不算完成"）；终态与 `result_refs` 由 worker 自己写入；目录元数据不含正文；正文来自平台附件经真实 `learning.parse`(pypdf) 解析；**未知截止保持 `due_at=null`/`due_precision=unknown`**，已知截止保留分钟精度 |
| C 课表/学习安排 | C13 | `campus.academics` → `tasks.list` → `plans.preview` → `plans.read` | **10/10** | 缓存课表行被正确落到 `2026-10-16 08:00–09:35`，校历 `sha256` 与 `period_times` 保留；预览时间块避开真实上课块；预览**不写正式计划**（只产生 `candidate`，`plans.read` 仍 `plan=null`，动作 `pending`）；无课表/校历证据时输出 `CAMPUS_TIMETABLE_UNCERTAIN {unreliable_timetable: 2}` 而**不编造空闲时段** |

合计 **39 项检查 / 0 失败**；两次运行（`chains-final.json`、`chains-local.json`）归一化后判定与答案字节一致；
离线运行自测的非回环连接计数为 **0**（`--public-live` 时才为 1，证明计数不是空转）。

**未在该入口覆盖**（如实声明）：真实学校 HTTP/DNS/TLS、CAS/验证码/浏览器 Cookie 采纳、WebVPN、浏览器会话服务、
模型与复习 provider。这些由 §5 的真实网络与真实模型证据分别覆盖。

三项非阻塞发现（未改产品代码，供验收判断）：
1. `apps/api/app/agent_bridge/tools.py:318` 把 `campus.notice.read` 描述为"已展示学院通知正文"，
   而 `tools.py:737` 实际按 id 读取任意已缓存通知（链 A 用它读了校级 `zdbk_public` 列表来源并成功）。
   描述比实现更窄，属文档-实现不一致，本轮不改以免在证据采集后再动产品源码。
2. 该验证在 SQLite 下观察到某 sync job 的 `jobs.started_at` 为 NULL，而 `apps/api/app/worker.py:177`
   的 `_claim_next` 明确用 Core UPDATE 写 `started_at=utc_now()`。最可能是验证脚本/ORM 身份映射导致的观察差异
   （`db.get` 未过期缓存对象），**未确认为产品缺陷**；`finished_at`/`result_refs`/`phase`/终态均正确，故检查以它们为准。
   既有 `apps/api/tests/test_worker_api.py:134` 对共享文件库变体也放宽了 `started_at` 断言，说明这不是新问题。
3. 同一时间戳下 build_tasks/parse 的取用顺序逐次不同，但最终资源/分块/作业/计划状态与全部判定一致。


## 5. M3：网络与真实验证

显式开关（默认全部不访问真实服务）：`--public-live`、`--campus-live`（`tools/verify_campus_chains.py`）、
`--model-live`（`tools/verify_campus_readonly_live.py`，无该开关时在读取 provider/凭据之前以 **exit 2** 拒绝，
并打印"未读取 provider 配置、未读取密钥文件、未调用模型"）。三种通路分别判断，某一路可用不作为另一路可用的证据。

### 5.1 公开联网（`--public-live`，无校园凭据）

- `tools/verify_campus_chains.py --chains a --public-live --timeout 15`：exit 0；真实 `https://example.org` HTTP 200、
  TLS 校验通过、记录正文 sha256；`non_loopback_connections=1`（离线运行时为 0）。
- 另用项目既有匿名探针 `tools/audit_public_network.py` 对 4 个固定 URL 各跑 system/direct 两次：exit 0、`credentials=false`、
  `tls_verified=true`、无代理环境变量。结果：`example.org` 200、`www.zju.edu.cn` **200 / 19092 B**、
  `zdbk.zju.edu.cn` 登录页 **200 / 12417 B**、`webvpn.zju.edu.cn` 302→`/login` 200。
  证据：`m3/public-network-python.txt`、`m3/transition-network-python.json`。
- 该结果同时是一个安全前提：**zdbk 直连可达**，因此 `campus_network.channel_for` 在 auto 模式下选择 `direct`
  （只有直连不可达时才会走门户），不会因一次真实读取而登录 WebVPN。

### 5.2 应用拥有的浏览器

本轮**未**新增浏览器真人验证：需要显式授权 + 有头登录，且 `channel_for` 的门户路径会与用户现有 WebVPN/浏览器
会话互踢（任务书禁止）。后端 HTTP 可达**不能**作为浏览器通路可用的证据，反之亦然——两条通路分开报告。

### 5.3 Harness 公开搜索/网页

`web_search`/`web_fetch` 是 Harness 上游工具，与校园 transport 无关；本轮未改动其配置，也未用"HTTP 能请求"推断
"具备搜索"。真实模型会话（§5.4）中若模型使用搜索，会单独标注为合成学校数据之外的公开来源。

### 5.4 真人校园（`--campus-live`，最小只读）

一次性真实只读探针（`m3/probe_real_campus.py` → `m3/campus-live.json`），取得：

| 项 | 实测 |
| --- | --- |
| 真实凭据库 | 使用 `var/secrets`（只读；`vault_dir_is_repo_secrets=true`）；缓存/工作区/领域库全部隔离 |
| WebVPN 通道 | `CAMPUS_WEBVPN_MODE=direct` 强制直连；运行后门户 `authenticated=false`、`login_attempts=0`（**未挤掉任何现有校园会话**） |
| 认证 | 自动重登 **1 次**（`AUTO_LOGIN_OK`，`needs_user=false`），有界、无冷却；**未**打印/落盘任何密码、Cookie、ticket |
| `grades` | 真实只读成功：18 条记录、`fresh=true`、`complete`（只记条数，不落个人记录） |
| `profile`（ETA） | 真实只读成功：1 条记录、`fresh=true`、`complete` |
| `courses`（未给学期） | 结构化 `UNSUPPORTED`（`connectors/campus/connectors/zdbk.py:16-17` 设计要求显式同步课程必须给出单个学期），符合"不编造聚合" |
| 未验证 | 验证码/MFA 场景、教务课表同步（需具体学期）、学院通知来源匹配（依赖账号学院字段）、智云课堂账号权限 |

证据边界：这是一次真实校园只读查询，**不是**校园功能全景验证；个人记录未进入交付物，凭据值未进入日志或证据文件。

### 5.5 真实模型会话（两轮：首轮失败原样保留 + 判分修正后同任务对照）

**第一轮（首轮失败，原样保留为 `m3/model-live-round1-as-run.json`）**

命令：`apps/api/.venv/Scripts/python.exe -X utf8 tools/verify_campus_readonly_live.py --model-live --out var/harness-campus-readonly-2b0480ed/m3/model-live.json --label campus-readonly-m1-revision`
（真实 API + 真实安装 Harness + 真实已保存 provider `prv_c68d2878` deepseek-flash；学校数据固定合成；每条会话独立 conversation + 独立隔离 home。）

| 任务 | 回合 | 模型请求 / 工具 / 秒（上限 12/40/150） | 超限 | 当时判定 | 当时失败判据 |
| --- | --- | --- | --- | --- | --- |
| T1 通知依据回答 | 1 | 4 / 7 / 22.2 | 否 | 未达成 | `notice_full_text_read` |
| T2 课程资料与截止 | 1 | 8 / 11 / 29.4 | 否 | **达成** | — |
| T3 课表学习安排 | 1 | 3 / 6 / 40.0 | 否 | 未达成 | `preview_created`、`preview_avoids_class_blocks`、`no_formal_plan_written`、`timetable_uncertainty_honest` |
| T4 登录失效后只读继续 | 2 | 11 / 12 / 40.5 | 否 | 未达成 | `trusted_automatic_resume_completed`、`resumed_no_repeat_of_completed_action` |

首轮汇总：4 条会话、26 次模型请求、36 次工具调用、合计 560,633 tokens；**金额 unknown**（无账单依据，记 token 与调用数但不编造费用）；
人工交互仅 T4 计 2 次、等待 0.172 s；四条均**未**触及预算上限；模型可见工具 52 项（T4 第二轮 load_tools 后 73 项）。

该轮随后被发现有两处**判定层**缺陷（不是产品行为）：判分在 `plans.preview` 上读了错误的 envelope 层级
（真实结构是 `{"action": {...}}`，导致从空证据计算），且未记录通知正文摘要。修正后的 judge 增加了证据投影自测用例
（`--self-test` **34/34**，含 offline guard、`var/personal.db`/`var/secrets` 未被触碰的 size+mtime 断言、投影用例）。

**同 `--out` 重跑不会重判**：实测 4 条全部 `SKIP`、0 次模型调用；文件字节被重写但逐条判定不变（已比对保留副本与当前文件）。
交付 CLI 没有 rejudge 开关，故修正后的判定必须换新 `--out` 重跑取得——本轮做了 T1/T3 对照重跑。

**第二轮（判分修正后，同任务对照）**

命令：`… --model-live --tasks T1,T3 --out var/harness-campus-readonly-2b0480ed/m3/model-live-round2-t1t3.json --label judge-projection-fix` → exit 1

| 任务 | 回合 | 模型 / 工具 / 秒 | 超限 | 判定 | 失败判据 |
| --- | --- | --- | --- | --- | --- |
| T1 通知依据回答 | 1 | 5 / 9 / 25.4 | 否 | 未达成 | `no_whole_school_claim` |
| T3 课表学习安排 | 1 | 4 / 6 / 32.1 | 否 | **达成** | — |

第二轮汇总：2 条会话、9 次模型请求、15 次工具调用、合计 197,901 tokens；金额 unknown；无人工交互；均未超预算。

- **T3 判定被纠正为达成**：新判分下 `preview_created`、`preview_avoids_class_blocks`、`timetable_uncertainty_honest`、
  `no_formal_plan_written` 全部通过 → 首轮那 4 项确为**假阴性**。
- **T1 仍被判未达成，但唯一失败项与证据矛盾**：`no_whole_school_claim` 命中的是**否定句**——
  "本机缓存里**没有**能读到正文的「全校性本科生选课确认」通知"；全篇 4 次出现"全校"均为否定或不可外推表述
  （"不涉及全校政策调整…不能当成学校通知""本通知不覆盖""**不能外推为全校规定**"）。其余 7 项判据（含
  `notice_full_text_read`、`answer_title_and_date`、`answer_cites_body`、`answer_scope_limited`）全部通过；工具轨迹显示模型
  第一次把 `record_id` 编造成 32 位十六进制串并被正确拒绝，**第二次用正确 `ckc:3001200001` 成功读到正文**。
  → 我**不推翻**工具判定、也不声称 T1 通过：把"判定失败"与"证据显示为否定句假阳性"并列，交独立验收方裁定。
- **T4 无法在纯合成学校数据下通过（结构性缺口，非产品缺陷）**：`probe_authenticated_read` 用真实 campus service 重放
  `campus.sync`，需要活跃的已授权会话，而合成数据没有注入会话的接缝（无环境变量；`bind_browser_cookie_provider` 无 HTTP 路由；
  `POST /campus/connect-browser` 需要真实已登录浏览器）。驱动脚本如实记录：可信入口
  `POST /api/v1/connections/conn_syn_xzzd/check` → 探针返回 AUTH_REQUIRED → 检查点保留在 `waiting_auth` 并进入冷却、
  **不空转不谎报**，随后手工续轮被单独计量；判据（正确地）拒绝用手工续轮冒充可信自动恢复。教务链恢复的正面证据见
  §5.4 的真实 `AUTO_LOGIN_OK` 与 `test_agent_auth_resume.py`（21 例）。

净口径：**T2 两轮均通过**、**T3 第二轮纠正为通过**；T1 判定未通过但唯一失败项疑为判分假阳性；T4 结构性未验证。
两轮合计 6 条会话、35 次模型请求、51 次工具调用；**不声称成功率**——小样本、无采样/温度/seed 控制、provider 侧路由不可控、金额 unknown。

**留给验收方的产品契约问题**：`campus.query(college_notices)` 会返回**校验过的通知完整正文**，因此"读到全文"不一定需要
`campus.notice.read`；而 C11 的意图是"搜索摘要不等于读过全文"。若合同要求"列表只给摘要、正文只能经 `campus.notice.read`"，
则当前两条路径都返回正文属产品缺口。本轮**未改**产品行为，只如实报告。


## 6. 平台能力矩阵

| 能力/平台 | 实现 | 合成验证 | 历史真人证据 | 本轮真人验证 |
| --- | --- | --- | --- | --- |
| 教务（zdbk，CAS 登录，courses/grades） | 已实现 | 链 A/B/C 与回归；M1 边界测试 | 历史：教务协议真实观测 | **本轮真人部分验证**：自动重登 1 次成功；`grades` 真实只读 18 条 `fresh/complete`。`courses` 显式同步需具体学期，本轮未跑 |
| ETA（eta，profile） | 已实现 | 同上 | 历史：ETA 协议真实观测 | **本轮真人验证**：`profile` 真实只读 1 条 `fresh/complete` |
| 校级公开通知（zdbk_public，免凭据） | 已实现 | 链 A 覆盖（合成学校页） | 历史：真实免登录通知采集 | 本轮证实直连可达（登录页 200 / 12417 B）；**真实通知正文采集本轮未跑**（链 A 用合成页） |
| 学院通知（ckc_public，按账号学院自动匹配） | 已实现，免凭据 | 链 A + 既有合成测试 | 历史：竺可桢学院来源 | **待验证**（依赖账号学院字段；本轮未跑真实匹配） |
| 学在浙大 xzzd（课程/资料/作业/回放） | 已实现（session_runner 受控浏览器取会话） | 链 B + 既有 worker 端到端 | 历史：2026-10-10 下载 3/3、2.4–3.7 MB | **外部条件阻塞**：会话获取必须一次可见浏览器人工登录；后端 HTTP 可达不等于该平台已授权 |
| 智云课堂 zykt | 已实现但账号权限未确认（`course_list` 支持、`assignments` unsupported、`resource` 未实现） | 部分合成 | 无本轮证据 | **外部条件阻塞**（同上 + 账号权限未确认） |
| WebVPN 独立通道 | 已实现（独立凭据槽、独立冷却、顶号提示） | 既有 `test_webvpn_channel` 21 例 | 历史观测映射 2026-10-12 | **本轮刻意未触发**：真实探针强制 `CAMPUS_WEBVPN_MODE=direct`，门户 `login_attempts=0`，以免顶掉用户现有会话；验证码/二次认证仍需人工一次 |
| 应用浏览器（campus_browser 白名单 21 项） | 已实现，需显式授权 | 既有 8+4 例 | 历史 | **本轮未验证**（需显式授权 + 有头登录），与后台 HTTP 分开计 |
| 真实模型（saved provider `prv_c68d2878` deepseek-flash） | 已接入 | 判分自测 34/34；两轮真实会话（§5.5） | 上轮 9/9、独立抽测 3/3（**任务不同，不替代本轮**） | 第二轮对照：T2、T3 通过；T1 判定未通过但唯一失败项疑为判分假阳性；T4 结构性未验证 |

同步修正的文档：`docs/audit/transition-tool-audit.md`、`docs/agent-design/tools-v2.md`、
`docs/agent-design/campus-migration-v2.md`，以及重新生成的 `docs/audit/transition-tool-schemas.json`。

## 7. 命令、exit code、耗时、通过/失败/skip、清理与未完成条件

全部在 `D:\AI\campus-agent` 下运行，Python 为 `apps/api/.venv/Scripts/python.exe` 3.12.10，HEAD `650a9348…`（本轮改动未提交）。
原始输出在 `var/harness-campus-readonly-2b0480ed/m0/*.txt`、`m2/*.log`。

| # | 命令 | exit | 结果 / 耗时 |
| --- | --- | --- | --- |
| 1 | `tools/audit_regression.py apps/api/tests -q -p no:cacheprovider`（M1 前基线） | 0 | **700 passed, 5 skipped**，274.70 s |
| 2 | 同上（本轮改动后，最终） | 0 | **713 passed, 5 skipped**，334.05 s（+13 = 新增边界回归 13 例；5 个 skip 是既有的 Windows junction/reparse 本机限制，非本轮引入） |
| 3 | `tools/audit_regression.py tests connectors/tests -q -p no:cacheprovider` | 0 | **594 passed + 87 subtests passed**，44.03 s |
| 4 | `tools/audit_regression.py tools/test_launcher.py -q -p no:cacheprovider` | 0 | **12 passed**，0.16 s |
| 5 | `tools/export_agent_contract.py --check` | 0 | `Contract is current: 51 Agent paths and 54 schemas` |
| 6 | `tools/verify_agent_runtime.py` | 0 | `{"ok": true, "model_calls": 83}`（真实 Harness + 受控模型；证据 `var/agent-integration-v9vt6byn/evidence.json`） |
| 7 | `node apps/agent-runtime/verify-transition-recovery.mjs` | 0 | 2/2 case ok（含 `auth-failure-no-retry`，`tools_execute_once`、`result_consumed` 均为 true） |
| 8 | `tools/verify_campus_chains.py --out …/m2/chains-final.json` | 0 | **39 checks / 0 failed**，三条链 PASSED，`non_loopback_connections=0` |
| 9 | 同 #8 第二次（不同 `--out`，确定性） | 0 | 归一化后判定与答案字节一致 |
| 10 | `tools/verify_campus_chains.py --chains a --public-live --timeout 15` | 0 | 真实 example.org HTTP 200 / TLS 通过；`non_loopback_connections=1` |
| 11 | `tools/audit_public_network.py`（项目既有匿名探针） | 0 | 4 URL × system/direct 共 8 次，全 ok；`credentials=false`、`tls_verified=true` |
| 12 | `probe_real_campus.py`（真实只读） | 0 | 15.23 s：自动重登 1 次 `AUTO_LOGIN_OK`；grades 18 条、profile 1 条；门户 login_attempts=0 |
| 13 | `tools/audit_regression.py apps/api/tests/test_campus_readonly_boundary.py`（基线 HEAD worktree） | 1 | **6 failed / 6 passed**（修改前首次失败已保留，`baseline-pre-fix-run.txt`） |
| 14 | `tools/audit_transition_inventory.py` | 0 | 过期快照重新生成为 `domain_count=32`（旧快照留档） |
| 15 | `tools/verify_campus_readonly_live.py`（无开关） | **2** | 明确拒绝，且未读 provider/密钥、未调用模型 |
| 16 | `tools/verify_campus_readonly_live.py --self-test` | 0 | 判分自测 **34/34**（含证据投影与 offline guard 用例），`model_calls=0, provider_reads=0, credential_reads=0`；`var/personal.db`、`var/secrets` 的 size+mtime 前后一致 |
| 17 | `tools/verify_campus_readonly_live.py --model-live --out …/m3/model-live.json`（第一轮，原样保留） | **1** | 4 条真实模型会话；当时判定 1/4（T2 达成）；后续发现判定层投影缺陷，见 §5.5；全部未超预算；金额 unknown |
| 18 | `… --model-live --tasks T1,T3 --out …/m3/model-live-round2-t1t3.json --label judge-projection-fix`（修正后同任务对照） | **1** | **T3 达成**（首轮 4 项为判分假阴性）；T1 仍判未达成，但唯一失败项 `no_whole_school_claim` 命中否定句，证据矛盾，交验收方裁定；2 条会话 9 请求 / 15 工具，未超预算 |
| 19 | 同 `--out` 重跑（验证是否重判） | 0 | 4 条全部 `SKIP`、0 次模型调用；文件字节重写但逐条判定不变（已比对保留副本） |

清理：
- 只改任务所需文件；`git status` 中已跟踪改动仅 §1 列出的 10 个文件 + 3 个新增文件（2 个验证入口 + 1 个边界测试）。
- 未 reset/clean/stash；未覆盖任何既有未跟踪审计材料；未 commit/push/合并 PR。
- 临时物清理：基线对照 worktree `.worktrees/prefix-baseline` 已移除；探针临时目录（`campus-probe-*`、`campus-m0-*`、`campus-live-*`）
  已删除，脚本改为写入 `var/harness-campus-readonly-2b0480ed/` 下；`verify_campus_chains.py` 每次运行后自行删除其隔离树
  （`isolated workspace removed: True`）；`m2/_dbg*.json`、`m3/_dev_check.py`、`m3/_t3_debug.py` 等中间文件已删除。
  **需说明的一处清理过头**：清理时用了 `-Include '*.log'`，把真实模型四条会话的运行时 stdout/stderr 文本日志一并删除。
  权威原始证据仍在：`m3/model-live.json`（完整轨迹：call id、参数、状态、错误、usage）、`m3/model-live-summary.md`，
  以及每条会话的官方 Harness 轨迹 `session.v4.jsonl`、浏览器 `actions.json`、合成学校模块源码、`journal.sqlite`、
  校园缓存库与 `domain.sqlite`（四条会话各一份，已核对齐全）。缺少的只是运行时控制台文本日志。
- 个人数据：`var/personal.db`、`var/files`、`var/secrets`、`var/agent`、`var/workspace` 未被写入（探针只读凭据库并重定向其余根）。
  真实探针未打印/保存任何密码、Cookie、ticket、签名 URL 或个人记录，只记条数与状态码。

未完成条件（不声称已完成）：
- 学在浙大/智云课堂的真实账号联调：需要一次可见浏览器人工登录，且智云账号权限未确认 → 外部条件阻塞。
- 验证码/MFA 的真实路径：需要人工一次；本轮只验证了"有界的自动重登 + 冷却 + 不重投密码"。
- WebVPN 真人验证、应用浏览器真人验证：本轮刻意未触发（避免顶号），分别标注为未验证。
- 学院通知真实来源匹配、需具体学期的教务课表真实同步：未跑，保留为待验证。
- 上一轮遗留的 PR 范围项（基线说明、白名单、脱敏、真实 CI）不在本轮范围。


## 8. 未完成/不宣称的事项

- 本文件只声明"完成实施与自测"；`通过／需修改／外部条件阻塞` 的判决属于独立验收方。
- 不把 `turn=succeeded`、目录已注册、job 已受理或 HTTP 200 当作任务完成。
- 多用户托管不在本轮范围；`OWNER=local` 不是多租户证据。
- 未新增空壳连接器；智云/学在的真实账号可用性仍以"需一次人工登录 + 账号权限未确认"如实标注。
- **两项留给验收方裁定，我不自行定性**：
  1) T1 第二轮的唯一失败项 `no_whole_school_claim` 命中的是否定句（详见 §5.5），证据与判定矛盾，需人工/验收方裁定；
  2) `campus.query(college_notices)` 返回完整正文，与"C11 列表只给摘要"的意图可能存在冲突，属产品契约问题（§5.5）。
- 首轮真实模型记录 `m3/model-live-round1-as-run.json` 为不可变的首次失败记录；第二轮为判分修正后的同任务对照，两者并列保留，不互相替代。
