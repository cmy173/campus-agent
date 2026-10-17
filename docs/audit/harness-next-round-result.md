# 下一轮 Harness 开发结果（M0—M3）

> 本文件是**实施方交付说明**，不是验收结论。最终由原 Codex 对话独立验收：重读工作区差异、独立运行回归、下载并打开产物、核对原始轨迹。文中所有“通过”均指对应命令/测试在本机实际执行的结果，并附证据文件路径。

- 工作目录：`D:\AI\campus-agent`
- 时间：2026-10-15（Asia/Shanghai）
- 进入基线：工作区**已有大量未提交修改**（`git status --porcelain` 161 项）。本轮**未** `reset`/`clean`/`stash`/整目录覆盖/自动 commit/push，未写入 `var/personal.db`、个人校园缓存、凭据目录或普通浏览器 profile。
- 机器证据目录：[`docs/audit/harness-next-round/`](harness-next-round/)
- 本轮任务书：[`harness-next-round-task.md`](../agent-design/harness-next-round-task.md)；验收合同：[`harness-next-round-acceptance.md`](harness-next-round-acceptance.md)

---

## 0. 执行环境与实际拥有的权限

| 能力 | 实际状态 |
|---|---|
| 仓库文件读取 | 可用（`read`/`glob`/`grep`/git 只读） |
| 任务范围内源码写入 | 可用（本机开发终端，非产品校园助手 profile） |
| 受控运行 Python | 可用：`apps/api/.venv/Scripts/python.exe`（CPython 3.12.10，reportlab 5.0.1 / openpyxl 3.1.5 / pypdf 可用） |
| 受控运行 Node/npm | 可用：Node v24.19.0，npm 11.17.0 |
| 编辑提示词/文档 | 可用（已更新 skill 与 persona，但**不只**改提示词） |
| 任意 Shell（产品侧） | **仍然禁用**：`pwsh/bash/shell/powershell/workflow/run_code/ralph/PTC` 不在模型可见工具内，由能力策略在执行前拒绝。本轮**没有**为了导出能力重新开放任意 Shell，`tools/verify_agent_runtime.py` 中“被拒工具族必须不可见”的断言继续保留并通过。 |
| 真人校园认证 | 本轮未执行（无账号条件）；不伪造在线态 |

未遇到“缺少开发权限”的阻塞：本轮没有提交 `blocked-environment.md`。

### 执行前基线（M0 冻结）

回归入口使用项目既有隔离脚本 `tools/audit_regression.py`（每次运行创建独立 UUID 临时工作区，日志首行 `ISOLATED_WORKSPACE=...`）。

| 命令 | exit | 结果 | 耗时 |
|---|---|---|---|
| `apps/api/.venv/Scripts/python.exe tools/audit_regression.py apps/api/tests -q -p no:cacheprovider` | 0 | **616 passed, 5 skipped** | 171.58s |
| `apps/api/.venv/Scripts/python.exe tools/audit_regression.py tests connectors/tests -q -p no:cacheprovider` | 0 | **594 passed, 87 subtests passed** | 47.93s |
| `node apps/agent-runtime` → `npm run test:audit` | 0 | **85 tests / 85 pass / 0 fail** | 2.47s |
| `apps/api/.venv/Scripts/python.exe tools/verify_agent_runtime.py` | 0 | `{"ok": true, "model_calls": 83}`（受控模型，非真实付费模型） | 见 `var/next-round-baseline-runtime.log` |
| `node apps/web/node_modules/typescript/bin/tsc -b apps/web` | 0 | 通过 | — |
| `apps/api/.venv/Scripts/python.exe tools/export_agent_contract.py --check` | 0 | `Contract is current: 51 Agent paths and 54 schemas` | — |

历史参考值（2026-10-15 早先记录）为 API 616 passed/5 skipped、Node 85 passed；本轮**实测复现**了同样的数字，说明进入基线未退化。基线原始日志：`var/next-round-baseline-api.log`、`var/next-round-baseline-connectors.log`、`var/next-round-baseline-node.log`、`var/next-round-baseline-runtime.log`。

M0 要求保留的既有语义（逐条复核，均仍在并通过回归）：

1. sync job 在返回前 commit，独立 worker 与下一 HTTP 请求可见；import 后仅该会话获得服务端验证的新课程 scope，其他会话不扩权。
2. 分页游标/partial/空结果/跨平台失败隔离；不把当前页当全量。
3. 窄 scope 不能同步/复用/读取/取消宽范围 job。
4. 后台 `failed`/`partial_failed` 不返回虚假业务成功。
5. 浏览器长操作不阻塞无关本地工具，停止仍等待原调用收尾。
6. 自定义模型桥的限流/超时可进入官方有限重试；认证错误不重试。
7. 匿名网页地址防护保留；精确已登记的教务只读通知路径不误判提交。

> 说明：进入基线时 `apps/api/tests` 有 3 项**工具清单断言**会因本轮新增 5 个工具而失败（见 §5）。这是清单同步，不是删除断言或放宽行为：新增工具仍在“严格 schema + 服务端 scope”范围内，被禁工具族的不可见断言保持不变。修复前该 3 项确实失败，修复后完整 API 回归通过。

---

## 1. M1：恢复可信文档导出能力

### 1.1 交付内容

| 位置 | 作用 |
|---|---|
| `apps/api/app/agent_bridge/artifacts.py`（新增） | 严格输入契约 `ExportArgs`、固定渲染器装载、受限 staging、会话文件发布、幂等回执 |
| `apps/api/app/agent_bridge/tools.py` | 注册 `artifacts.export`（`model_name=campus_artifacts_export`，`effects=write`）；`ToolContext.files` 会话文件store注入点 |
| `apps/api/app/models.py` | `AgentArtifactReceipt`（同一可信 call ID 重放回执） |
| `apps/api/migrations/versions/a7c3e5b91d24_agent_receipts.py` | 建表迁移（down_revision `d02b61c738af`，单 head） |
| `apps/api/app/agent_bridge/files.py` | 补 `.pdf/.xlsx/.docx/.md` MIME 映射，避免真实交付物被当成未知二进制 |
| `apps/agent-runtime/skills/document-artifacts/SKILL.md` | 从“只能文本”改为说明 `artifacts.export` 的可用输入与禁止项 |
| `apps/agent-runtime/prepare-profile.mjs` | persona 同步：需要 PDF/XLSX 时用 `artifacts.export`，不再声称“不能生成” |
| `apps/api/tests/test_artifacts_export.py`（新增，25 项） | A01–A05 确定性验收 |

### 1.2 输入契约（对验收第 1—2 条）

`artifacts.export` 只接受**数据**，`extra="forbid"` 且严格类型：

- `title`（1–200，必填）、`subtitle`、`file_stem`（仅文件名，禁止目录/保留字符）——注意：`file_stem` 是**用户可见文件名**，不是输出目录；输出目录永远由服务端决定。
- `formats`：`pdf|xlsx|md` 任意组合，去重且有上限。
- `sections[]`：`heading`、`paragraphs[]`、`bullets[]`、`table{columns,rows}`；每行单元格数必须等于列数；`section` 不能为空。
- `sheets[]`：`name`（1–31、唯一、不含 Excel 禁用字符）、`columns[]`、`rows[]`。
- 单元格类型：`str | int | float | bool | null | {"date":"YYYY-MM-DD"}`。**不接受** `{"formula": "=..."}` 形状。
- `anchors[]`：`source/resource_id/version/locator/chunk_id/note`，至少一项，渲染进“来源”。
- 服务端上限：`rows ≤ 500`、`columns ≤ 30`、`sheets ≤ 10`、`sections ≤ 40`、序列化后 `≤ 512 KiB`、渲染 `30s` 超时。

明确**不接受**：Python/JS/Shell 代码、命令、表达式、任意模块名、可执行程序、任意输出目录、字体路径、远端 URL。这些参数名一旦出现即 `INVALID_INPUT`（`extra_forbidden`），在危险 IO 之前拒绝。

### 1.3 执行方式（对验收第 3—4 条）

渲染是**固定可信模块调用**，不是子进程、不是通用代码执行器：`artifacts.load_renderer()` 以固定路径 `apps/agent-runtime/skills/document-artifacts/scripts/export_documents.py` 用 `importlib` 装载并调用其 `export()`。模型无法选择解释器、程序、输出目录或字体。

- 输出先写入服务端拥有的 staging：`<journal.sqlite 所在目录>/artifact-work/<uuid>`，渲染全部格式成功后才发布。
- 发布走既有会话文件存储 `ConversationFiles.save`，身份/根目录由服务端确定；`ConversationFiles` 原有的 symlink/重解析点检查（`_no_links`）与 `conversation_files` 会话归属检查继续生效，另在 staging 链路上做同样检查。
- 失败清理仅限本次 staging 目录；不删除任何既有文件。
- 幂等：`request_key = turn_id:call_id`（服务端构造，不接受模型提供）。同一 key 不同内容 → `IDEMPOTENCY_CONFLICT`；同一 key 相同内容 → 返回同一持久文件回执，不重复创建文件（`agent_artifact_receipts` 持久化，跨会话/跨请求可见）。

### 1.4 渲染语义（对验收第 5—8 条）

- PDF：中文字体嵌入（Windows `msyh.ttc` / DejaVu 回退），`wordWrap='CJK'`，跨页表格用 `LongTable(repeatRows=1, splitByRow=1, splitInRow=1)`，生成后用 `PdfReader` 复读正文，正文为空即失败。列数 >5 自动横向 A4。
- XLSX：数字为数值、`{"date":...}` 为日期（`number_format='yyyy-mm-dd'`）、布尔为布尔、`null` 为空。以 `=` 开头的字符串**保持文本**（`cell.data_type == 's'`），不被自动转成公式。
- 不接收任意公式/外链/DDE；最小版本只支持文字、数字、日期、布尔，因此**不声称公式已计算**。
- 未知事实保持未知：渲染器不补日期，不推断缺失字段；工具结果显式带 `unknown` 说明与 `anchors`。
- 模型看到成功前，产物必须已存在且可从本会话下载：`ARTIFACT_RENDER_FAILED`（渲染失败）、`ARTIFACT_TIMEOUT`（超时）、`ARTIFACT_PUBLISH_FAILED`（**已渲染未交付**）、`ARTIFACT_RECEIPT_FAILED`（已交付但回执未存，明确告知勿重复）、`DEPENDENCY_UNAVAILABLE`（渲染器缺失）。全部为结构化错误，绝不以后缀伪造 PDF/XLSX。
- 权限策略**未开放任意 Shell**；`document-artifacts` skill 与 persona 已同步更新。

### 1.5 闭环 A 实测

真实链路：**会话内合成资料 → `read` → 模型/调用方构造结构化内容 → `artifacts.export` → 发布到会话文件区 → HTTP 下载 → PDF/XLSX 解析与内容核验**。确定性部分由 `test_artifacts_export.py` 覆盖并通过；真实模型部分见 §3。

---

## 2. M2：接通复习卷完整业务（不复制原引擎）

### 2.1 交付内容

| 位置 | 作用 |
|---|---|
| `apps/api/app/review_service.py`（新增） | 从 router **逐字提取**的受理链：引擎/题库依赖、provider 校验、材料选择与可用性、限额、指纹、冻结快照、`review_generate` 入队 |
| `apps/api/app/routers/review_papers.py` | 改为委托 `review_service`（保留原函数名兼容既有调用）；HTTP 契约与行为不变，**未整体搬迁 router** |
| `apps/api/app/agent_bridge/review_actions.py`（新增） | Agent 适配：范围校验的课程/材料/provider 解析、幂等回执、读卷归属、导出复用 `review_export` |
| `apps/api/app/agent_bridge/tools.py` | 注册 `reviews.preflight`（read）、`reviews.generate`（write）、`reviews.read`（read）、`reviews.export`（write）；`jobs.read` 归属改为 `_job_owned` |
| `apps/api/app/agent_bridge/job_actions.py` | `owns()` 认 `AgentReviewReceipt`，取消/读取都要求本会话回执 |
| `apps/api/app/models.py` | `AgentReviewReceipt`（幂等 + job/paper 归属） |
| `apps/api/tests/test_reviews_agent.py`（新增，19 项） | B01–B04 确定性验收 + 有界等待/envelope 真值回归 |

### 2.2 逐条对应验收要求

1. **复用原引擎**：材料选择、预检、冻结快照、版本、题目配额/约束、已提交作业排除、费用配置全部走原 `review` / `review_engine` / `review_service`。tool 文件中**没有**第二套出卷引擎。
2. **只能选本会话授权且当前可用的课程/材料版本**：`review_actions.course()` 先做 scope 校验；`normalize_materials()` 对每个 resource/assignment 校验存在性与 `course_id`，越界即 `SCOPE_DENIED`。模型不能指定 owner；`provider_config_id` 只能引用已保存配置，且服务端要求 `enabled` 且有密钥；**不接受** provider 地址或密钥。
3. **预检返回缺失资料与可继续条件**：`can_generate`、`materials.items`、`materials.missing`、`limits`、`bank`、`fingerprint`；只返回元数据（显式剥离正文 `text`），不发正文、不建快照、不入队。元数据/未解析资料不会假装读过：选到 unparsed 资源时 `can_generate=false` 且 `missing` 列明原因。旧预检/过期版本/删除材料通过 `If-Match` 指纹拒绝（`PREFLIGHT_FAILED`）或版本校验拒绝（`STALE_VERSION`）。
4. **生成返回持久 job ID**：受理即返回 `job_id` 与 `paper_id`，并**同一事务**写入快照行 + job + 归属回执（`db.commit()`），因此请求结束、重复 call、服务重启后仍可读。`jobs.read` 现在能读到本会话发起的 review job；归属由回执证明，**不接受“知道 job_id 就能读”**（其他会话 `SCOPE_DENIED`）。
5. **状态分别返回**：受理 `queued`；`jobs.read` 区分 `queued/running/succeeded/failed/partial_failed/cancelled`，终态失败返回 `error`、部分失败返回 `partial` 并保留原因；取消落 `cancelled` 且不落修订（不留假完成）。同一可信调用重放返回同一 job，**不重复扣费**；参数变化 `IDEMPOTENCY_CONFLICT`。
6. **沿用服务端 provider**：生成走原 `provider_config_id` + `secrets_store`，密钥只存在于服务端；`provider_snapshot` 漂移（base_url/model/protocol 变化）由原 worker 以 `PROVIDER_CHANGED` 拒绝。
7. **读卷/导出验证归属**：`reviews.read`/`reviews.export` 均先 `owns_paper()`，再按 `paper_id/revision/snapshot` 取版本；快照缺失即 `INTERNAL_ERROR`，版本不存在即 `NOT_FOUND`。最小版本交付**原有可靠 Markdown 导出**（复用 `review_export.RENDERERS`），并返回冻结快照 `fingerprint` 与题目/答案版本。PDF 由 M1 承担：`reviews.export` 产出正文/题目证据，必要时再用 `artifacts.export` 生成 PDF 并保留锚点。
8. **不需要逐步点 UI**：用户明确要求出卷后，读取 → 预检 → 已授权生成 → 轮询 → 读卷 → 导出全部由工具链完成；只有真正需要登录/新增资料授权/缺失必要信息才进入 `needs_user`（本轮未引入新的确认弹窗）。
9. **router 未整体搬迁**：仅把受理链提取为应用服务，其余 REST 与工作台页面兼容（`test_review_papers_api.py` 21 项继续通过）。

### 2.3 闭环 B 实测

真实链路：**指定已授权课程 → 发现/读取真实合成材料 → `preflight` → `generate`（冻结版本 + 持久入队）→ 真实 worker 完成 → `jobs.read` → `reviews.read` 指定版本 → `reviews.export` 发布下载 → 验证文件内容与冻结材料一致**。确定性部分由 `test_reviews_agent.py` 覆盖；真实模型部分见 §3。

原引擎在某 mode 缺题库/模型/依赖时保留真实标准错误：`DEPENDENCY_UNAVAILABLE`（引擎/题库模块缺失）、`BANK_NOT_CONFIGURED`（无可用题库来源）、`PROVIDER_UNAVAILABLE`（无可用已配置模型）。未伪造生成结果。

### 2.4 真实模型 T2 暴露的缺口与修复（有界等待）

首轮真实模型 9 次会话中 **T2 三项全部未达标（0/3）**，其余 T1 3/3、T3 3/3。原始记录完整保留在 `live-model.json`，未覆盖。

**根因（用原始轨迹与 worker 日志定位，不是模型能力问题）**：`reviews.generate` 只入队即返回（语义正确，不应阻塞），但模型没有**有界**方式观察独立 worker。每次 `jobs.read` 往返约 2 秒，模型把 12 次模型请求预算全部消耗在轮询 `running` 上。worker 日志证明任务是真实完成的：

```text
t2-2  job_f8d8c69d(review_generate) 开始 → 15.9s 后 “出卷完成：paper=... revision=1 题目 3 告警 2 报告 ok” → succeeded
t2-3  job_9f23ff0e(review_generate) 开始 → 15.6s 后 succeeded
t2-1  job_781e5b8b 仍在 running（turn 结束时未完成）
```

即“业务确实完成，但模型已耗尽预算、来不及读卷与导出”。

**修复（产品代码，放宽无关）**：

| 改动 | 语义 |
|---|---|
| `reviews.generate` 新增 `wait_seconds`（默认 0，上限 6） | 在**本次调用内**有界等待真实 job 行进入终态；上限远低于域工具 20s 超时。超时仍返回 `running` 并提示 `jobs.read`，**不把受理当成功** |
| 响应新增 `observed_within_call` | 明确区分“本次调用内已观察终态”与“仍在运行” |
| `wait_seconds` 从工具重放缓存键与出卷回执身份中排除 | 不同等待窗口的重放仍是**同一个已受理 job**，不会重复生成扣费任务 |
| 等待实现 | 每 0.4s `expire_all()` 后重读**已提交的 job 行**，不读内存缓存；仅本次调用内有效 |

确定性测试：`test_reviews_agent.py` 18 项全绿，其中新增 3 项针对该修复（观察真实终态、同一 turn 内重放不重复建 job、窗口内未完成不假成功）。修复后 T2 的**复测**与首轮记录分开报告（见 §3.3）。


---

## 3. M3：可复跑的验收证据

> 详细明细见 [`docs/audit/harness-next-round/`](harness-next-round/)。本节汇总四类必须分开报告的证据。

### 3.1 分类报告（四类分开统计，互不替代）

| 类别 | 入口 | 是否经过模型 | 合同范围 | 本轮结果 |
|---|---|---|---|---|
| ① 直接工具/业务层测试 | `apps/api/tests/test_artifacts_export.py`（25 项）、`apps/api/tests/test_reviews_agent.py`（19 项） | **否** | A01–A05、B01–B04 的工具层行为 | **44 项全部通过**；全量 API 回归 660 passed / 5 skipped |
| ② 真实 FastAPI + 真实 Harness + 受控模型 + 合成业务数据 | `tools/verify_next_round_e2e.py`（12 类确定性场景） | 受控模型（本地 synth 端点，`paid_model_called: false`） | A01–A05、B01–B04、E01–E03 | **12/12 covered 且 passed，exit_code 0**（`deterministic.json`） |
| ③ 真实配置模型 + 合成课程/附件 + 实际工具执行 | `tools/verify_next_round_live.py --live` | **是**（已保存 provider `prv_c68d2878` / deepseek-flash） | 3 项任务 × 3 次独立会话 = 9 次 | 首轮 **6/9**（T1 3/3、T2 0/3、T3 3/3，门槛未满足）；T2 修复后单列复测 **3/3**。**不满足“9 次首轮 ≥8”**，如实保留 |
| ④ 真人校园授权验证 | — | — | 真实学在/智云/CAS/教务/ETA/WebVPN | **未执行**（无账号条件），明确记为未验收 |

**关于验收门槛的诚实结论**：真实模型 9 次首轮为 **6/9**，**未达到**“≥8 且每项 ≥2/3”。其中 T2 的 0/3 已定位为产品缺口（§2.4）并修复，修复后 T2 复测 3/3；但按要求**首轮原始记录不覆盖、复测单独统计**，因此实施方**不声称真实模型测试整体通过**，交由验收方按“首次 9 次”与“复测”分开评判。

### 3.2 工具 Schema 与端到端调用轨迹

- 现行 32 个领域工具的 `name/model_name/effects/字段/additionalProperties` 快照：`docs/audit/harness-next-round/next-round-tool-inventory.json`。
- 源码哈希（**修复后**工作区，另附 `head`）：`docs/audit/harness-next-round/next-round-source-hashes.json`。
- 确定性 12 类场景与 9 次真实模型的逐场景 trace（含 call ID、工具结果、job id、文件 id、usage、耗时）：`deterministic.json`、`live-model*.json`、`traces-live/<run_id>.json`。

### 3.3 真实模型 9 次任务（分类③）明细

**必须分开报告，不得用复测替换首轮。** 全部集合均为 append-only，前一组字节不变。

| 证据集 | 文件 | 标签 | 结果 |
|---|---|---|---|
| 首轮 9 次 | `live-model.json` + `live-model-summary.md` | first_9runs | **6/9**：T1 3/3，**T2 0/3**，T3 3/3；门槛未满足 |
| 修复前复测 | `live-model-retests.json` | `t2_retest_before_fix` | 0/2 |
| 修复①后 | `live-model-t2-retest.json` | `t2_retest_after_fix` | 0/3 |
| 修复②后（6s 窗口） | `live-model-t2-final.json` | `t2_final_after_bounded_wait_fix` | 1/3 |
| 修复③后（14s 窗口） | `live-model-t2-final2.json` | `t2_after_wait_window_14s` | 2/3 |
| 修复④后（终态） | `live-model-t2-final3.json` + 摘要 | `t2_after_envelope_fix` | **3/3** |

首轮逐次（judge v2，金额 unknown）：

| run | 任务 | 达标 | 模型调用 | 工具调用 | 秒 | 备注 |
|---|---|---|---|---|---|---|
| t1-1-5754bc6e | t1 | 是 | 3 | 4 | 18.1 | PDF+XLSX，原件被恢复式记录（见下） |
| t1-2-63c81253 | t1 | 是 | 3 | 3 | 32.1 | PDF+XLSX |
| t1-3-3c2b6182 | t1 | 是 | 4 | 5 | 38.5 | PDF+XLSX，同名不同内容两个文件均保留 |
| t2-1-03525889 | t2 | 否 | 12 | 13 | 26.3 | 预算耗尽在轮询；worker 后成功 |
| t2-2-a201a8c6 | t2 | 否 | 12 | 14 | 31.3 | 同上 |
| t2-3-e0226e62 | t2 | 否 | 12 | 15 | 24.0 | 同上 |
| t3-1-1b67a153 | t3 | 是 | 5 | 8 | 25.9 | 诚实报缺口 |
| t3-2-da649e62 | t3 | 是 | 5 | 9 | 24.2 | 诚实报缺口 |
| t3-3-b4024110 | t3 | 是 | 6 | 7 | 24.7 | 诚实报缺口 |

自动失败项（凭据泄露／越权读取／未授权外部提交／假文件）：**9 次全部为 none**；人工介入 0。

**最终 T2 复测（`t2_after_envelope_fix`，终态代码，3/3）**：

| run | 达标 | 模型/工具调用 | 秒 | wait_seconds | jobs.read 观察到的 envelope | job 终态 | 导出文件 |
|---|---|---|---|---|---|---|---|
| t2-1-ca7d4b41 | 是 7/7 | 9/9 | 44.8 | [14] | ok/succeeded | succeeded | `file_21622abe…` 3022B sha256 `88a41704…` |
| t2-2-00bc3c8c | 是 7/7 | 12/16 | 52.0 | [14] | **running×3** → ok/succeeded | succeeded | `file_977f4a6f…` 3188B sha256 `fe5737d0…` |
| t2-3-2e4c0ed9 | 是 7/7 | 9/9 | 41.7 | [14] | ok/succeeded | succeeded | `file_44c53c4c…` 4464B sha256 `abe9c2c4…` |

三次均为真实 deepseek-flash、真实 worker、真实 `learning.review`；试卷 v1、3 题、快照指纹与冻结片段 `chunk_syn_00/01/02` 一致；回答如实报告引擎告警（无作业材料、一个知识点未覆盖）而非编造覆盖率。t2-2 是本轮 `jobs.read` 终态修复的直接证据：三次在途读取返回 `running`（修复前被压成 `queued`），且本轮 `load_tools/job_output/job_list` 原生工具绕行 **0 次**（此前每轮 1–4 次）。

**usage 与费用**：首轮 9 次主 Agent 报告 tokens = input 51,515 / output 21,633 / cache-read 1,069,824 / 合计 **1,142,972**；最终 T2 复测 3 次合计 **572,813**。搜索链路 0 次；出卷 worker 的模型调用计入业务链路并单独记录（无逐次计数器，次数 unknown）。金额一律 `unknown`（无账单依据）。**未控制因素**：该桥不暴露 temperature/top_p/seed，采样不可控，因此**不声称确定性**。

**诚实披露（失败与自身缺陷均保留）**：
1. 首次 T1 会话真实产出两个文件，但写记录时因 openpyxl 日期单元格不可 JSON 序列化而失败。修复 writer 并新增 `--recover-run`，从保留的 harness JSONL／API 事件／journal／已交付文件**重建第一条记录**（标记 `origin: recovered-from-persisted-evidence`），**没有**用复测替换首轮。
2. judge 曾有两处度量缺陷，均通过**重放既有证据**修正，未重跑模型：T3“编造完成”用了裸子串匹配，误伤条件句“生成完成后…我会交付”（t3-2 更正为达标）；T2 曾按模型侧名查 envelope 而 API 存领域名（preflight/generate 被误报失败）。修正前的判定保留在 `goal.previous_verdicts` 与 `judge_changelog`。
3. 真实模型的采样参数不可控，已在每个记录中标注为未控制因素。

### 3.4 确定性 12 类场景（分类②）

`tools/verify_next_round_e2e.py`，真实 FastAPI + 真实 Harness `runner.mjs` + 受控（本地 synth）模型 + 合成业务数据，真实 worker 执行与真实下载解析。最终记录：run id `fda2ec41c74646a0ad91fa4a4db1c667`，2026-10-15T18:53:48+08:00，193.1s，**exit_code 0**，`overall = {classes_total 12, passed 12, failed 0, not_run 0, scenario_failures [], bookkeeping_failures [], verdict all_covered_classes_passed}`，`paid_model_called: false`，`source_hashes == source_hashes_post_run`。

| 类别 | 状态 | 秒 | 检查数 | 关键断言 |
|---|---|---|---|---|
| A01 | passed | 2.88 | 6 | 真实 PDF（1.68MB 原始字节流可解析）、中文可读、跨页表格 000..119 行完整、字形均在页面内、12pt 页边、来源保留 |
| A02 | passed | 1.77 | 6 | XLSX 数值/日期/布尔/空值/`=`开头文本类型正确，**0 个公式单元格** |
| A03 | passed | 1.00 | 3 | 同 call ID 重放返回同一文件回执；改内容冲突且无重复产物 |
| A04 | passed | 2.47 | 7 | 代码/URL/路径/其他会话/超限输入在危险 IO 前拒绝 |
| A05 | passed | 11.83 | 5 | 渲染失败/超时/发布失败均为结构化错误且无假成果、不删既有文件 |
| B01 | passed | 4.81 | 4 | 预检缺口如实报告；材料变更使旧预检失效 |
| B02 | passed | 3.75 | 6 | 受理持久入队；独立 worker 完成；按真实终态读卷与导出 |
| B03 | passed | 8.77 | 7 | 同调用重放不重复建 job；其他会话读不到 job/卷 |
| B04 | passed | 18.61 | 5 | 模型/题库不可用、部分失败、失败、取消状态准确 |
| E01 | passed | 17.75 | 8 | 受控模型 read→export→publish→download 真实顺序与 ID 关联 |
| E02 | passed | 25.28 | 11 | 课程→材料→preflight→generate→poll→read→export 全链 |
| E03 | passed | 89.36 | 6 | 隔离回归既有 sync/scope/分页/锁/重试；skip 集合与宿主基线一致、未改既有测试、`var/personal.db`+`var/secrets` 等个人数据根指纹未变 |

E02 关键证据（含此前唯一不通过项的最终去向）：

- `the agent observed the queued job before any worker claimed it` → **passed**：runner 现在持有 worker 直到 bridge **已记账**一条本轮的 `jobs.read` 且状态为 `queued`（本轮 2 次 queued 轮询），随后放行 worker，同一 job 真实到达 `succeeded`。
- `a real worker completed the accepted job` → `job_4d087774: status=succeeded, progress=1.0, refs=[review_paper pap_e40a4d67]`。
- `the generated paper is traceable to the frozen snapshot` → `snapshot snp_f965afa8 fingerprint=sha256:41c95d9ef... items=[resource_text res_nr_ready v1 chunks chk_nr_1/chk_nr_2]`。
- `review markdown carries the frozen snapshot fingerprint in the file itself` → 指纹与快照 id **字面出现**在 895 字符的已交付 markdown（`材料快照`/`材料指纹`/`出卷引擎` 行），文件自证可审计。

产物副本（HTTP 会话文件 API 下载后解析，`docs/audit/harness-next-round/artifacts/`）：

| 文件 | 字节 | sha256 |
|---|---|---|
| A01-synthetic-report.pdf | 46196 | `1682406a7403db5e1674dd90ef946c032b21b78e4eab707792eaca98bcf01149` |
| A01-synthetic-report.md | 4413 | `c3e0e9797aa818b882696e81b7b9892a637514d67e0d1a8d40a3c78aeb3ccd02` |
| A02-synthetic-materials.xlsx | 5632 | `bd3200e7e2525a676f367fb3d6c31e567d4dabe2b9896037ff8754473aa7c694` |
| E01-harness-report.pdf | 46203 | `e020712a3f62213d1a89c507a00c90a02414036dd64716ea1a6f984004856f19` |
| E01-harness-materials.xlsx | 5632 | `64bcd97c5daeebb17153410048cbf40db41e53ae39b8500f2460f05f4a86e324` |
| E02-review-paper.md | 1715 | `e6d9fb3316aa755ba255adf55a0f4d598fd2210ca7c01c35517e66eb50d19527` |

`deterministic.json` 另含 261 条有界 trace（每次 HTTP/internal 调用、工具名/call id/status/error code、job id、文件 id），361KB，供验收方无需重跑即可核对链路。

### 3.5 本轮过程中发现并修复的问题（含检查自身缺陷）

所有发现均先复现、再修复、再补确定性回归，失败证据保留：

| # | 发现（证据） | 归属 | 修复 | 验证 |
|---|---|---|---|---|
| 1 | 真实模型 T2 首轮 0/3：模型把 12 次请求预算全花在轮询；worker 日志证明任务 15.9s/15.6s 后真实 succeeded | 产品缺口 | `reviews.generate` 增加有界 `wait_seconds` | `test_bounded_wait_*`，T2 复测 3/3 |
| 2 | `jobs.read` 把在途 job 的 envelope 状态压成 `queued`（`tools.py` 末段二次覆盖） | 产品缺陷 | 末段改为按观察到的 job 取 `running`/`queued`，域 envelope schema 增加 `running` | `test_running_job_is_not_collapsed_into_a_queued_envelope`；T2 复测中 3 次在途读取如实返回 `running` |
| 3 | 6s 等待窗口小于实测 14–16s 的生成耗时 | 产品参数 | 窗口上限 6→14s（仍远低于 20s 工具围栏，预算未变） | T2: 1/3 → 2/3 → 3/3 |
| 4 | `reviews.export` 的 markdown 不含快照 id/指纹，交付文件无法自证 | 产品缺口 | `review_export._meta_pairs` 增加 `材料快照`/`材料指纹`/`出卷引擎`（缺失则省略，不猜） | E02 与 `test_b02_export_*` 字面断言 |
| 5 | `reviews.generate` 既有描述写“不等待生成完成”，与新参数不一致 | 文案 | 更新工具描述并提示域 job 用 `jobs.read`、原生 `job_list/job_output` 不是校园 job | 清单快照 |
| 6 | 实施方 runner：E03 把既有宿主 symlink skip 当新 skip 判失败；源码哈希检查极性反向 | 证据工具 | 改为基线对比 + 分离 per-file 极性检查 + 显式区分 `scenario_failures`/`bookkeeping_failures` | 最终 `exit_code 0`，skip 项与基线一致 |
| 7 | 实施方 runner：E02 在模型决定轮询时就放行 worker，抢占早于记账 | 证据工具 | 持有 worker 直到 bridge 记账本轮 `queued` 答案，再放行并等待真实终态 | E02 连续 4 次独立 + 全量均通过 |
| 8 | 真实模型侧 judge 两处度量缺陷（T3 裸子串误判、T2 用模型侧名查 envelope） | 证据工具 | 重放既有证据修正（保留 `previous_verdicts`），**未重跑模型** | judge v2/v3 |



---

## 4. 本轮修改文件与复现／修复／回归／回滚

| 文件 | 改动 | 复现方式 | 回归 | 回滚 |
|---|---|---|---|---|
| `apps/api/app/agent_bridge/artifacts.py` | 新增：`artifacts.export` 契约+固定渲染器+发布+幂等 | `test_artifacts_export.py -k a01` | 完整 API 回归 | 删文件 + 摘除 SPECS 条目 + 回滚迁移 |
| `apps/api/app/agent_bridge/review_actions.py` | 新增：复习卷 Agent 适配 | `test_reviews_agent.py -k b01` | 完整 API 回归 | 删文件 + 摘除 4 个 SPECS 条目 |
| `apps/api/app/review_service.py` | 新增：受理链应用服务（从 router 逐字提取） | `test_review_papers_api.py` | 21 项原 REST 用例 | 恢复 router 内联实现 |
| `apps/api/app/routers/review_papers.py` | 改为委托 `review_service`，保留原名字 | 同上 | 同上 | 同上 |
| `apps/api/app/agent_bridge/tools.py` | 5 个新工具 + `ToolContext.files` + `_job_owned` + review 分派 | 各新测试 | 完整 API 回归 | 回滚 SPECS 与分派分支 |
| `apps/api/app/agent_bridge/job_actions.py` | `owns()`认 `AgentReviewReceipt` | `test_reviews_agent.py -k b03` | job/scope 用例 | 去掉该分支 |
| `apps/api/app/agent_bridge/files.py` | 交付物 MIME 映射 | `test_artifacts_export.py -k a01` | 文件/上传用例 | 去掉 `MIME_TYPES` |
| `apps/api/app/models.py` | 两张回执表模型 | `agent_artifact_receipts`/`agent_review_receipts` | `alembic upgrade head` | 见迁移 `downgrade()` |
| `apps/api/migrations/versions/a7c3e5b91d24_agent_receipts.py` | 迁移（单 head，接 `d02b61c738af`） | `alembic upgrade head` / `heads` | 迁移实测通过 | `alembic downgrade d02b61c738af` |
| `apps/api/tests/test_artifacts_export.py` | 新增 25 项 A01–A05 | 见 §5 命令 | — | 删文件 |
| `apps/api/tests/test_agent_tools.py`、`test_agent_gateway.py`、`test_agent_plan_actions.py` | **工具清单同步** 27→32、write 集合加 3 项 | 见 §5 命令 | 无行为放宽；被禁工具族不可见断言保留 | 改回 27 |
| `apps/api/tests/test_reviews_agent.py` | 新增 19 项 B01–B04 + 有界等待/envelope 真值回归 | 见 §5 命令 | — | 删文件 |
| `apps/api/app/review_export.py` | 导出 markdown/HTML 增加 `材料快照`/`材料指纹`/`出卷引擎` 元信息（缺失则省略，不猜） | `test_reviews_agent.py -k b02_export`、E02 | `test_review_papers_api.py` 21 项 | 恢复 `_meta_pairs` 原实现 |
| `apps/api/app/routers/agent.py` | 工具重放缓存键排除 `wait_seconds`（观测窗口不属于业务请求，重放不得重复建 job） | `test_bounded_wait_replays_within_one_turn_via_the_tool_cache` | 完整 API 回归 | 恢复原 `request` 键 |
| `apps/api/app/agent_bridge/review_actions.py` | 另含 `await_job()` 有界观测、`MAX_JOB_WAIT_SECONDS=14`、`request_key` 与回执身份排除 `wait_seconds` | `test_bounded_wait_*` | 完整 API 回归 | 去掉等待分支 |
| `apps/api/app/agent_bridge/tools.py` | 另含 `jobs.read` envelope 真值（running/queued 不再互压）、`reviews.generate` 描述更新与 `jobs.read` 描述区分原生工具 | `test_running_job_is_not_collapsed_into_a_queued_envelope` | 完整 API 回归 | 恢复原分支 |
| `apps/agent-runtime/campus-plugin.mjs` | 域 envelope schema 状态枚举增加 `running` | E02 / T2 复测 | `npm run test:audit` 85 项 | 去掉 `running` |
| `tools/verify_next_round_e2e.py`（实施方证据工具，非产品） | 12 类确定性 E2E + 产物下载解析 + trace + 隔离/哈希自检 | 见 §5 命令 8 | 自身 `exit_code 0` | 删文件 |
| `tools/verify_next_round_live.py`（实施方证据工具，非产品） | 真实模型 9 次 + 可复测入口（`--live` 门槛、append-only、`--recover-run`/`--rejudge`） | 见 §5 命令 9/9b | — | 删文件 |
| `tools/verify_agent_runtime.py` | 期望工具清单同步加 5 项 | `python tools/verify_agent_runtime.py` | `{"ok": true, "model_calls": 83}` | 去掉新增行 |
| `apps/agent-runtime/skills/document-artifacts/SKILL.md` | 从“只能文本”改为说明新导出能力 | 人工复核 | — | 恢复旧文 |
| `apps/agent-runtime/prepare-profile.mjs` | persona 同步新能力 | 人工复核 | — | 恢复旧 persona |

**未**修改：登录/认证、缓存语义、scope 模型、停止语义、TLS/SSRF/Origin/runtime token 检查、OS 代理/DNS/路由；**未**新增通用代码执行、**未**重写 Harness loop、**未**更换模型或升级上游依赖。

---

## 5. 完整回归命令、exit code、耗时与 skip 原因

全部在仓库根目录执行（`apps/agent-runtime` 那条除外）。

| # | 命令 | exit | 结果 | 耗时 |
|---|---|---|---|---|
| 1 | `apps/api/.venv/Scripts/python.exe tools/audit_regression.py apps/api/tests -q -p no:cacheprovider` | 0 | **660 passed, 5 skipped** | 233.45s |
| 2 | `apps/api/.venv/Scripts/python.exe tools/audit_regression.py tests connectors/tests -q -p no:cacheprovider` | 0 | **594 passed, 87 subtests passed** | 51.81s |
| 3 | `apps/api/.venv/Scripts/python.exe tools/verify_agent_runtime.py` | 0 | `{"ok": true, "model_calls": 83}`（受控模型） | 见 `var/next-round-FINAL-runtime.log` |
| 4 | `node apps/agent-runtime/verify-transition-recovery.mjs` | 0 | `ok:true`（限流 1 次重试后成功、认证错误只请求 1 次、`llm_retries: 1`、read 只执行一次） | — |
| 5 | `node apps/web/node_modules/typescript/bin/tsc -b apps/web` | 0 | 通过 | — |
| 6 | `apps/api/.venv/Scripts/python.exe tools/export_agent_contract.py --check` | 0 | `Contract is current: 51 Agent paths and 54 schemas` | — |
| 7 | `cd apps/agent-runtime; npm run test:audit` | 0 | **tests 85 / pass 85 / fail 0 / skipped 0** | — |
| 8 | `apps/api/.venv/Scripts/python.exe tools/verify_next_round_e2e.py --out docs/audit/harness-next-round/deterministic.json` | **0** | **12/12 covered classes passed**，193.1s，`overall.verdict=all_covered_classes_passed` | 193.1s |
| 9 | `apps/api/.venv/Scripts/python.exe tools/verify_next_round_live.py --live --out docs/audit/harness-next-round/live-model.json` | 0 | 首轮 9 次 **6/9**（门槛未满足，见 §3.3） | — |
| 9b | `… --tasks t2 --runs 3 --out …/live-model-t2-final3.json` | 0 | 终态代码 T2 **3/3** | — |

对基线：API 616→**660**（+44 = 本轮新增 `test_artifacts_export.py` 25 + `test_reviews_agent.py` 19），skipped 恒为 5；connectors 594（不变）；Node 85（不变）；runtime verifier 83 次受控模型调用（不变）。**没有**删除测试、放宽断言或新增 skip。

### 5.1 skip 原因（5 项，全部为宿主权限，非本轮引入）

| 位置 | 数量 | 原因 |
|---|---|---|
| `apps/api/tests/test_agent_data_management.py:246` | 3 | `Symlink creation unavailable on this test host: [WinError 1314]` —— 当前 Windows 宿主未授予创建符号链接权限（需开发者模式/提升权限），测试自行 `pytest.skip` 并给出原因；非断言放宽 |
| `apps/api/tests/test_agent_file_scope.py:127` | 2 | `Host cannot create symlinks: 1314` —— 同上 |

这两处 skip 在**进入基线时即存在**（基线 616 passed 时同为 5 skipped，最终 660 passed 时仍为 5 skipped），本轮未新增，`deterministic.json` 的 E03 也把该集合作为**记录基线**逐条比对（`Not in baseline: []`）。因此“symlink/路径穿越”类防护的**宿主级**创建场景在此环境不可执行；本轮新增的 staging 链接检查与 `_no_links` 复用由非 symlink 分支（普通路径 + 拒绝分支）覆盖，链接分支沿用既有测试语义，不做“已通过”的过度声明。

### 5.2 本轮为清单同步而调整的断言（非行为放宽）

| 测试 | 调整 |
|---|---|
| `apps/api/tests/test_agent_tools.py::test_inventory_is_exact_and_inputs_closed` | 27→32，并显式要求新 5 个工具在册；`additionalProperties is False` 断言保留 |
| `apps/api/tests/test_agent_gateway.py::test_internal_auth_scope_contract_and_idempotent_call` | schema 数量 27→32 |
| `apps/api/tests/test_agent_plan_actions.py::test_registered_tools_use_real_engine_and_do_not_accept_model_approval` | write-effects 集合加 `artifacts.export/reviews.generate/reviews.export`；“模型不能自批 plans.apply”断言保留 |
| `tools/verify_agent_runtime.py` | 期望工具清单同步加 5 项；“被禁工具族（shell/workflow/MCP 资源/页面脚本）必须不出现在线上”的断言保留并通过 |
| `apps/agent-runtime/campus-plugin.mjs` | 域 envelope 状态枚举增加 `running`（如实表达在途状态）；被禁工具族的策略未改动 |

未新增 skip，未删除任何断言，未提高预算：模型 12 请求／工具 40 次／150 秒的上限全程未变（9 次真实任务与全部复测均按该上限执行），页面/工具预算默认值亦未调整。

---

## 6. 仍缺的权限、服务、账号与未完成能力

- **真人校园认证未做（分类④未执行）**：学在浙大/智云/CAS/教务/ETA/WebVPN 私有读取仍为“实现存在、自动测试通过、本人新联调未做”。本轮合成闭环与受控/真实模型闭环都不能替代真人授权验收，因此该项明确记为**未执行**，不冒充已完成。
- **WebVPN 隧道**：仍只验证门户可达与入口映射，不承诺认证后业务可用。
- **一般资料导入/解析为课程材料、长期偏好修改、全库 JSON/ICS 导出**：仍无领域工具，按任务书列后续待办，本轮未迁移。
- **自定义模型桥 usage/contextWindow**：未改造（非本轮阻塞项）；该桥不暴露 temperature/top_p/seed，故采样为**未控制因素**，本轮不声称确定性。
- **出卷 worker 的单次模型调用计数**：worker 直接调用已配置 provider，没有逐次计数器，因此业务链路调用次数记为 `unknown`（有依据说明），金额亦为 `unknown`。
- **浏览器页面脚本与事件副作用**：文件/提交 guard 不是 OS 级多租户沙箱；未承诺绝对隔离。
- **E02 等待窗口的边界**：`wait_seconds` 上限 14s 仍略低于实测 15–18s 的生成耗时，所以 `observed_within_call` 常为 false；实际仍只需 1–4 次 `jobs.read` 即在预算内完成，但**更保守的结论是**：该窗口是“显著减少轮询”，不是“保证单次调用内完成”。未为掩盖该边界而提高 12 请求上限。
- **未跑项**：无 `not_run` 的 12 类确定性场景（12/12 covered 且 passed）；真实模型首轮 9 次中 T2 曾 0/3，已如实保留并单列复测。

## 7. 交付索引

| 内容 | 路径 |
|---|---|
| 本结果说明 | `docs/audit/harness-next-round-result.md` |
| 机器证据目录 | `docs/audit/harness-next-round/` |
| 工具清单快照（32 项，含 schema 字段） | `docs/audit/harness-next-round/next-round-tool-inventory.json` |
| 源码哈希（修复后工作区 + head） | `docs/audit/harness-next-round/next-round-source-hashes.json` |
| 12 类确定性 E2E 证据 + trace + 产物哈希 | `docs/audit/harness-next-round/deterministic.json` |
| 12 类产物副本 | `docs/audit/harness-next-round/artifacts/` |
| 真实模型首轮 9 次 | `docs/audit/harness-next-round/live-model.json`、`live-model-summary.md` |
| 真实模型 T2 各轮复测（含失败轮） | `live-model-retests.json`、`live-model-t2-retest.json`、`live-model-t2-final.json`、`live-model-t2-final2.json`、`live-model-t2-final3.json`（+ 各自 summary） |
| 真实模型原始轨迹与产物 | `docs/audit/harness-next-round/traces-live/`、`artifacts-live/` |
| 复跑入口 | `tools/verify_next_round_e2e.py`、`tools/verify_next_round_live.py` |

### 复跑命令（供验收方独立执行）

```powershell
# 12 类确定性 E2E（真实 API + Harness + 受控模型 + 真实 worker）
apps/api/.venv/Scripts/python.exe tools/verify_next_round_e2e.py --out docs/audit/harness-next-round/deterministic.json

# 真实模型 9 次（需要已保存并启用的 provider；不加 --live 会拒绝且不调用模型）
apps/api/.venv/Scripts/python.exe tools/verify_next_round_live.py --live --out docs/audit/harness-next-round/live-model.json
apps/api/.venv/Scripts/python.exe tools/verify_next_round_live.py --live --tasks t2 --runs 3 --out docs/audit/harness-next-round/live-model-t2-final3.json

# 直接工具/业务层测试 + 完整回归
apps/api/.venv/Scripts/python.exe tools/audit_regression.py apps/api/tests -q -p no:cacheprovider
apps/api/.venv/Scripts/python.exe tools/audit_regression.py tests connectors/tests -q -p no:cacheprovider
apps/api/.venv/Scripts/python.exe tools/verify_agent_runtime.py
node apps/agent-runtime/verify-transition-recovery.mjs
node apps/web/node_modules/typescript/bin/tsc -b apps/web
apps/api/.venv/Scripts/python.exe tools/export_agent_contract.py --check
# apps/agent-runtime 目录
npm run test:audit
```

> 本文件不写“已通过最终验收”。三态结论（通过／需修改／外部条件阻塞）由验收方给出。所有“passed”均指上述命令/用例在本机实际执行结果；失败与失败尝试均保留在证据文件中。
