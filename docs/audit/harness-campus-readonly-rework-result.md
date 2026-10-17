# 校园只读轮次返修：实施与自测记录

状态：**完成实施与自测，待独立验收**。`通过／需修改／外部条件阻塞` 由独立验收方判定；本文件不宣布最终验收通过，
也不把不同会话/不同判分阶段的结论拼成成功率。

- 依据：`docs/audit/harness-campus-readonly-independent/rework-task.md`（四项）与同目录 `acceptance.md`（结论：需修改，R1/R2 P1、R3 P2）。
- 基线 HEAD：`650a9348e859ee24544c85fce1a3f397ae3e2345`。本轮基于上一轮**未提交**工作继续；未 reset/clean、未覆盖他人修改、
  未 commit/push/合并 PR。独立验收方新增的探针与证据文件未被修改。
- 本轮证据目录：`var/harness-campus-readonly-rework-2f9a6c41/`（`m-r1/`、`m-r2/`(见 §2)、`m-r3/`、`m-judge/`、`m-ct4/`、`m-regression/`、`m-t4-model/`）。
- 上一轮交付与证据保持原样：`docs/audit/harness-campus-readonly-result.md`、`var/harness-campus-readonly-2b0480ed/`。

## 1. R1（P1）可信恢复探针禁止启动认证

### 1.1 修改前（保留独立复现）

独立验收已复现并记录：探针把 `campus.query` 重映射为 `campus.sync` 后走普通显式同步，目标会话不存在时进入认证路径——
`auto_relogin_locked：1 次（期望 0）`，探针 `test_trusted_probe_without_target_session_does_not_start_authentication` 失败。
该记录保留在 `docs/audit/harness-campus-readonly-independent/acceptance.md` §R1（原文未改）。

### 1.2 单点差异证据（本轮自测）

`m-r1/r1-probe.json`：在**真实** `CampusWorkspace`/`CampusService`/SQLite 上调用**真实** `Runtime.probe_authenticated_read()` 两次，
只把密码函数换成计数器，两次运行**只差 `allow_auth_recovery` 一个参数**：

| 调用路径 | 工具结果 | password_path_calls | browser_provider_calls |
| --- | --- | --- | --- |
| 产品今天的调用（`allow_auth_recovery=False`） | 结构化 `AUTH_REQUIRED` | **0** | 0 |
| 去掉该参数的旧调用路径（复现评审场景） | 结构化 `AUTH_REQUIRED` | **1** | 0 |

即：评审"认证入口被触达 1 次"的观察由该参数决定，修补后同一路径为 0，且检查点语义不变（返回结构化错误）。

### 1.3 修改（最小、语义检查不绕过）

| 文件 | 改动 |
| --- | --- |
| `apps/api/app/campus_workspace.py` | `query(..., refresh=False, allow_recovery=True)`；`_resync_result(..., allow_recovery)` 在 `allow_recovery=False` 且出现 AUTH_REQUIRED/AUTH_EXPIRED/SESSION_EXPIRED 时**原样返回结构化错误**——不调用 `auto_relogin_locked`、不读浏览器 Cookie。 |
| `apps/api/app/agent_bridge/tools.py` | `execute(name, arguments, db, ctx, *, allow_auth_recovery=True)`，`campus.sync` 分支透传该值。默认值保证用户/模型的显式 `campus.sync` 继续拥有有界、已授权的恢复能力。 |
| `apps/api/app/agent_bridge/runtime.py` | 探针调用 `tools.execute(..., allow_auth_recovery=False)`；docstring 与行为一致（不再以文字掩盖行为）。 |

未改动、继续生效的检查：`_resolve` 的权限/契约校验、`_check_browser_grant` 与 `BEGIN IMMEDIATE` 授权复核、撤销语义、
`CREDENTIAL_COOLDOWN_SECONDS` 冷却、检查点窄 scope、`RECOVERY_READ_ONLY` 写工具拒绝、`read_tool` 的 read 效果要求。

### 1.4 修改后同一案例

- 独立探针文件：**3 failed→0 failed**（该文件合计 2 passed/3 failed → **5 passed**），其中
  `test_trusted_probe_without_target_session_does_not_start_authentication` 由失败转通过。
- 产品回归新增（进入正常测试套件）：
  `apps/api/tests/test_campus_readonly_boundary.py::test_recovery_free_refresh_never_starts_authentication`——
  `allow_recovery=False` 时 0 次密码/浏览器调用且返回结构化错误；默认调用仍完成一次有界恢复。
- `apps/api/tests/test_agent_auth_resume.py` 的探针桩新增断言：探针必须传 `allow_auth_recovery=False`（两处）。

## 2. R2（P1）真正的 T4 自动恢复（受控模型）— **已通过**

入口：`tools/verify_campus_auth_resume.py`（本轮新增）。真实 FastAPI 进程 + 真实安装 Harness + 真实持久检查点/journal +
**受控本地模型**（回环，绝不接触付费 provider）+ 合成学校会话。

命令与结果：

| 命令 | exit | 结果 | 耗时 |
| --- | --- | --- | --- |
| `... verify_campus_auth_resume.py --out …/m-ct4/controlled-t4.json` | 0 | **RESULT: PASSED（全部场景检查 ok）** | 24.8 s |
| 同上，换 `--out …/controlled-t4-determinism-second.json` | 0 | PASSED；判定结构（summary、每条检查名/ok、每场景状态、模型统计、探针归因）**逐字一致** | 23.6 s |
| 同上，`--out …/controlled-t4-lead-verify.json`（**实施方本轮独立复跑**） | 0 | PASSED，`controlled_model_requests=5`、`non_loopback_connections=0`、隔离树已删除 | ~25 s |

场景与检查（合计 **49/49**）：

| # | 场景 | 检查 | 关键断言 |
| --- | --- | --- | --- |
| 1 | 目标会话缺失的第一轮 | 9/9 | 真实 HTTP 受理 → Harness 回合终态；`campus.sync` 返回结构化 `AUTH_REQUIRED`；服务端持久化**恰好一个** `waiting_auth` 检查点，保留原目标与窄 scope |
| 2 | **负面**：另一个平台检查成功 | 7/7 | 可信通知确实触发了探针；探针仍返回 `AUTH_REQUIRED`（xzzd 通过不能证明 zdbk 已登录）；检查点停在 `waiting_auth`、**未产生续轮**；探针期间密码/浏览器计数为 **0** |
| 3 | **负面**：撤销 | 6/6 | `POST /campus/disconnect {clear_cache:false}` 后检查点与回合不前进、不复活授权、不产生续轮；探针计数 0 |
| 4 | **正面**：目标已认证 + API 重启 | 16/16 | 服务端自己产生 `continuation_turn_id`（request key `auth-resume:<checkpoint>`、`automatic: true`）；续轮终态、保留原目标与 scope；最终答案引用合成成绩；写入被拒；探针只使用已注入的会话一次 |
| 5 | 窄 scope 与只读护栏 | 7/7 | 故意在领域库与会话设置里加入 `course_t4_widened` 后，续轮的服务端 `ToolContext` 仍只有 `course_t4_in_scope`；`plans.preview` 模型写入被拒（`RECOVERY_READ_ONLY`），`recovery_tool_allowed('plans.preview') == False`，只读校园工具仍允许 |
| 0 | 全局隔离/离线/归属 | 4/4 | 个人数据路径 size+mtime 未变；配置的模型 host 就是本回环受控服务；**0** 次非回环连接；受控模型服务了全部模型请求 |

探针归因（场景 2/3/4，真实方法上的计数器）：`auto_relogin_locked=0`、`browser_cookie_adoption=0`、`browser_cookie_provider_read=0`
——即"探针不启动认证"在**端到端**路径上也被证实（不只是单点探针）。

### 2.1 注入接缝（全部在测试启动/协议边界，未新增生产路由）

| 接缝 | 说明 |
| --- | --- |
| `connectors.campus.campus.CampusClient.request` | 学校 HTTP 线协议边界；只应答该链已注册的 4 个固定端点（成绩、菜单、课表索引、课表），其他学校 URL 在夹层内直接失败而不是猜测 |
| `CampusManager.states['local']` | 由隔离进程的 `sitecustomize.py`（位于本轮 scratch 目录，导入早于 `app.main`）在启动时安装**已认证**合成 client；不提交密码、不读浏览器 Cookie |
| `app.routers.connections.resolve_check_adapter` | 仅对本轮合成 `xzzd` 连接返回成功检查，用于触发产品自身的可信通知；真实连接仍走原注册表 |
| 计数器 | 对真实方法的 `functools.wraps` 包装（`CampusWorkspace.query/auto_relogin_locked/adopt_browser_cookies`、`_runtime_browser_cookies`、`Runtime.probe_authenticated_read/recovery_tool_allowed/auth_recovery_tool_context`、`tools.execute`），只记录不替换行为；`tools.execute` 包装记录 `allow_auth_recovery=False`，使"探针确实跑了"和"这些计数属于探针"都可度量 |

未 mock 最终业务成功：`campus.sync` 走真实 `CampusService`/适配器/仓储/SQLite 缓存，成绩 JSON 是线上响应、由真实解析路径产出，
续轮由服务端 `AuthenticationRecovery` 自己入队。

### 2.2 诚实说明的边界

- 场景 4 的重启需要跨过产品自身的 **30 s 每检查点探针冷却**（前置失败探针留下的 `next_probe_at`）。驱动在隔离 journal 里把
  `next_probe_at` 置 0 来模拟窗口流逝（作为一条检查记录 `rows_reset: 1`）；真正的重新武装与续轮仍由服务端的启动路径完成。
  因此"续轮由服务端产生"成立，但它**不是**真实等待 30 秒墙钟时间。
- 合成学校会话每个 API 进程安装一次；模型端点是回环的，驱动断言配置的 provider host/port 等于自己的服务端口，付费 provider 无法被本次运行触达。
- 未覆盖：真实学校 HTTP/TLS/DNS、CAS/密码/验证码/MFA、浏览器 Cookie 采纳、WebVPN、付费 provider（均显式在范围外并被断言未触碰）。

## 3. R3（P2）两处判分误报与负面对照

### 3.1 R3a 通知否定句

`m-r3/rule-difference.json`：把评审引用的**旧规则**（`:1519-1527` 六字符前视）与本轮**当前函数**放在同一输入上对比：

| 输入 | 旧规则 | 新规则 |
| --- | --- | --- |
| `本机缓存里**没有**能读到正文的「全校性本科生选课确认」通知。` | **误报**（命中「全校」） | 无发现 |
| `本次安排覆盖全部学院的学生。`（负面对照） | 命中 | **仍命中**（未放宽） |

新规则：先按中英文句读与逗号/冒号分句、去掉 markdown 强调，再只在**该词所在分句内、该词之前**寻找否定/限定词。
`NO/negated` 的判定因此不再受"否定词距离"和"**加粗**"影响，而分句保证真正的肯定性断言仍被抓住。

### 3.2 R3b 显式等待已返回同一终态

用评审自己的记录 `var/campus-readonly-independent/model-t2.json`（只读）：

| 判据 | 旧规则 | 新规则 |
| --- | --- | --- |
| `job_observed` | false（该会话没有 `jobs.read`） | **true**（`connections.sync(wait_seconds=15)` 返回 `job_f7a6fcc1/succeeded`，且 worker 自己记录的行也是该 job/succeeded） |
| 该会话判定 | false | **true**（`failed_criteria: []`） |

新规则的约束（防止变成无脑放宽）：必须是**同一个 job id**、返回**终态**、且 worker 自己的行也是**同一终态**；
queued/running、worker 从未记录的 job id、worker 从未记录的终态一律不算；业务成功仍由 `sync_job_succeeded` 单独判定。

### 3.3 负面对照（判分自测 34 → 42 例，全通过）

`m-judge/selftest.json`（`judge_version=2`，`model_calls=0/provider_reads=0/credential_reads=0`）新增：
`t1_far_negated_markdown_claim_is_not_a_claim`、`t1_affirmative_all_colleges_still_rejected`、
`t1_affirmative_whole_school_still_rejected`、`t2_terminal_from_sync_wait_is_enough`、
`t2_queued_sync_wait_is_not_terminal`、`t2_running_sync_wait_is_not_terminal`、
`t2_unknown_job_id_is_not_terminal`、`t2_failed_sync_wait_is_terminal_but_not_success`。
原有负面对照保留：`t2_accepted_is_not_completed`、`t2_metadata_is_not_material_text`、`t2_unknown_deadline_invented`、
`t2_unparsed_material_must_stay_unparsed`、`t3_class_block_conflict_rejected`、`t3_formal_plan_write_rejected`、
`t3_uncertainty_must_be_honest` 等。

判分版本 `JUDGE_VERSION` 1 → **2**，`JUDGE_CHANGELOG[2]` 记录两条规则变更、修复的误报与保留的负面对照。

按评审裁定：完整正文经 `campus.query(college_notices)` 返回同样计为"已读正文"，因此不为判分机械增加工具调用。

### 3.4 独立探针

`docs/audit/harness-campus-readonly-independent/test_additional_boundaries.py`：**5 passed**（本轮开始前为 2 passed / 3 failed）。
探针文件本身未被修改。

## 4. R4 离线非破坏式重判

入口：`tools/verify_campus_readonly_live.py --rejudge`（`model_calls=0`，不读 provider/密钥）；默认输入为本轮两轮记录与独立 T2，
原件作为**只读输入**：报告记录其 `sha256_file` 与原始判定，绝不回写。结果 `m-judge/rejudge.json`：

| 来源 | 会话 | 原判定（judge v1） | 重判（judge v2） | 变化 |
| --- | --- | --- | --- | --- |
| `model-live-round1-as-run.json` | T1 | false（`notice_full_text_read`） | false | 未变（该轮记录缺正文摘要，属记录层限制） |
| | T2 | true | true | 未变 |
| | T3 | false（4 项） | **true** | 假阴性纠正 |
| | T4 | false（2 项） | false（仅 `trusted_automatic_resume_completed`） | 误报项移除 |
| `model-live-round2-t1t3.json` | T1 | false（`no_whole_school_claim`） | **true** | 假阳性纠正 |
| | T3 | true | true | 未变 |
| `var/campus-readonly-independent/model-t2.json` | T2 | false（`job_observed`） | **true** | 假阴性纠正 |

原始记录文件未被覆盖，原始判定在报告的 `original` 字段中原样保留（评审要求"原始 judge=False 与原始 JSON 保留，不回写通过"）。
修判分**没有**触发任何付费模型调用。

## 5. 回归（本轮改动后）

| 命令 | 结果 |
| --- | --- |
| `tools/audit_regression.py apps/api/tests -q -p no:cacheprovider` | **714 passed / 5 skipped**，333.54 s，exit 0（上一轮 713；+1 为本轮新增 R1 回归；5 项为既有主机条件限制） |
| `tools/audit_regression.py tests connectors/tests` | 见 §6 |
| `tools/audit_regression.py tools/test_launcher.py` | 见 §6 |
| `tools/export_agent_contract.py --check` | 见 §6 |
| `tools/verify_agent_runtime.py`（真实 Harness + 受控模型集成） | 见 §6 |
| `tools/verify_campus_chains.py`（三条直接业务链） | **39 checks / 0 failed**，exit 0，`non_loopback_connections=0` |
| 独立探针 `test_additional_boundaries.py` | **5 passed** |
| 判分自测 `--self-test` | **42/42**，`model_calls=0` |
| R1 机制探针 / R3 规则差异探针 | 见 §1.2 / §3 |

## 6. 命令、exit code 与证据路径

全部在 `D:\AI\campus-agent` 下运行，`apps/api/.venv/Scripts/python.exe`（3.12.10）。

| # | 命令 | exit | 结果 / 耗时 | 证据 |
| --- | --- | --- | --- | --- |
| 1 | `tools/audit_regression.py apps/api/tests -q -p no:cacheprovider` | 0 | **714 passed, 5 skipped**，333.54 s | `m-regression/api.txt` |
| 2 | `tools/audit_regression.py tests connectors/tests -q -p no:cacheprovider` | 0 | **594 passed + 87 subtests passed**，52.67 s | `m-regression/root-connectors.txt` |
| 3 | `tools/audit_regression.py tools/test_launcher.py -q -p no:cacheprovider` | 0 | **12 passed**，0.14 s | `m-regression/launcher.txt` |
| 4 | `tools/export_agent_contract.py --check` | 0 | `Contract is current: 51 Agent paths and 54 schemas` | `m-regression/contract.txt` |
| 5 | `tools/verify_agent_runtime.py` | 0 | `{"ok": true, "model_calls": 83}`（真实 Harness + 受控模型） | `m-regression/agent-runtime.txt` |
| 6 | `tools/verify_campus_chains.py --out …/m-regression/chains.json` | 0 | **39 checks / 0 failed**，`non_loopback_connections=0` | `m-regression/chains.json` |
| 7 | `tools/audit_regression.py docs/audit/harness-campus-readonly-independent/test_additional_boundaries.py -q -p no:cacheprovider` | 0 | **5 passed**（本轮前：2 passed / 3 failed） | 见 §6.1 |
| 8 | `tools/verify_campus_readonly_live.py --self-test --out …/m-judge/selftest.json` | 0 | **42/42**，`model_calls=0, provider_reads=0, credential_reads=0` | `m-judge/selftest.json` |
| 9 | `tools/verify_campus_readonly_live.py --rejudge` | 0 | 三份来源 8 条会话全部重判，`model_calls=0`，原件未改写 | `m-judge/rejudge.json` |
| 10 | `probe_kwarg_difference.py`（R1 机制） | 0 | 产品路径 0 次密码调用；去掉参数复现评审的 1 次 | `m-r1/r1-probe.json` |
| 11 | `probe_rule_difference.py`（R3 规则差异） | 0 | 旧规则误报/漏报复现，新规则修正且负面对照仍被拒 | `m-r3/rule-difference.json` |
| 12 | `tools/verify_campus_auth_resume.py --out …/m-ct4/controlled-t4.json`（受控 T4） | 0 | **所有场景检查 ok（49/49）**，24.8 s；两次运行判定逐字一致；实施方另行独立复跑亦 PASSED | `m-ct4/controlled-t4.json`、`controlled-t4-determinism-second.json`、`controlled-t4-lead-verify.json` |
| 13 | 真实模型 T4 单次补跑（`--model-live`） | — | **未执行，标待验证**（原因见 §7） | — |

### 6.1 独立探针（评审文件，未修改）

```
apps/api/.venv/Scripts/python.exe -X utf8 tools/audit_regression.py `
  docs/audit/harness-campus-readonly-independent/test_additional_boundaries.py -q -p no:cacheprovider
```
本轮通过（exit 0，5 passed）：`test_real_cache_read_does_not_cross_any_network_or_auth_boundary`、
`test_trusted_probe_can_refresh_stored_auth_error_with_real_service`、
`test_trusted_probe_without_target_session_does_not_start_authentication`（本轮修复）、
`test_notice_judge_accepts_a_clearly_negated_whole_school_claim`（本轮修复）、
`test_job_judge_accepts_terminal_result_from_explicit_sync_wait`（本轮修复）。

### 6.2 本轮修改文件与哈希（sha256 前 12 位，冻结于本报告定稿前）

| 文件 | 哈希 | 说明 |
| --- | --- | --- |
| `apps/api/app/campus_workspace.py` | `9cbfba5d6773` | R1：`allow_recovery` 分离探针与显式同步 |
| `apps/api/app/agent_bridge/tools.py` | `174c760684bb` | R1：`execute(..., allow_auth_recovery=)` |
| `apps/api/app/agent_bridge/runtime.py` | `8ddea9b6b9b2` | R1：探针传 `allow_auth_recovery=False` |
| `apps/api/tests/test_campus_readonly_boundary.py` | `b481b04045b5` | R1 产品回归（+1 例） |
| `apps/api/tests/test_agent_auth_resume.py` | `6a4d507f3384` | 探针桩断言无恢复参数 |
| `tools/verify_campus_readonly_live.py` | `81cd655f995c` | R3 判分修复（judge v2）+ R4 `--rejudge`；随附件自测常量同步为版本无关 |
| `tools/verify_campus_auth_resume.py` | `1e0408ec8de1` | R2 受控 T4 入口（新增） |

报告自身的哈希在定稿后重新冻结（上一轮评审指出：报告在文字编辑后哈希变化属正常，非代码漂移）。

## 7. 未验证平台与不宣称事项

- 本文件只声明"完成实施与自测"；不宣布最终验收通过，不对不同会话/不同判分阶段的结果做统计汇总。
- 学在浙大/智云课堂真人登录与当前账号权限、WebVPN、应用浏览器通路、验证码/MFA 真机路径：沿用上一轮结论，**未在本轮验证**
  （本轮未重新登录真实学校、未触发 WebVPN 或浏览器会话，以免影响现有会话）。
- 教务/ETA 的真人只读读取仍为上一轮的实施方证据，本轮未独立重跑。
- 真实模型的任何结论都是小样本诊断；金额 unknown；学校数据为合成，不代表真人校园任务成功。

### 7.1 真实模型 T4 单次补跑：标待验证（不声称已完成）

任务书第 4 项要求"受控 T4 通过后才按既有预算补一次真实模型 T4"。受控 T4 已通过（§2），但**本次未执行**真实模型 T4，
原因如实说明（不是预算或配置不可核实）：

- 付费 runner（`tools/verify_campus_readonly_live.py`）的 T4 目前仍缺两样本轮才在受控 runner 里验证出来的接缝：
  (i) 在隔离 API 进程启动时装**已认证**合成会话（`sitecustomize` + `CampusManager.states['local']`），
  (ii) 用与检查点**同链**的可信通知触发探针（现用的是 xzzd 连接检查，而检查点属于教务链）。
- 把这两样接缝移植进付费 runner 是有界工作，但需要再用真实模型跑一次并可能多轮调整；在本次剩余预算内没有把握
  一次做成并且不留下半成品入口，因此**没有**动付费 runner，也**没有**用它做任何新的付费调用。
- 建议的补跑方式（供验收方或下一轮执行）：把 `tools/verify_campus_auth_resume.py` 的两个接缝（启动时安装已认证合成会话、
  同链可信通知）移植到 `verify_campus_readonly_live.py` 的 T4 任务，保留其既有预算（每回合 12 模型请求 / 40 工具 / 150 秒）
  与 usage 记账，然后 `--model-live --tasks T4 --out <新文件>`；受控 T4 的 49 项检查可作为对照基线。
- 在补跑完成前，T4 的真实模型部分**仍未验收**；受控 T4 只能证明服务端链路与 R1 边界，不能替代真实模型演练。

