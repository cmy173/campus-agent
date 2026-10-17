# 智能体 D · M1 交接（packages/learning）

- 批次：M1（手动闭环）｜分支 `agent/d`｜工作目录 `.worktrees/agent-d`
- 提交链（M1）：`45e5176`(D1 回执) → `d3fab29`(D2) → `b8b51cf`(D5) → `2bd5aeb`(D4) → `655fe57`(D8) → `1574472`(D3) → `9b05b44`(D6) → `bb20705`(D7) → `202dea3`(D9) → `b7b43bb`(A10/D4 对齐)
- 提交链（M1 收口轮）：`bfcadeb`(D12) → `fe62c3a`(D13) → `0a3e01e`(B15 配合) → `1b37ccf`(D14)；收口轮交付详见 `docs/D-M1收口轮交付.md`
- 复跑：**227 项 pytest 全绿 / 0 skipped**（含样例预期、硬约束、确定性重跑、模型端口零网络、B13 集成、零网络强断言、D10 真实样例校准/观察）
- 状态口径：**已实现 / 已验证 / 模拟验证 / 尚未验证** 严格区分（见 §5）

## 1. 变更文件

| 区域 | 文件 | 说明 |
|---|---|---|
| D1 契约回执 | `contracts/reviews/2026-10-09-D1-确认回执.md` | 同意冻结 v0.1（5 份 schema 逐项核对，validate 工具全过） |
| D2 解析 | `src/learning/parse/{__init__,types,chunking,assemble,pdf,pptx,docx,cache}.py` | `parse_document` / `detect_format`；`PARSER_VERSION=0.1.0` |
| D3 评估 | `samples/evaluation/`（5 文档 + `labels.json`）、`tests/make_eval_docs.py`、`tests/test_eval_samples.py`、`docs/D3-解析评估报告.md` | 确定性生成 + 人工标注对照 |
| D4 回放 | `src/learning/transcript.py`、`samples/transcript/transcript-sample.json`、`tests/test_transcript.py` | 双形态字幕接受、主题分段保留秒数 |
| D5 重排 | `src/learning/schedule/revise.py`、`tests/test_revise.py` | `revise_plan` + `PlanRevision`（变化/冲突/中文摘要） |
| D6 估计 | `src/learning/estimate.py`、`taskgen.py`（`type_estimate_factors`/`scaled_minutes`）、`tests/test_estimate.py` | `calibrate_estimates` 规则闭环 |
| D7 校准 | `samples/baseline.json`、`tests/test_calibration.py`、`docs/D7-样例校准说明.md`、`taskgen.py`（`RESOURCE_TASK_TYPE`） | 形态对齐契约与 xzzd 样例族；数值待实测 |
| D8 端口 | `src/learning/model.py`、`tests/test_model.py` | `ModelConfig`/`ModelGateway`/默认零调用 |
| D9 矩阵 | `tests/test_matrix.py` | 11 项边界矩阵 + `validate_plan` 反向用例 |
| 导出 | `src/learning/__init__.py`、`samples.py` | 汇总导出与样例加载 |

## 2. 输入输出契约（B 集成视角）

### 2.1 解析（D2，`from learning.parse import ...`）

- `detect_format(filename=None, mime_type=None) -> "pdf" | "pptx" | "docx" | None`
- `parse_document(data: bytes, *, filename=None, mime_type=None, context: ParseContext | None = None, limits: ParseLimits | None = None, cache_dir=None) -> ParseResult`
  - `ParseResult.status`：`ready` / `needs_ocr`（**扫描件非失败**，chunks 可能为空）/ `failed`（附 `error_code`）。
  - `ParseContext(resource_id, resource_version, content_hash, job_id, observed_at)` 由 B 现盖；
    `result.chunk_dicts(context)` 产出契约 `ContentChunk` 形状（locator=page/slide/text；含 provenance）。
  - 缓存键 = `content_hash + PARSER_VERSION + 解析器库版本 + limits.cache_shape()`；命中仅复用解析结果、重盖溯源。
  - 错误码：`UNSUPPORTED_MEDIA_TYPE / EMPTY_DOCUMENT / CORRUPT_FILE / ENCRYPTED_FILE / FILE_TOO_LARGE / PAGE_LIMIT_EXCEEDED / ZIP_LIMIT_EXCEEDED / PARSE_TIMEOUT / DEPENDENCY_MISSING / INTERNAL_ERROR`。
- `ParseLimits`（护栏，B 可按环境收紧）：文件 30MB、300 页/张、5000 片段、单页 20 万字符、60s。

### 2.2 回放字幕（D4）

- `normalize_transcript(segments, *, limits=None) -> Transcript`：接受契约字段（`start_seconds/end_seconds/text/translated_text`）**或**平台原始字段（`BeginSec/EndSec/Text/TransText`，数值或数字字符串）。
- `segment_topics(transcript, config) -> list[TopicSpan]`（保留起止秒数）；`transcript_chunks(...)` → `ParsedChunk`（locator=timestamp）。
- 仅 `ready` 视为有内容（`TranscriptStatus.is_ready`）；其余五态（A10 §2 状态表）只按时长创建观看任务、不声称理解内容——`non_ready_note(status)` 供完成标准备注：`pending/processing`=等待平台更新（A 侧拉取，D 不轮询）、`unavailable`=确认无字幕、`failed`=暂不可得、`unknown`=保守按无字幕。

### 2.3 任务与计划（M0 基线上强化）

- `build_tasks(resources, assignments, recordings, options: TaskGenOptions) -> TaskGenResult{tasks, warnings}`；稳定 ID、版本/重复合并、用户修正保留；`TaskGenConfig.type_estimate_factors` 仅在 D6 校正后由 B 注入且只作用于**推断型**估计。
- `build_tasks_json(payload) -> {"tasks": [...], "warnings": [...]}`（**B13 集成入口**，收口轮 D12）：契约形状 JSON 直传；`tasks[].id` 即 `match_key`；幂等与用户修正保留语义详见 `docs/D12-B13接入说明.md`。纯函数，无 DB/网络依赖。
- `generate_plan(tasks, availability, options) -> Plan`（不变）。
- `revise_plan(previous, tasks, availability, options) -> PlanRevision{plan, changes, conflicts, fully_valid}`；`changes.summary_lines` 为中文变化摘要；`conflicts` 四类锁定冲突码显式返回（`fully_valid=False` 时不假装计划有效）。
- `validate_plan(plan, tasks, availability, options=None) -> list[Violation]`（12 类违规码，可单独作为 B 落库前复核）。
- `calibrate_estimates(entries, *, current_factors=None, options=None) -> CalibrationReport`：只计 completed 且有正整数实际耗时；中位比抗离群；单步 ≤0.15；<3 样本明确不调整。

### 2.4 模型端口（D8，默认零网络）

- `ModelConfig.from_dict(data)`：`enabled / base_url / model / api_key_ref / timeout / max_retries / max_calls_per_run / max_cost_units / extra`；明文密钥字段（`api_key/key/token`）拒绝；`config.version()` = sha256 12 位，随配置变化。
- `ModelGateway(config, client=None)`：默认 `NullModelClient`（任何调用返回 disabled，不发起网络）；预算（调用数/成本）与超时/重试；`CallRecord` 记录成本与配置版本。
- `build_request(...)`：上下文 ≤20 块 / 8000 字符，无工具执行面；`validate_suggestions(...)` 结构/引用/时长/依赖校验（`BAD_STRUCTURE / UNKNOWN_TASK / DURATION_OUT_OF_RANGE / UNKNOWN_PREREQUISITE`）。

## 3. 运行方法（可直接复制）

```bash
cd .worktrees/agent-d/packages/learning
.venv/Scripts/python -m pytest -q                        # 全量 227 项，0 skipped
.venv/Scripts/python -m pytest tests/test_d10_real_samples.py -q  # D10 真实样例校准/观察（快照 sha256 冻结）
.venv/Scripts/python -m pytest tests/test_matrix.py -q   # D9 矩阵
PYTHONPATH=src .venv/Scripts/python -c "import io,sys; sys.stdout=io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8');
from learning import run_sample; r=run_sample('baseline'); print(len(r['plan'].blocks), len(r['plan'].unassigned))"
PYTHONPATH=src .venv/Scripts/python tests/make_eval_docs.py   # 确定性重生成 D3 评估文档（哈希应不变）
```

依赖：Python 3.13 + pytest；PDF 解析需 `pypdf`（`.venv` 已装 6.20.0）；pptx/docx 用标准库。

## 4. 测试结果

- **227 passed / 0 skipped**（约 0.9s，按收集口径）：样例期望 34、解析 32、重排 25、模型 21、字幕 20、估计 15、任务生成 9、确定性 9、分块 9、排程单元 8、矩阵 7、评估 7、校准 6、**B13 集成 7**、时间工具 5、**零网络 5**、**D10 真实样例 8**。
- 阶段验收对照：
  1. 片段可落库并定位页码/幻灯片 → D 侧已验证（解析单测 + 5 份标注对照）；B 落库/C 展示联调见 §6。
  2. D5 四类原因可复现 → 已验证（`test_four_reason_codes_reproducible`）。
  3. 既有测试不回归 → 通过（收口轮 202 → 218 全绿、0 skipped）。
  4. 无模型规则模式完整可用 → 已验证（全部测试在无网络下运行；`NullModelClient`；收口轮 socket 层强断言）。
  5. D8 端口就绪、默认零调用 → 已验证（`test_disabled_config_never_calls_client` 等 21 项 + `tests/test_no_network.py`）。
- D9 矩阵 11 项（零空闲/跨日窗口/每日上限/已过期/缺失截止/锁定任务/依赖环/不可拆分/截止改变/模型超时/伪造引用）逐项定位见 `tests/test_matrix.py` 顶部表格；并含 `validate_plan` 反向用例（证明违规可被检出，非空转预言机）。

## 5. 已知限制（状态口径）

- **已实现 / 已验证**：D2 文本型解析（自编文档集）、D4/D5/D6/D9 全部规则路径、D8 端口与零调用。
- **模拟验证（形态真实、数值虚构）**：D7——`minutes_per_unit`（3 分/页、2 分/张）与兜底时长（reading 60 / watching 60 / assignment 90 / review 45）均为**假设值**，不宣称准确。D10 已用 A16 真实样例复核：**仍无法数值校准**（缺 ① 页/张数——extent 恒 null ② 实际耗时观测），观察与补全路径见 `docs/D10-真实样例校准报告.md`。
- **已执行（D10）**：真实样例校准——结论"无法校准（输入缺失，非实现缺失）"+ 8 项观察固化（快照哈希、90 任务确定性、extent 休眠静默兜底、>30MB 护栏命中集、截止形态等）；触发条件与回填建议已转 B（`docs/D致B-D10观察与回填建议.md`）。
- **解析边界提示**：`needs_ocr` 按"可提取文本 < 20 字符"启发式判定（三格式一致，`test_needs_ocr_threshold_boundary` 固化）；极短的真实文本文档可能被误标，精细化需真实样例。
- **尚未验证**：
  - 真实 xzzd 课件（中文版式、复杂表格/公式、加密课件）的解析鲁棒性——待 A5b 或首批真实文件；
  - 真实平台字幕的端到端（A 样例形态已单测接受，真实抓取链路未联调）；
  - 外接模型 API 真实调用（本阶段按决策不调用，端口就绪）。
- 口径声明：启发式失败表述为“当前策略未能安排”，不宣称数学不可行；不承诺全局最优；准确率只见 D3 报告所载观察，不跨课程泛化。

## 6. 待协作事项

1. **B（需裁决）**：`parse_status` 契约枚举无 `needs_ocr`——D2 对扫描件返回 `status="needs_ocr"`（非失败）。请裁决落库映射（建议二选一：① schema 增枚举值；② B 侧存独立标志位）。C 的片段抽屉需展示“需 OCR”态。
2. **B（集成）**：解析缓存目录（`cache_dir`）与 worker 复用；`FeedbackRecord → calibrate_estimates` 取数窗口（建议近 N 条完成记录）与系数回注 `type_estimate_factors`。
3. **B（配置位）**：`ModelConfig` 从服务端配置注入（B10）；密钥仅引用名（env/secret），缺失即降级为规则模式。
4. **A/B（A16 → D10 已完成）**：数值校准触发条件=每类 ≥3 条实际耗时反馈（D6 闭环，流程见 `docs/D7-样例校准说明.md` §5）；B 回填 `extent_value/unit`（D2 解析统计）后页/张估算激活；3 个产品裁决项与观察已转 B（`docs/D致B-D10观察与回填建议.md`）。
5. **C（展示）**：计划变化摘要（`changes.summary_lines`）、未安排项 `suggestion`、`needs_ocr` 状态均以上游数据为准，不做前端推断。
