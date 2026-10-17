# 智能体 D · M1 收口轮交付（packages/learning）

- 批次：M1 收口轮｜分支 `agent/d`｜工作目录 `.worktrees/agent-d`
- 提交链（收口轮）：`f34c1c6`(merge main@471e736) → `bfcadeb`(D12) → `fe62c3a`(D13) → `0a3e01e`(B15 配合) → `1b37ccf`(D14) → `8fe8839`(D10 + 隔离修复)
- 复跑：**227 项 pytest 全绿，0 skipped**（收口轮初 218 项；D10 轮 +9 = D10 8 + 导入检查元测试 1）
- 状态口径：**已实现 / 已验证 / 模拟验证 / 尚未验证** 严格区分（见 §5）

## 1. 任务一览

| 任务 | 状态 | 证据 |
|---|---|---|
| **D12** 配合 B13 接入 `build_tasks` | **已实现 + 已验证** | `build_tasks_json` 薄封装（`taskgen.py`）、集成契约 `docs/D12-B13接入说明.md`、`tests/test_b13_integration.py` 7 项 |
| **D13** 默认零网络调用强断言 | **已验证** | `tests/test_no_network.py` 5 项：socket 层守卫 + 守卫元测试（证明断言非空转） |
| **B15** 配合：缺依赖即失败（D 侧测试层） | **已实现 + 已验证** | `_require_pypdf` 去 `importorskip` 改硬失败；`test_test_environment_has_pdf_dependency`；屏蔽 pypdf 实测 FAILED（非 skip） |
| **D14** 解析护栏加固 | **已实现 + 已验证** | 新增 4 项边界测试（§4）；既有护栏逐项复核为已有覆盖 |
| **D10** 真实样例校准（A16 补做） | **已执行；数值校准结论=无法校准（如实）** | 快照入包 + `tests/test_d10_real_samples.py` 8 项；报告 `docs/D10-真实样例校准报告.md`：缺 ① 页/张数（extent 恒 null）② 实际耗时观测 → 不伪造数值；实际观察全固化（§4） |

## 2. 变更文件（收口轮）

| 文件 | 变更 |
|---|---|
| `src/learning/taskgen.py` | 新增 `build_tasks_json(payload)` 薄封装（契约行直传、match_key 对齐、已归档不回生、人工任务分流） |
| `src/learning/__init__.py` | 导出 `build_tasks_json` + 文档说明 |
| `tests/test_b13_integration.py` | 新增 6 项：契约行零转换消费 / 幂等（同输入同 match_key 集合）/ 用户修正保留 / 人工任务与归档语义 / 七类 warnings 可展示（非空、可 json.dumps、确定性）/ 包导入无 DB/HTTP 依赖 |
| `docs/D12-B13接入说明.md` | 新增：调用入口、payload 表、输出形状、幂等 upsert 伪代码、用户修正保留清单、warnings 码表、未验证项 |
| `tests/test_no_network.py` | 新增 5 项：socket 层守卫下解析三格式（含缓存命中）、任务生成+排程+局部重排全链、模型网关默认降级、启用但配置不全仍降级、守卫元测试 |
| `tests/test_parse.py` | `_require_pypdf` 改硬失败；新增环境完整性断言；新增 4 项边界测试（大小上限恰等/超1字节、needs_ocr 阈值 19/20、缓存键随 content_hash、needs_ocr 入缓存） |
| `tests/parse_fixtures.py` | pypdf 依赖说明更新（缺依赖即失败） |
| `docs/D-M1交接.md` | 测试数更新至 218 |
| `samples/real-2026-10-10/` | 新增：A16 真实脱敏样例只读快照（28 资源 + 62 作业；sha256 冻结于测试） |
| `tests/test_d10_real_samples.py` | 新增 8 项：快照哈希冻结 / 契约形状可消费 / 确定性 / extent 休眠（静默兜底 60）/ 作业无耗时证据 / 截止形态与北京时间分布 / >30MB 护栏命中集 / 视频映射与已提交观察 |
| `tests/test_b13_integration.py` | 隔离修复：导入检查改**干净子进程**（原进程内 `sys.modules` 断言与 apps/api 同进程必假阳性）；+1 元测试（注入假 fastapi 证检测非空转） |
| `docs/D10-真实样例校准报告.md` | 新增：校准结论（无法校准及缺项）、实际观察清单、补全路径、冻结复现 |

## 3. 输入输出契约（D12：B 如何调用，详版见 `docs/D12-B13接入说明.md`）

```python
from learning.taskgen import build_tasks_json
result = build_tasks_json(payload)   # 纯函数：不访问数据库、网络、平台
# payload: {resources, assignments, recordings, existing_tasks, user_tasks?, hints?, config?}（均为契约形状）
# result:  {"tasks": [...], "warnings": [...]}
```

- `tasks[].id` **即稳定匹配键**（B 存入 `match_key` 列）；契约行（B 的 `to_contract()` 输出）可直接传入，多余键忽略、`"Z"` 结尾时间可解析。
- `existing_tasks` 分流：已归档 → 忽略（不回生）；有 `match_key` 或 `id` 以 `t:` 开头 → 按稳定键对齐合并；无稳定键 → 视为人工任务原样保留（不产生未匹配告警）。
- 幂等：同输入重复调用得到相同 `match_key` 集合；B13 落库用 upsert（伪代码见说明文档 §4）。
- 用户修正保留：`status / estimated_minutes+provenance / completion_criteria / splittable / priority / prerequisite_ids` 不被覆盖；`due_at / source_refs / title` 跟随来源更新。
- warnings 7 类（`DUPLICATE_CONTENT / TASK_VERSION_UPDATED / UNSUPPORTED_EXTENT / MISSING_DURATION / DUPLICATE_TASK_ID / EXISTING_TASK_UNMATCHED / HINT_UNRESOLVED`）：`{code, message, task_ids, details}`，message 为可直接展示的中文文案（C11 可直接渲染）。

## 4. 运行方法（可直接复制）

```bash
cd .worktrees/agent-d/packages/learning
.venv/Scripts/python -m pytest -q                        # 全量 227 项，0 skipped
.venv/Scripts/python -m pytest tests/test_no_network.py -q    # D13 零网络强断言
.venv/Scripts/python -m pytest tests/test_b13_integration.py -q  # D12 B13 集成（含子进程导入检查）
.venv/Scripts/python -m pytest tests/test_parse.py -q    # D14 解析护栏（含缺依赖硬失败）
.venv/Scripts/python -m pytest tests/test_d10_real_samples.py -q  # D10 真实样例校准/观察
```

## 5. 测试结果与状态口径

- **全量 227 passed / 0 skipped**（收口轮初 218；D10 轮 +9）。新增 9 项全绿；既有 218 项零回归。
- 新增覆盖明细：
  1. **D12**：幂等（重复调用同 match_key 集合、无重复任务）；用户修正保留（42 分钟/完成状态/自定义标准/可拆分/优先级/前置不被覆盖，到期时间跟随来源）；人工任务保留与归档不回生；7 类 warnings 文案非空、可 `json.dumps`、确定性；包导入不牵出 DB/HTTP 依赖（子进程干净解释器 + 元测试）。
  2. **D13**：`socket.socket / create_connection / getaddrinfo / gethostbyname(_ex) / getfqdn` 全部替换为断言守卫后，解析（PDF/DOCX/PPTX + 缓存命中）、任务生成（B13 封装 + 人工任务 + 配置）、排程、局部重排、模型网关默认调用全过程零触发；守卫元测试反向证明断言有效（直接连接即 AssertionError）。
  3. **D14**：大小护栏恰等上限可解析 / 超 1 字节 `FILE_TOO_LARGE`；needs_ocr 阈值边界 DOCX/PPTX/PDF 三格式 19 字符 → `needs_ocr`（`error_code=None`、非失败）、20 字符 → `ready`；缓存键随 `content_hash`（同名异内容不复用、同内容异名命中）；`needs_ocr` 结果入缓存且复用后状态不变。
  4. **B15**：`_require_pypdf` 硬失败（附可执行修复命令）；实测以 `PYTHONPATH` 屏蔽 pypdf 后该用例按预期 FAILED（非 skip）；全仓 `0 skipped`。
  5. **D10**：快照 sha256 冻结防漂移；28+62 行过 `from_dict`；90 任务=28 阅读（全兜底 60、静默无告警）+62 作业（全 90）；两次生成逐字节一致；warnings 零误报；截止 62/62 `datetime`+ISO Z 毫秒（北京 23:59×56 / 21:15×6）；5/28 超 30MB 护栏命中集固定；4 个 mp4→reading、3 条已提交仍生成（观察固化）。
- 状态口径：
  - **已实现 / 已验证**：D12（D 侧契约与测试）、D13、D14、B15（D 侧测试层）、D10（校准可执行性与观察固化）。
  - **模拟验证**：`minutes_per_unit`（3 分/页、2 分/张）与兜底时长为假设值（D7 口径不变）——D10 已确认真实样例**无法提供校准输入**（缺页/张数与耗时观测），等 B 回填 extent + 用户反馈后方可进入数值校准。
  - **尚未验证**：B13 实际接线后的端到端（B 侧落库 + C 侧展示）；D10 数值校准（缺输入，非缺实现）；真实 xzzd 课件解析鲁棒性。
  - **未执行**：无（收口轮全部任务已执行；D10 报告即"无法校准"的如实结论）。
- 已知限制：
  - `needs_ocr` 判定采用"可提取文本 < 20 字符"启发式（三格式一致，已由边界测试固化）；对**极短但真实的文本文档**可能误标 `needs_ocr`——精细化需 A16 真实样例校准，本阶段保持现状并如实标注。
  - 零网络断言证明的是"调用点在 Python socket 层零触碰"；本包无子进程/原生扩展网络面（D8 红线②的实现基础）。
  - 缓存复用语义复核：缓存体仅存内容派生字段，provenance 现盖（M1 已测，收口轮未变更）。

## 6. 待协作事项

1. **B（B13，阻塞链）**：按 `docs/D12-B13接入说明.md` 调用 `build_tasks_json`；job 结果交付完整 `warnings` 供 C11 展示；落库 upsert 用 `match_key`（注意已归档不回生）。
2. **B（裁决，承接 M1 交接 §6.1）**：`parse_status` 契约枚举无 `needs_ocr` 的落库映射仍开放（建议 schema 增枚举值或 B 侧独立标志位）。
3. **B（B15）**：D 侧"缺依赖即失败"已落地（0 skipped）；B 侧 README/环境 fail-fast 请照 B15 任务书推进。
4. **C（C11）**：warnings 7 类码值与中文文案可直接展示；`needs_ocr` 状态以上游数据为准。
5. **A/B（A16 → D10 已完成，观察转答见 `docs/D致B-D10观察与回填建议.md`）**：
   - **B 回填建议**：文件解析完成后将 `extent_value/extent_unit`（页数/张数，取 D2 定位统计）回填资源行——页/张估算路径即从休眠转为生效（数值本身仍需用户反馈校准）；
   - **产品裁决项（B/A）**：① 唯一"可下载但超 30MB 默认护栏"的 PDF（42.9MB）会永久 `FILE_TOO_LARGE`——提高上限 / 流式 / 明示"暂不支持"三选一；② 4 个 mp4（kind=other）→ reading 任务的映射是否改为观看类；③ 已提交作业是否过滤不生成任务。
   - 数值校准触发条件：用户产生每类 ≥3 条实际耗时反馈（D6 闭环），届时按 `docs/D7-样例校准说明.md` §5 执行。
