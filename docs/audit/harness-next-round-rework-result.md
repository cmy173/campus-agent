# Harness 返修结果（针对 `harness-independent/acceptance.md` 的 F1—F5）

> 本文件是**实施方交付说明**，不是验收结论。结论由原独立验收方给出：`通过／需修改／外部条件阻塞`。文中所有“通过”均指对应命令/测试在本机实际执行的结果，并附证据文件路径；真实模型结论严格按证据集分开统计。

- 工作目录：`D:\AI\campus-agent`；时间：2026-10-15（Asia/Shanghai）
- 返修依据：[`harness-independent/acceptance.md`](harness-independent/acceptance.md)（其中 §2 的 F1–F5、§5 的复验命令）
- 上一轮交付说明：[`harness-next-round-result.md`](harness-next-round-result.md)（保留不动，供对照）
- 本轮证据目录：[`harness-next-round/`](harness-next-round/)（新文件均为 `rework-*` 前缀；旧证据文件未改写）
- 进入基线：工作区仍有大量**未提交修改**（`git status --porcelain` 169 项）。本轮**未** `reset`/`clean`/`stash`/整目录覆盖/自动 commit/push，未写入 `var/personal.db`、`var/secrets`、个人校园缓存或普通浏览器 profile。原独立验收方新增的 2 个边界测试文件**未删除、未 skip、未削弱断言**（其中 F1 的测试因执行单元更换而改写为驱动新的真实子进程，见 §2.1）。

---

## 1. 返修范围与保留不变量

F1–F5 全部落实，另有 4 处在本轮自测中被发现的产品/证据缺陷一并修掉（§5）。**没有**重写 Harness，**没有**开放任意 Shell/`run_code`/PTC/workflow，**没有**只改提示词或提交报告了事：登录、缓存、scope、权限、停止语义、TLS/SSRF/Origin/runtime-token 校验、出卷 REST 契约均按原样保留。

| 入口 | 状态 |
|---|---|
| 模型可见工具数 | **32**（未增未减；`jobs.read` 仅新增可选参数 `wait_seconds`） |
| 任意 Shell / 代码执行 | 仍然禁用（能力策略在执行前拒绝；`verify_agent_runtime.py` 的不可见断言继续通过） |
| 数据库迁移头 | 单 head `a7c3e5b91d24`（`alembic upgrade head` 在隔离库通过） |
| 出卷 REST 行为 | 由 `review_service` 承载，21 项 REST 测试原样通过 |

---

## 2. F1—F5 逐条对应

### 2.1 F1 · P1 渲染超时没有终止渲染，且上下层超时预算冲突

**根因**：`Thread.join(timeout)` 到期只抛错，线程仍在跑；线程无法被安全终止，所以"已中止且未交付"不成立。另外域工具围栏 20 秒 < 渲染上限 30 秒，模型可能先看到工具超时而后端继续执行。

**改为**：渲染在**服务端自有的子进程**内执行（`apps/agent_bridge/artifacts.py` 的 `_render`）。

- 命令 = `sys.executable` + 固定脚本路径（`RENDER_COMMAND`，服务端常量）+ `--formats`（闭集）+ `--output-dir`/`--stem`（`--stem` 已由 `ExportArgs` 拒绝目录分隔符与保留字符）。**模型内容一律不进 argv**：写入 staging 内的 `spec.json`，由渲染器从 `--input` 读取。
- 成败路径：`Popen` → `communicate(timeout=RENDER_TIMEOUT_SECONDS)`；超时则 `terminate()` → 有界 `wait` → 需要时 `kill()` → **再次有界 `wait` 回收**，之后才抛 `ARTIFACT_TIMEOUT`。因此报告"已中止且未交付"时子进程已不可能再写文件。
- 收尾有界：`_drain` 用守护线程 + 固定等待读取残留输出，任何管道异常都不会把调用拖过围栏。
- 子进程环境由服务端设定 UTF-8 与无缓冲（`PYTHONUTF8`/`PYTHONIOENCODING`/`PYTHONUNBUFFERED`）；这是服务端配置，模型无法提供环境值。
- **deadline 一致**：`RENDER_TIMEOUT_SECONDS = 15` + `RENDER_TEARDOWN_SECONDS = 2` **严格小于** `DOMAIN_TOOL_FENCE_SECONDS = 20`；模块导入时即断言该关系，并有测试读取 `campus-plugin.mjs` 的 `timeoutMs: 20000` 复核（若围栏被改小、deadline 未跟上，测试直接失败）。

**故障对应测试**

| 测试 | 断言要点 |
|---|---|
| `docs/audit/harness-independent/test_acceptance_boundaries.py::test_timeout_stops_renderer_before_reporting_abort` | 真实可终止子渲染器；`ARTIFACT_TIMEOUT` 返回后其 pid 已不存在（OS 级存活检查），且它确实已开始写文件（否则探针不成立） |
| `apps/api/tests/test_artifacts_export.py::test_a06_timeout_terminates_and_reaps_the_render_process` | 同场景在 0.5 秒上限下；abort 后再等 4 秒，`late-render.txt` 不出现、无存活子进程、无交付文件 |
| `...::test_a06_render_deadline_fits_inside_the_domain_tool_fence` | 15+2 < 20，且 < 运行时实际围栏 |
| `...::test_a06_renderer_missing/deadline/timeout` 系列 | 超时非"永久缓存错误"，`retryable=true`，且重复超时重试不累积资源（staging 每次删除） |

> 说明（透明记录）：原 F1 测试用 `SimpleNamespace(export=...)` 注入**进程内可调用对象**，并断言"API 报 aborted 后仍发生文件 IO"。执行单元换成子进程后该注入方式不再存在（这正是返修要求的方向："固定渲染器使用可终止且能够回收的受控执行单元"）。因此该测试改为驱动新的执行单元——**真实子进程**（不是 mock、不是内存对象），断言强度提高为 OS 级进程存活检查；其余 4 个边界测试与全部断言未动。

### 2.2 F3 · P2 多格式发布中断的状态不真实，建议的重试路径实际上不可用

**根因**：逐个 `save` 后才写整体回执；第二个格式失败时第一个文件已真实可下载，但错误仍称"未交付"，且重试会被网关缓存直接回放成永久错误。

**改为**：**整批全有或全无**（`artifacts.export`），并让"可重试"真实可用。

- 先渲染全部格式并校验文件名与渲染器报告一致，才开始发布；任一步失败 → `_discard_published` 只删除**本批自己刚创建**的行（`ConversationFiles.discard` 校验 `conversation_id`，先删索引再删 blob），因此"未交付"就是零文件，且绝不触碰此前或他人的文件。
- 回滚成功：`ARTIFACT_PUBLISH_FAILED` + `retryable=true` + 文案"未交付任何文件（批次已回滚），可以安全重试同一调用"。
- 回滚不完整（真实限制，不隐藏）：`retryable=false`，并在 `error.details` 里给出**真实已交付文件的行**（仅服务端自有字段：id/name/size/sha256/mime_type/created_at）与 `retained_file_ids`/`failed_format`，文案明确"这些文件真实存在、先用它们、不要重复同一调用"。该 `details` 只在确有内容时出现，普通错误不改变原有信封形状。
- **安全重试入口**：网关在 `cached_call` 命中时，若缓存信封自身是 `retryable=true` 的错误，则**重新执行**该调用（`_retryable_error`）；`cache_call` 变为按 call id 幂等（同请求指纹才允许覆盖，不同指纹仍 `IDEMPOTENCY_CONFLICT`）。因此"同一可信调用 ID + 同参数"能真正重试，而**换内容重试仍被拒绝**。
  - 注意：业务计费的重复受理不是靠缓存拦住的。`reviews.generate` 有自己的业务身份与回执（见 F2），F2 实施方核查后确认"同一指纹重放不可能二次扣费"。这一区分是本轮唯一放宽缓存的地方，且有测试覆盖。

**故障对应测试**

| 测试 | 断言要点 |
|---|---|
| 边界 `test_second_format_publish_failure_has_no_hidden_partial_delivery`（原样保留） | 第二格式失败 → `ARTIFACT_PUBLISH_FAILED` 且会话内 output 列表为空 |
| 边界 `test_publish_transient_failure_same_call_can_recover`（原样保留） | 同一 call id 重试后 `error is None`（不再回放永久缓存） |
| `test_a06_second_format_publish_failure_leaves_no_partial_delivery` | 第一次 `save` 真的落库后再失败；事后索引与 blob 均为空 |
| `test_a06_transient_publish_failure_can_recover_under_the_same_call` | 重试前零文件、重试后**恰好一个**文件且内容与请求一致，`save` 尝试次数=2 |
| `test_a06_same_call_id_cannot_swap_content_after_a_retryable_failure` | 同 call id 换内容 → HTTP 409 `IDEMPOTENCY_CONFLICT`；已完成调用的回执层同样拒绝换内容 |
| `test_a06_a_failed_batch_never_removes_an_earlier_export` | 回滚不删除上一次请求的产物，且该文件仍可下载 |
| `test_a05_existing_files_are_never_deleted_by_a_failed_export`（原样保留） | 渲染失败不删除既有产物 |

### 2.3 F2 · P1 真实模型等待出卷时重复提交付费业务，耗尽预算后没有交付答复

**根因**（独立验收的真实轨迹 `t2-1-ff0459e9`）：同 call ID 重放有保护，但模型换 call ID 再提交同一在途请求，会受理第二个付费 job。

**改为**：把出卷的身份从"调用 ID"提升为**业务身份**（`apps/agent_bridge/review_actions.py`）。

- `business_key = 会话 + 课程 + 模式 + 解析后的 provider + 冻结材料快照指纹`；`wait_seconds`、call id、`new_generation` 都**不**属于身份。指纹在**只读** `resolve_accept_context` 之后取得，未受理即失败，不产生副作用。
- `generate()` 顺序：同 call ID 回执重放（内容不同仍 409）→ 只读解析与 `check_preview` → 按 `business_key` 取本会话最新的受理回执：
  - job 为 `queued`/`running` → 复用，`reused=true`、`reused_reason=identical_request_in_flight`，**不新建、不重复计费**；
  - job 已 `succeeded` → 复用该已完成试卷，`reused_reason=identical_request_completed`；
  - job 为 `failed`/`cancelled`/`partial_failed`（或行不存在）→ 不复用，正常新建，便于恢复。
- 显式新业务：工具参数 `new_generation=true`（机器可判定，不靠提示词）。
- **节省模型轮数的有界观测**（F2 明确要求"预留最终交付预算"）：本次实测发现真实 worker 耗时 15–45 秒且波动大，单次调用（围栏 20 秒）不可能覆盖。因此：
  - `reviews.generate(wait_seconds≤14)` 保持"受理即等待"；
  - `jobs.read` 新增可选 `wait_seconds`（≤14），**只对出卷类 job 生效**，同步类 job 仍是普通读取；
  - 两者共用按 **job 受理时刻**计算的窗口（`wait_window_seconds` 总窗口 14 秒、`job_wait_window` 总上限 45 秒），重复调用**不会重置**窗口，超限立即返回真实状态并给出 warning；等待只改变观测量，绝不把"受理"写成"成功"。
- 归属不变：`owns_job`/`owns_paper` 仍按本会话回执校验；`business_key` 只留在回执 JSON 内并从模型可见响应中剥离。

**真实模型证据（返修中间集 `rework_v1`，与首轮分开统计；最终集见 §3.3）**

> 这一集（`rework-live-model.json`）跑在"F1–F5 已修、但还没有 `jobs.read` 有界等待"的源码上；下表如实保留它的失败，随后才补了有界等待并在最终源码上重跑。

| run | 结果 | 关键观测 |
|---|---|---|
| t2-1-e491db8a | 完整交付 3 题复习卷 | 1 个 job，`wait_seconds=14` |
| t2-2-b0f50296 | 完整交付 3 题复习卷 | 1 个 job，本调用内观测到终态 |
| t2-3-5ec4b475 | **未完整交付**（turn failed/BUDGET_EXCEEDED，最终答复为空） | 第 2 次 `reviews.generate` **复用**同一 job（`reused=true`、`identical_request_in_flight`、job 相同，**未产生第二个付费 job**）；worker 真实耗时 **43.0 秒**（`created_at`→`finished_at`），模型仍用了 7 次 `jobs.read` 轮询，第 12 次模型调用被预算上限中止；turn 结束后核验该 job 真实 `succeeded` 并有一份 v1/3 题试卷 |

`t2-3` 说明 F2 的"重复计费"缺陷已修复（此前该场景会受理第二个 job），但"长任务 + 有限预算下的编排"仍有真实缺口，且该证据集修改前（无 `jobs.read` 等待）产生，因此**不把 `t2-3` 当作已解决**；随后补了 `jobs.read` 有界等待并在最终源码上重跑 3×3（见 §3.3）。

**故障对应测试**

| 测试 | 断言要点 |
|---|---|
| `test_f2_a_new_call_id_with_the_same_request_reuses_the_in_flight_job` | 第二次同业务、不同 call ID 的请求 `reused=true`、`identical_request_in_flight`，job/paper 相同，且 `jobs`/`review_papers`/`receipts` 计数仍为 1/1/1（观测真实行数） |
| `test_f2_an_explicit_new_generation_is_a_machine_checkable_signal` | `new_generation=true` → 计数 2/2/2 且 job 不同 |
| `test_f2_failed_prior_job_still_allows_regeneration` / `..._cancelled_...` | 真实 worker 失败/真实取消后仍能新建（2/2），恢复路径不被去重堵死 |
| `test_f2_completed_identical_request_is_reused_not_rebilled` | 完成后重放 → `identical_request_completed`、`revision=1`、计数仍 1/1/1 |
| `test_f2_a_changed_business_request_is_not_deduped` | 备注变化 → 2/2（身份对业务敏感） |
| `test_f2_a_reused_generation_cannot_reset_the_wait_window` | 旧 job 重放的等待窗口为 0（不能靠重复调用把一轮拖住） |
| `test_jobs_read_wait_covers_a_slow_worker_without_a_polling_loop` | 带 `wait_seconds` 的 `jobs.read` 观测到真实终态；同一 job 未产生新行；不暴露 payload |
| `test_jobs_read_wait_cannot_spin_past_its_own_cap` | 超过服务端总窗口后立即返回（<5 秒）真实 `queued/running`，不重复等待 |
| `test_jobs_read_does_not_wait_on_other_job_types` | 同步/解析类 job 仍是普通读取（不等待、不阻塞、不改契约） |

### 2.4 F4 · P2 读卷适配器丢失试卷主键

**根因**：`to_paper_contract()` 返回键 `id`（`models.py:805`），适配器却读 `payload.get('paper_id')`，故 `reviews.read.data.paper.paper_id` 恒为 null。

**改为**：`read()` 显式映射 `payload["id"]`（回退 `paper.id`），并在 read/generate/export 统一使用同一个 `paper_id` 字段。

| 测试 | 断言要点 |
|---|---|
| `harness-independent/test_review_boundaries.py::test_read_preserves_completed_paper_identity`（**原样保留，未改**） | 真实 worker 完成后 `read.data.paper.paper_id == generate 返回的 paper_id` |
| `apps/api/tests/test_reviews_agent.py::test_f4_read_paper_id_is_directly_usable_for_export` | 读卷返回的 `paper_id` 非空、与受理一致；原契约确有 `id` 而无 `paper_id`；直接用它 `reviews.export` 成功且回显同一 id/revision |

### 2.5 F5 · P2 真实模型验收评分漏掉最终交付

**根因**：`judge_t2` 只查 job/读卷/文件/快照，未要求回合成功与最终回答；导致 `t2-2-00bc3c8c`（turn failed、final_answer 长度 0）被记为达标。

**改为**：**judge v4**（`tools/verify_next_round_live.py`，`JUDGE_VERSION=4`，旧判定保存在 `goal.previous_verdicts`，旧证据文件未被改写）。

- 三门任务统一新增 `turn_succeeded` + `final_answer_nonempty`：failed/cancelled/stopped 的回合即使有可用文件也**不算完整交付**。
- T2 新增 `single_operation_lineage`：文件 → paper → revision → 冻结快照 → 已受理 job 必须来自**该次运行自己的** `reviews.export` 信封（不再"最后一次 generate"拼"第一张 paper"）；另有 9 项一致性检查（job 属于该 paper 且 succeeded、模型确实观测到终态、读卷与快照一致、内容与该 revision 的题目一致、preflight 指纹等于快照指纹），并要求最终回答点名交付文件与其 paper/job id。
- T1 要求每个下载文件来自本运行自己的 `artifacts.export` 回执（id+sha256）。
- 判定明细记录额外生成、重复受理尝试、复用标记、取消（调用数与真实 cancelled job）。
- 阶段保护：新运行带 label + 产品源码摘要；同一文档混入两个阶段会 `phase_mixed=true` 且阈值强制不通过；门槛不变（9 次、≥8、每类 ≥2）。禁止跨代码阶段拼接九次。
- `--rejudge`/`--self-test` 移到 `--live` 门禁与任何 provider/凭据读取**之前**：复核已存证据可证明**零模型调用**（输出记录 `model_calls: 0`）；`--live` 仍拒绝无授权运行（exit 2 且不写文件）。

**故障对应测试与修正后的判定**

| 项目 | 结果 |
|---|---|
| `--self-test` 12 例 | 12/12 通过，`model_calls: 0`（含"失败回合但有可用文件 → 不算完整交付"反例、正例对照、额外生成不替换已交付操作、身份不一致、混合阶段不可通过、旧判定保留、真实 F2 轨迹只读复核） |
| 首轮九次重判（`live-model-rejudged-v4.json`） | **6/9**，T1 3/3、T2 0/3、T3 3/3，`thresholds.passed=false`（与独立验收一致） |
| `live-model-t2-final3.json` 重判（`live-model-t2-final3-rejudged-v4.json`） | **2/3**（脚本旧口径曾报 3/3）；`t2-2-00bc3c8c` 由 `achieved=true` 更正为 **false**（turn failed/BUDGET_EXCEEDED、答复为空），而它的 lineage 仍通过——正好证明"有真实业务进展"不等于"完整 Agent 交付"；原件 sha256 未变 |

---

## 3. 同一最终源码上的证据集

证据分三类，**分别统计**，不合并不同阶段/不同源码哈希的运行。

### 3.1 直接工具/业务层（确定性）

| 命令 | exit | 结果 |
|---|---|---|
| `audit_regression.py apps/api/tests -q -p no:cacheprovider` | 0 | **679 passed / 5 skipped**（246.14 s） |
| `audit_regression.py tests connectors/tests` | 0 | **594 passed / 87 subtests**（48.28 s） |
| `audit_regression.py` 边界四件（独立验收复现命令） | 0 | **4 passed**（7.28 s）——返修前为 4 failed |
| `npm run test:audit`（`apps/agent-runtime`） | 0 | 85/85 |
| `verify_agent_runtime.py` | 0 | `{"ok": true, "model_calls": 83}`（受控模型） |
| `node apps/agent-runtime/verify-transition-recovery.mjs` | 0 | 两例通过，`llm_retries: 1` |
| `node apps/web/node_modules/typescript/bin/tsc -b apps/web` | 0 | 通过 |
| `export_agent_contract.py --check` | 0 | 51 paths / 54 schemas |

5 个 skip 与基线一致，均为宿主限制（`test_agent_data_management.py:246` ×3、`test_agent_file_scope.py:127` ×2，`WinError 1314` 无法创建符号链接）。E03 的 skip 基线比对记录 `Not in baseline: []`。

独立验收的复现命令现状：

```powershell
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py docs/audit/harness-independent/test_acceptance_boundaries.py docs/audit/harness-independent/test_review_boundaries.py -q -p no:cacheprovider
# 4 passed（返修前 4 failed）
```

### 3.2 真实 API + Harness + 受控模型（确定性 12 类）

`rework-deterministic.json`：run id `edd4ad25061542e7bde4ce5c4584610b`，**12/12 类通过、74 项检查通过，exit_code 0**，224.75 秒，265 条 trace，`paid_model_called: false`（合成模型 20 次请求：agent 14、review_generate 5、repair 1），产物已重新下载解析并记录 sha256。E03 断言个人数据根（`personal.db`/`study.db`/`files`/`secrets`/`agent`/`workspace`）字节指纹前后一致、产品源码在运行中未被改动、未改动任何既有测试文件。该报告产出于最终源码（`tools/verify_next_round_e2e.py` 未再变动）。

### 3.3 真实模型（同一最终源码，固定配置与原预算）

实际模型运行（`rework-live-model.json`、`rework-live-model-summary.md`，标签 `rework_v1`，源码 = 不含 `jobs.read` 有界等待）与**最终**运行（`rework-final-live-model.json`、`rework-final-live-model-summary.md`，标签 `rework_final`，源码 = 最终冻结哈希）分别统计，不合并：

| 证据集 | 标签 | 完整交付 | 每类 | 门槛 | 说明 |
|---|---|---:|---|---|---|
| 首轮 9 次（上一轮交付） | — | 6/9 | T1 3/3、T2 0/3、T3 3/3 | 不满足 | 独立验收已复核；judge v4 重判仍为 6/9 |
| T2 单类修复后复测（旧口径 3/3） | — | 2/3 | T2 2/3 | 不适用 | judge v4 更正：`t2-2-00bc3c8c` 回合失败，不算完整交付 |
| 返修中间集（F1–F5 + judge v4） | `rework_v1` | 8/9 | T1 3/3、T2 2/3、T3 3/3 | 满足 | `t2-3` 失败：worker 43 秒 + 轮询耗尽模型预算（该集在补 `jobs.read` 有界等待之前） |
| **最终集（当前冻结源码）** | `rework_final` | **9/9** | **T1 3/3、T2 3/3、T3 3/3** | **满足** | 见下表 |

最终集逐次（每次独立 UUID 工作区，预算固定 12 模型请求 / 40 工具调用 / 150 秒，未放宽）：

| run | 任务 | turn | judge v4 | 模型调用 | 工具调用 | 秒 | 真实 job | 交付物 |
|---|---|---|---|---:|---:|---:|---|---|
| t1-1-db8590d9 | t1 | succeeded | 达标 | 3 | 3 | 30.9 | — | PDF 97,060 B（2 页）+ XLSX 7,031 B（2 sheet） |
| t1-2-923ed23b | t1 | succeeded | 达标 | 3 | 3 | 31.1 | — | PDF 97,924 B（2 页）+ XLSX 5,692 B |
| t1-3-60b9e5bf | t1 | succeeded | 达标 | 3 | 3 | 33.8 | — | PDF 104,256 B（2 页）+ XLSX 6,996 B |
| t2-1-2ed88943 | t2 | succeeded | 达标 | 9 | 10 | 51.2 | job_f6f0c0be succeeded | `合成实验课程 复习卷-完整卷.md` 3,071 B（3 题、含答案） |
| t2-2-fd406bc7 | t2 | succeeded | 达标 | 9 | 9 | 38.1 | job_405c9649 succeeded | 同上 3,601 B |
| t2-3-c3dc0081 | t2 | succeeded | 达标 | 9 | 10 | 38.4 | job_c1f36bfd succeeded | 同上 3,499 B |
| t3-1-b6b6ed37 | t3 | succeeded | 达标 | 5 | 8 | 23.6 | — | 按设计不生成假卷 |
| t3-2-2aa1bbce | t3 | succeeded | 达标 | 6 | 10 | 27.3 | — | 同上 |
| t3-3-eb8c2222 | t3 | succeeded | 达标 | 5 | 10 | 29.0 | — | 同上 |

Lead 独立复核（不是只读脚本自报）：

- 聚合 `thresholds`：`runs_completed 9`、`runs_achieved 9`、`need_runs_achieved 8`、`per_task {t1:3,t2:3,t3:3}`、`automatic_failures []`、`phase_mixed false`，阶段标识 `rework_final:f03e496dae97`；Lead 用 `source_phase()` 的算法（相对路径 + 文件字节）重算，得 `f03e496dae97`，与记录一致——真实模型证据与确定性证据属于**同一源码**。
- 每次 T2 只有 **1 个** `review_generate` job，`extra_generation_jobs []`、`duplicate_acceptance_attempts []`、`cancellations {cancel_calls:[],cancelled_jobs:[]}`：F2 关心的"重复受理付费业务"在这些运行中未再出现。
- 每次 T2 的 `single_operation_lineage` 9 项一致性检查全为 true（文件 sha256 与导出回执一致、paper 存在、revision/snapshot 与导出一致、job 属于该 paper 且 succeeded、模型确实观测到终态、读卷一致、卷面内容与该 revision 的题目一致、preflight 指纹与快照一致），且最终回答点名了交付文件与 paper/job id（`answer_cites_delivery` 为 true）。
- 产品产物由 Lead 重新下载并解析：PDF 均为真实 PDF（`pypdf` 2 页）、XLSX 由 `openpyxl` 打开（2 个 sheet、14–15 行 × 3 列）、Markdown 含 3 个 `## ` 章节且包含"答案"。
- 原始轨迹：`traces-live/<run_id>.json`；产物副本：`artifacts-live/<run_id>/`；sha256 记于证据 JSON 的 `artifacts` 字段。

**仍然要如实说明的限制**：9/9 是 3 类任务各 3 次的**小样本**，不构成生产成功率；本轮 worker 实测耗时在 15–43 秒之间波动（`rework_v1` 集内 `t2-3` 为 43.0 秒），因此"单次调用内观测到终态"不是稳定性质；真实模型采样参数不可控（桥不暴露 seed/temperature），不声称确定性。


---

## 4. 源码哈希与工具清单

- 24 个产品/测试/证据脚本文件 + 迁移头的 sha256：[`rework-source-hashes.json`](harness-next-round/rework-source-hashes.json)（含 `apps/api/app/agent_bridge/{artifacts,files,review_actions,job_actions,store,tools}.py`、`routers/agent.py`、`models.py`、`review_service.py`、`review_export.py`、`routers/review_papers.py`、运行时 `campus-plugin.mjs`/`prepare-profile.mjs`/`SKILL.md`/`export_documents.py`、三个测试文件、两个独立边界测试、两个证据脚本；`alembic_heads = a7c3e5b91d24 (head)`）
- 工具清单：[`next-round-tool-inventory.json`](harness-next-round/next-round-tool-inventory.json)（32 项；`jobs.read` 新增可选 `wait_seconds`，`reviews.generate` 新增可选 `new_generation`；无新增工具族）
- 与上一轮的差异（便于独立验收定位），仅列本轮被修改或新增的文件：

| 文件 | 本轮改动 |
|---|---|
| `apps/api/app/agent_bridge/artifacts.py` | F1/F3：子进程执行单元 + 可终止可回收 + 收尾有界 + deadline 断言 + 全有或全无发布 + 诚实部分交付详情 |
| `apps/api/app/agent_bridge/files.py` | `discard()`：仅删除本会话、属于本批次的已发布文件 |
| `apps/api/app/agent_bridge/store.py` | `cache_call()` 幂等化：同 call id 同指纹可覆盖（重试落库），不同指纹仍 409 |
| `apps/api/app/routers/agent.py` | 可重试错误缓存不再回放；`error.details` 仅在确有部分交付时出现（保持原信封形状） |
| `apps/api/app/agent_bridge/review_actions.py` | F2 业务身份复用；F4 `read()` 主键映射；`wait_window_seconds`/`job_wait_window` 有界观测 |
| `apps/api/app/agent_bridge/tools.py` | `jobs.read.wait_seconds` 有界观测与 warning；出卷描述与复用/`new_generation` 文档；工具数仍 32 |
| `apps/api/tests/test_artifacts_export.py` | F1/F3 回归测试（子进程存活、无迟到 IO、回滚、重试、换内容拒绝） |
| `apps/api/tests/test_reviews_agent.py` | F2 去重/复用/恢复、F4 read→export、`jobs.read` 有界等待与上限 |
| `apps/api/tests/test_agent_tools.py` | `new_generation` 与 `jobs.read.wait_seconds` 的 schema 断言 |
| `docs/audit/harness-independent/test_acceptance_boundaries.py` | F1 测试改为驱动新的真实子进程（断言意图保留并强化，见 §2.1） |
| `apps/agent-runtime/skills/document-artifacts/SKILL.md` | 只描述**已落地**行为：子进程渲染与超时语义、整批回滚与部分交付详情、业务身份复用、`jobs.read` 有界等待 |
| `apps/agent-runtime/prepare-profile.mjs` | 同上（persona 文案） |
| `tools/verify_next_round_e2e.py` | A05 故障注入改用真实子渲染器脚本（`RENDER_COMMAND`），不再注入进程内可调用对象 |
| `tools/verify_next_round_live.py` | F5 judge v4（回合成功/最终回答/单一业务 lineage/阶段保护），`--rejudge`/`--self-test` 提前到门禁之前 |

新增文件：`docs/audit/harness-next-round/rework-{deterministic,live-model,live-model-summary.md,rework-judge-v4-selftest.json,source-hashes.json}`、证据脚本无新增（沿用 `tools/verify_next_round_{e2e,live}.py`）。

---

## 5. 返修中一并修掉的其他真实缺陷（自测发现）

1. **相对 staging 路径在 `cwd=staging` 下自指**：`_render` 改为 `Path(staging).resolve()`，否则子进程把 `spec.json` 解析到自身目录内。
2. **`--formats` 未传给子进程**：CLI 默认值是 `pdf,xlsx,md`，导致只请求 Markdown 也走 Excel 校验并报 `Excel 至少需要一张 sheet`。改为服务端构造闭集参数。
3. **非 ASCII 标题/文件名 mojibake**：子进程继承宿主 ANSI 代码页，JSON 结果回传的文件名与磁盘名不一致（真实渲染器路径会产生 `ARTIFACT_RENDER_FAILED` 假故障）。改为服务端强制 `PYTHONUTF8/PYTHONIOENCODING`，并保留"渲染器报告的文件必须与磁盘一致"的交叉校验。
4. **管道 EOF 延迟可把调用拖过围栏**：`_drain` 用守护线程 + 固定等待，任何残留句柄都不会让收尾无限等待。
5. **同 call id 重试击穿 `tool_calls` 唯一约束**：`cache_call` 幂等化（同指纹覆盖、异指纹 409），否则"可安全重试"的重试会以 500 失败。
6. **`error_envelope` 副作用**：原实现给所有错误都加 `details: None`，破坏既有 `INVALID_INPUT` 契约与"其他异常细节不外泄"测试（由 F2 实施方独立发现）。改为仅在确有部分交付时添加。
7. **A05 证据脚本仍注入旧的 `RENDERER_PATH`/`load_renderer`**：确定性运行首次复跑 A05 报假失败（`NO_ERROR` + 1 个产物），改用真实子渲染器脚本后 12/12 通过。**该假失败被如实记录并修复，不是把断言改宽**。

---

## 6. 用量、成本与不确定边界

- 预算：每一次真实模型运行都是 **12 模型请求 / 40 工具调用 / 150 秒**，未提高；`wait_seconds` 上限 14 秒是**观测量**，不改变任何计费操作。
- 主 Agent token 用量见 `rework-live-model.json` 各 run 的 `agent_reported_usage`；**worker（复习卷生成）的 token 与真实货币费用仍为 `unknown`**——没有账单依据，不能用主 Agent usage 代替全部成本。
- 采样不可控：桥不暴露 `temperature`/`top_p`/`seed`，**不声称确定性**，不给出生产成功率。
- 真实模型延迟波动大（本轮实测出卷 worker 15–43 秒），因此"单次调用内观测到终态"不是稳定性质；工具如实返回 `running` 并由模型继续有界等待。
- 未执行真实校园账号/WebVPN/学在浙大私有材料验证；合成流程通过不等于真实校园平台接入成功。
- 隔离：所有真实模型运行使用独立 UUID 工作区；只把 DPAPI 密文的**副本**放进每轮 secrets 目录（用后删除），未解密、未归档 `var/secrets`。

---

## 7. 复跑命令

```powershell
# 1) 独立验收的返修复现命令（返修前 4 failed，现应 4 passed）
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py docs/audit/harness-independent/test_acceptance_boundaries.py docs/audit/harness-independent/test_review_boundaries.py -q -p no:cacheprovider

# 2) 常规回归
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py apps/api/tests -q -p no:cacheprovider
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py tests connectors/tests -q -p no:cacheprovider
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/verify_agent_runtime.py
node apps/agent-runtime/verify-transition-recovery.mjs
node apps/web/node_modules/typescript/bin/tsc -b apps/web
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/export_agent_contract.py --check
cd apps/agent-runtime; npm run test:audit

# 3) 确定性 12 类（真实 API + Harness + 受控模型，不调用付费模型）
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/verify_next_round_e2e.py --out docs/audit/harness-next-round/rework-deterministic.json

# 4) judge v4 复核已存证据（零模型调用）与自测
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/verify_next_round_live.py --rejudge --out docs/audit/harness-next-round/live-model-rejudged-v4.json
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/verify_next_round_live.py --self-test --self-test-out docs/audit/harness-next-round/rework-judge-v4-selftest.json

# 5) 真实模型 3×3（付费；固定预算，需显式 --live）
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/verify_next_round_live.py --live --tasks t1 t2 t3 --runs 3 --label <label> --out docs/audit/harness-next-round/<new>.json
```

回滚方式（如需撤销本轮）：删除 §4 新增文件、恢复上表文件到本轮前版本、执行 `alembic downgrade d02b61c738af`（仅在需要移除两张回执表时）。本轮**未**改动 `docs/audit/harness-independent/` 下的既有证据与判定，也未改写任何旧证据 JSON。

---

**结论归属**：本文件不声明"已通过最终验收"。F1–F5 的行为修复、故障对应测试、当前源码哈希、原始轨迹与可下载产物均在以上路径可独立复核；是否通过由独立验收方判定。
