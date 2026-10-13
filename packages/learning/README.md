# packages/learning — 内容处理与学习计划引擎（智能体 D）

纯函数、无平台/数据库 IO 的 Python 包：输入领域对象（课程资料、作业、回放、可用时间），
输出候选学习任务与排程计划（含未安排原因、告警、摘要）。由 B 在后端调用并负责持久化、
输入修订号、幂等与 API 呈现；本包不访问学在浙大/智云课堂，也不直接读写业务数据库。

当前为**第一批交付**：D3 规则任务生成（`build_tasks`）+ D4 规则排程（`generate_plan`）+
计划约束校验（`validate_plan`）+ 基准/边界样例 + 测试套件。
**尚未实现**：D1 文档解析、D2 回放文本整理、D5 局部重排与解释、D6 可选模型适配。

## 目录结构

| 路径 | 内容 |
|---|---|
| `src/learning/models.py` | 领域模型（Course/Resource/Assignment/Recording/LearningTask/Availability/Plan…），全部可 `to_dict`/`from_dict` |
| `src/learning/timeutil.py` | ISO 8601（强制带偏移量）解析、按本地日期拆分、周窗口+例外展开（跨午夜、范围裁剪） |
| `src/learning/taskgen.py` | D3：资料/作业/回放/用户任务 → 候选任务（重复合并、版本合并、用户修正保留、稳定 ID） |
| `src/learning/schedule/engine.py` | D4：确定性排程（截止+优先级排序、分块、每日上限、锁定块、未安排原因分类） |
| `src/learning/schedule/validate.py` | 独立约束校验器（对计划如实报告违规，供验收与联调自查） |
| `src/learning/samples.py` | 样例加载与一键运行（`list_samples` / `run_sample`） |
| `samples/` | 基准样例与 8 个边界场景（见下表） |
| `tests/` | pytest 测试套件（65 项） |
| `docs/D0-第一批交付与集成交接.md` | 交付说明：契约细节、运行方法、测试结果、限制、待协作事项 |

## 快速开始

```bash
cd packages/learning
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[test]"   # Windows；Linux/macOS 用 .venv/bin/python
.venv/Scripts/python -m pytest -q                  # 65 passed
```

最小用法（示例数据见 `samples/baseline.json`，加载逻辑见 `samples.py`）：

```python
from learning import build_tasks, generate_plan, validate_plan
from learning.models import TaskGenOptions, ScheduleOptions

gen = build_tasks(resources, assignments, recordings, TaskGenOptions(hints=hints,
                                                                    user_tasks=user_tasks))
plan = generate_plan(gen.tasks, availability, ScheduleOptions(
    horizon_start="2026-10-12T00:00+08:00",
    horizon_end="2026-10-19T00:00+08:00",   # 半开区间：不含 10-19 当天
    reference_time="2026-10-09T12:00+08:00",
    input_revision=1,
    locked_blocks=locked_blocks,
))
violations = validate_plan(plan, gen.tasks, availability, options)  # 预期为空
```

## 参数说明

`TaskGenConfig`（任务生成，均可由 B 覆盖）

| 参数 | 默认 | 含义 |
|---|---|---|
| `minutes_per_unit` | `{"page": 3, "slide": 2}` | 每页/每张幻灯片分钟数（资料有无页码/页数时） |
| `watch_minutes_factor` | `1.0` | 回放观看时长系数（时长 × 系数，向上取整） |
| `default_minutes_by_type` | reading/watching 60、assignment 90、practice 60、review 45 | 缺失可用量时的按类型兜底（标记 `inferred`） |

`ScheduleConfig`（排程，需与 B 冻结）

| 参数 | 默认 | 含义 |
|---|---|---|
| `min_chunk_minutes` | 25 | 拆分后的最小块（可拆分任务） |
| `max_chunk_minutes` | 60 | 单块时长上限；超过则按 `ceil(总时长/上限)` 均匀预拆（如 150→3×50） |
| `default_splittable` | `true` | 任务未显式声明 `splittable` 时的缺省值 |

`build_tasks` 选项：`existing_tasks`（重新生成时保留用户修正）、`hints`（依作业给资料任务补前置）、
`user_tasks`（用户自建任务，原样保留）。

## 样例清单

`python -c "from learning.samples import run_sample; ..."` 或见 `tests/test_samples_expectations.py`。

| 样例 | 目的 | 关键预期 |
|---|---|---|
| `baseline` | 一周全流程（两门课、重复文件、同名新版本、已延期作业、无字幕回放、锁定块） | 11 项任务 10 项可排；逾期作业 `DEADLINE_PASSED` |
| `scenarios/zero_free_time` | 零空闲 | 全部未安排 + `EMPTY_HORIZON` 告警 |
| `scenarios/due_too_soon` | 截止前窗口不足 | `INSUFFICIENT_WINDOW_BEFORE_DUE`（附可用 60/需要 120） |
| `scenarios/indivisible_too_long` | 不可拆分/碎片化 | `TASK_TOO_LONG_INDIVISIBLE`、`FRAGMENTED_WINDOW` |
| `scenarios/dependency_cycle` | 依赖环 + 未知前置 | `DEPENDENCY_CONFLICT` + 环告警；未知前置按已满足处理 |
| `scenarios/cross_midnight` | 跨午夜窗口 | 150 分钟任务拆 3×50，第三块 23:40→次日 00:30 |
| `scenarios/tight_daily_limit` | 每日上限紧 | 90 分钟拆两块落两天；不可拆块溢出 |
| `scenarios/insufficient_total` | 总容量不足 | 第三项 `INSUFFICIENT_TOTAL_TIME` |
| `scenarios/locked_conflict` | 锁定块与上限/窗口冲突 | 原样保留 + 告警，校验器如实报违规 |

## 关键约定（对齐 contracts v0.1 草案）

- 所有时间字符串 ISO 8601 **必须带偏移量**；数据库/接口建议 UTC，渲染由客户端按用户时区（默认 `Asia/Shanghai`）完成。
- 日期缺失为 `null` 不猜测；`due_precision ∈ {datetime, date, unknown}`，`date` 表示“当日截止”（排程截止时刻取次日 00:00）。
- 时长整数分钟；`fact_source`/`estimate_provenance ∈ {platform, user, inferred}`，推断可附 `confidence ∈ [0,1]`。
- 任务 ID 为确定性“稳定匹配键”：`t:{course_id}:{type}:{external_id 或 slug(标题)}`（>120 字符截断加摘要）；
  B 持久化时可映射为契约建议的 `tsk_` 不透明 ID（详见交接文档「待协作事项」）。
- 计划为**候选**：`input_revision` 由 B 维护，应用/过期校验（`PLAN_STALE`）由 B 执行。

## 已知限制

- 排程为确定性启发式（按截止+优先级靠前填充），**不承诺全局最优**；失败表述为“当前策略未能安排”。
- 单块上限 `max_chunk=60` 会把 >60 分钟任务预拆成均匀小块（如 120→2×60、138→3×46），如需整块可调整配置。
- 每日上限按自然日核算：跨午夜块按各日实际分钟分别计入。
- 时区语义仅按用户单一时区（默认 `Asia/Shanghai`）展开；多时区用户未支持。
- 未做规模化性能验证（样例为小数据量）；D1/D2/D5/D6 未实现。

测试与验证状态：65 项测试全部通过（约束校验、场景预期、确定性重跑、任务生成稳定性）。
尚未用真实平台数据验证（等 A 提供样例后再行校准）。
