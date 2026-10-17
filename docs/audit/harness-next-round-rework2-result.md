# Harness 第二轮返修交付说明（R1 / R2 / W1）

2026-10-15。对象：`docs/audit/harness-independent-rework/acceptance.md` 的返修要求。
本轮结论由独立验收方判定；本文只陈述实际改动、命令与原始结果，**不声明"已通过最终验收"**。

## 1 范围与不变量

- 只做局部工程修正，未重写 Harness,未开放任意 Shell,未只改提示词或只交分析报告。
- 保持原有登录、缓存、权限与停止语义：未触碰 `Runtime`/租约/授权/停止路径；`git status` 中其它既有未提交修改未被覆盖。
- 未新增能力：工具仍是 32 个,`wait_seconds ≤ 14` 仍是"有界观测"而非预算扩张；模型侧 schema 除本轮新增的只读 `rollback` 投影外无变化。
- 未提权、未请求沙箱升级、未提交 commit、未创建 PR。
- 个人数据根未被写入：`var/personal.db`（mtime 仍 2026-10-15 15:39:22）与 `var/secrets` 三个 key 文件 mtime 未变，确定性运行器 E03 的前后指纹自证一致。

## 2 三项返修

### R1 · P2 显式等待参数使完全相同调用不能重放

**根因（验收原文）**：网关读缓存时删掉 `wait_seconds`，写缓存时用原始 request，首次即带 `wait_seconds` 时两侧 hash 不同 → 同一 call id、完全相同的参数第二次返回 409。

**修复**：把"缓存身份"收敛为一处，读写共用同一个值。
- `apps/api/app/agent_bridge/tools.py`：新增 `OBSERVATION_ARGUMENTS`（仅 `jobs.read` / `jobs.cancel` / `connections.sync` / `reviews.generate` 声明 `wait_seconds`）、`observation_arguments(name)`、`cache_arguments(name, arguments)`。规则**不全局套用**：未声明观测参数的工具保持原始参数身份，业务参数变化仍冲突；`new_generation` 明确留在身份内（它决定出哪份卷）。
- `apps/api/app/routers/agent.py`：`cached_request = tools.cache_arguments(req.name, req.arguments)`，读（`cached_call`）与写（`cache_call`）都用它；原始 `req.arguments` 仍用于工具执行与 `tool_result` 事件（日志/审计不失真）。
- `apps/api/app/agent_bridge/store.py`：在 `cached_call`/`cache_call` 上写明"两侧必须收到同一个规范化值"，避免日后再分叉。

**边界**：显式等待、不同等待值、省略/显式 0、真正业务参数变化（仍 409）、事件里保留原始参数——见 `apps/api/tests/test_agent_gateway.py` 新增 6 项与 `apps/api/tests/test_agent_tools.py` 新增 2 项不变量测试。

### R2 · P2 已删除索引的文件被报告为"仍可交付"

**根因（验收原文）**：`discard()` 先提交索引 DELETE 再删物理文件；物理删除失败即抛错，调用方把"抛错"当成"文件仍可交付"，把已失去索引的 id 放进 `delivered`/`retained_file_ids`，下载得 404。

**修复**：把"索引状态"与"物理内容"分开表达，并让报告只依据可验证事实。
- `apps/api/app/agent_bridge/files.py`
  - `discard(cid, file_id) -> 'removed' | 'retained' | 'cleanup_pending' | 'uncertain'`，**不再用异常表达状态**。顺序为索引行先删、再删内容、内容删除失败则按原值把索引行放回（`_restore`，且仅在内容确实还在时才放回）。因此危险的"索引行在、内容不在"不会成为终局：要么行已删（最多留下待清理 blob），要么行与内容一起还在。
  - `_local_path()`：unlink 目标复用与下载入口同一套校验（同级目录、非链接/非重解析点），存的 `storage_name` 不能被用来在会话文件区之外删除文件；`get()` 改用它，使"可下载"与"可交付"判定同源。
  - `indexed()` / `discard_state()`：以索引行为唯一依据（`discard` 已保证行与内容一致）。
- `apps/api/app/agent_bridge/artifacts.py`
  - `_discard_published()` 用 `discard()` 的四个结果分别记录，返回 `{removed, retained, cleanup_pending, uncertain}` 四个**独立**清单（不再把"干净删除"和"残留 blob"混成一个 removed）。
  - `_rollback_details()`：`retained_file_ids` 只含仍能通过会话文件入口取到的 id；`cleanup_pending_file_ids` 只含"索引已删、内容残留"的 id；另有 `rollback` 汇总四态。`delivered` 按同一规则过滤。
  - `_batch_failure()`：仅当 retained/cleanup/uncertain 全空时才给"批次已回滚，可以安全重试"（`retryable=True`）；否则 `retryable=False` 并在消息中分别说明"仅残留待清理"与"状态无法确认"。
- `apps/api/app/routers/agent.py`：`sanitized_delivery_details()` 在 `delivered` 为空但存在 retained/cleanup/rollback 时**也**返回 details（此前会整个吞掉）；新增有界的 `sanitized_rollback()`，四个状态键都发（空列表也发），只含服务端自有 id。

**边界**：索引删除前失败 / 索引已删而 blob 删除失败 / 多文件部分回滚 / 残留不可恢复 / 干净回滚仍可重试 —— 见 `apps/api/tests/test_artifacts_export.py` 的 9 个 R2 case 与独立验收两条新增测试。

### W1 · PR 的前端测试稳定性门槛

**根因（验收原文）**：`AgentPage.test.tsx:107` 断言远端搜索请求，但 `useConversationController.ts:34-37` 的远端搜索有 200ms debounce；第 106 行只等本地列表移除旧项（新 queryKey 下 `historyItems` 立刻为空），不保证请求已发生 → 时序竞争。

**修复**（`apps/web/src/features/agent/AgentPage.test.tsx`，未改产品源码）：先 `waitFor` 断言**真实远端请求**已带上 `search: '数学'`，再 `waitFor` 等远端返回真正替换列表（恰好 1 个按钮、旧项消失），并新增远端返回项 `数学复习` 可见断言；原分页去重、"2 个按钮"、call 参数形状、输入框清空断言全部保留。无 sleep / skip / only / advanceTimers，未调大任何 timeout。

**诚实说明**：改前该竞态在本机是间发性的（单文件 27/27 通过、全量也曾 193/193 通过），未能构造"改前必挂"的对照实验；修复依据是代码路径 + 反复运行证据，**不声称永不再抖动**。

## 3 证据（同一冻结源码）

源码清单：`docs/audit/harness-next-round/rework2-source-hashes.json`（26 个文件）。实时证据阶段摘要由运行器从 6 个产品文件算出：

```
current live source_phase = a1fa2c2057e6   (我独立重算)
```

| 项目 | 结果 | 证据 |
|---|---|---|
| 独立验收新增边界（上轮 2 failed） | **2 passed**，exit 0 | 直接跑 `docs/audit/harness-independent-rework/test_replay_wait.py` |
| 上轮四项边界（回归保留） | **4 passed** | `docs/audit/harness-independent/test_acceptance_boundaries.py`、`test_review_boundaries.py` |
| `artifacts.export` 全量（含 9 个新 R2 case） | **41 passed**，exit 0 | 隔离工作区 |
| 完整 API | **696 passed / 5 skipped**，274.64 s，exit 0 | 冻结树上重跑；5 项为既有主机符号链接限制（WinError 1314） |
| 确定性 12 类 | **12/12 passed，74 checks，exit 0**，263 trace；合成模型 21 次请求，`paid_model_called: false`；E03 前后源码哈希与个人数据根指纹一致 | `rework2-deterministic.json` |
| 真实模型 3×3 | **9/9 完整交付**（t1 3/3、t2 3/3、t3 3/3），阶段 `rework2_live:a1fa2c2057e6`，无自动失败项 | `rework2-live-model.json`、`-summary.md`、`traces-live/`、`artifacts-live/` |
| 前端全套 | **193 passed / 34 files**，exit 0（Lead 于冻结树独立重跑一次；测试责任人另跑两次同为 193/193），目标用例单文件连跑 5 轮全绿 | `apps/web`，见第 5 节 |
| TypeScript | `tsc -b` 通过（exit 0，无输出） | 同上 |

**真实模型 9 次明细**（原保存 provider `prv_c68d2878` / `deepseek-flash`，预算仍为 12 模型请求 / 40 工具 / 150 秒）：

| run | 任务 | 达标 | 模型/工具 | 秒 |
|---|---|---|---|---|
| t1-1-95ecd663 | 附件→PDF/XLSX | ✓ | 3 / 3 | 34.4 |
| t1-2-fe04e6ac | 同上 | ✓ | 3 / 3 | 31.5 |
| t1-3-a143bb3b | 同上 | ✓ | 3 / 4 | 32.0 |
| t2-1-8ce322ee | 材料→预检→生成→等→读卷→导出 | ✓ | 9 / 10 | 44.2 |
| t2-2-d19eec37 | 同上 | ✓ | 9 / 9 | 41.1 |
| t2-3-c3a397e7 | 同上 | ✓ | 9 / 9 | 37.8 |
| t3-1-3d4297a8 | 缺资料不编造 | ✓ | 5 / 10 | 23.2 |
| t3-2-a188282a | 同上 | ✓ | 6 / 10 | 27.4 |
| t3-3-75b24013 | 同上 | ✓ | 5 / 9 | 23.7 |

我另行核对：三次 T2 **各自只有一个** `campus_reviews_generate`（`tool_trace` 计数为 1/1/1），每卷一行 `job`/`paper`/`snapshot`，lineage 与 `answer_cites_delivery` 全为 true。

## 4 本轮一并修掉的真实缺陷（自测发现，非放宽断言）

1. **干净回滚被误报成"有残留"**：`_rollback_details` 一度把"干净删除"也并入 `cleanup_pending_file_ids`，使 `retryable=false` 且消息谎称有残留待清理（teammate-r2 定位到行）。
2. **"行在、内容无"的坏回执**：我曾把删除顺序改成"先内容后索引"，索引行删除失败时留下指向缺失内容的行且仍被当作 retained（实测下载 404）。已改为索引先行 + 校验式放回。
3. **`cleanup_pending` 不可达**：残留 blob 的 id 在分类时被两边丢弃，导致"已删索引 + 残留内容"被当成干净回滚。
4. **HTTP 层吞掉 details**：`delivered` 为空时整个 `details` 返回 None，模型看不到 cleanup 状态；`rollback` 也未投影。
5. R1 侧的同源问题：`cached_call` 与 `cache_call` 各自决定身份，任何一侧改动都会再次分叉（已用单一 helper + 不变量测试锁死）。

## 5 复跑命令

```powershell
# 独立验收新增边界（修复前 2 failed，现应 2 passed）
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py `
  docs/audit/harness-independent-rework/test_replay_wait.py -q -p no:cacheprovider --tb=short

# 上轮四项边界（不得回归）
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py `
  docs/audit/harness-independent/test_acceptance_boundaries.py `
  docs/audit/harness-independent/test_review_boundaries.py -q -p no:cacheprovider

# R1/R2 受影响测试
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py `
  apps/api/tests/test_artifacts_export.py apps/api/tests/test_agent_gateway.py `
  apps/api/tests/test_agent_tools.py apps/api/tests/test_reviews_agent.py -q -p no:cacheprovider

# 完整 API
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py apps/api/tests -q -p no:cacheprovider

# 确定性 12 类（隔离工作区，不写个人数据）
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/verify_next_round_e2e.py `
  --out docs/audit/harness-next-round/rework2-deterministic.json

# 真实模型 3×3（需显式 --live 授权付费调用）
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/verify_next_round_live.py --live `
  --label rework2_live --out docs/audit/harness-next-round/rework2-live-model.json

# 前端
cd apps/web; npm test -- --maxWorkers=2
npx vitest run src/features/agent/AgentPage.test.tsx --maxWorkers=1
npx tsc -b
```

## 6 使用限制与不确定性

- **判定权在独立验收方**：本文只给改动与原始结果，不声明"最终验收通过"；`通过／需修改／外部条件阻塞` 由验收方给出。
- **9/9 是 3×3 小样本**，不构成生产成功率；无温度/随机种子控制，不声称确定性。worker 实测耗时 15–45 秒量级且波动，`wait_seconds ≤ 14` 是"有界观测"而非预算扩张。
- **worker 总 token 与真实金额仍为 `unknown`**（无账单依据）；主 Agent 用量见 JSON。
- **R2 修复后的语义变化（需要验收方确认）**：批次失败时不再一律"零残留"。若某文件确实已发布且其回滚本身失败，它会被**如实报告为 retained 且真能下载**（`retryable=false`）；只有索引已删、内容残留的 id 才进 `cleanup_pending_file_ids`。`test_a06_second_format_publish_failure_leaves_no_partial_delivery` 因此由"必须零文件"改为"干净回滚 → 可重试；有存活文件 → 必须可下载并如实披露"，该改写已由测试责任人确认接受。这是断言语义更新，**不是放宽**（新增了逐 id 实际下载校验）。
- **W1 的 flake 只在间发时被观察到**，未构造"改前必挂"对照；不能断言今后零抖动。
- **未做真实校园账号/WebVPN/学在浙大私有数据验证**：合成流程通过不等于真实平台接入成功。
- 5 项 API skip 为本机既有符号链接权限限制，与本轮改动无关。
- 确定性 12 类中 E03 在**第一次**运行时失败，原因是我当时仍在改产品源码（运行器检测到被测文件在过程中变化）：该次记录已作废，重跑（`run_id=rework2-det-2`）在冻结源码上 12/12 通过；两次记录都已保留。

## 7 回滚

改动集中在 5 个产品文件与 3 个测试文件，无 schema/迁移变化（alembic 单头仍 `a7c3e5b91d24`）。回滚方式：按 `rework2-source-hashes.json` 逐文件还原上一轮内容即可；R1 回滚会恢复"首次带 wait_seconds 的重放被判 409"，R2 回滚会恢复"已删除索引的文件被报告为仍可交付"。
