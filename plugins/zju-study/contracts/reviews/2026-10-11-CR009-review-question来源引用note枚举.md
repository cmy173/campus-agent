# CR009 · review-question 来源引用枚举增补 `note`（备注行出处）

- **编号**：CR009（CR008 之后，编号由 B 分配）
- **日期**：2026-10-11
- **提出人**：智能体 B
- **状态**：merged（B 裁决，随 #47 出卷流水线并入 v0.1；兼容新增）
- **关联**：`contracts/schemas/review-question.schema.json`（枚举与描述）；`contracts/examples/api/review-paper.json`（qst_7c1a03 增备注出处）；`contracts/examples/validate_examples.py`（出处校验分支扩充）；B 实现（`apps/api/app/review_engine.py::_map_source_ref`——D 的 `notes` 来源 → 契约 `note`）；随 #47 冒烟（`var/tmp/smoke_review_generate.py` T1 全卷逐题过 `review-question.schema` 时发现）

## 背景与动机

CR008 冻结时 `review-question.schema.json` 的 `source_refs[].kind` 枚举为 `["resource", "assignment", "web"]`，而本轮功能要求之一是**用户备注完整原文与材料一起发送给模型**，且约束允许依据备注行出题（样例备注第 2 行即「出一道头插法相关的题」；`paper-blueprint.schema.json` 的知识点 evidence 早已有 `kind: "note"`）。D 引擎的题目来源引用（`QuestionSourceRef`）含 `notes_line` 行号来源，B 适配层如实映射为契约 `kind: "note"`（`locator: "备注第N行"`）时被冻结枚举拒绝——属冻结时的枚举遗漏。

若按冻结枚举丢弃 `note` 出处，将违反「来源如实、不得伪造通过」；映射为其他 kind 则会指向不存在的 chunk/作业。故提出本次**兼容新增**：枚举加值 `note`，并显式定义其形状与语义。

## 变更类型

- [x] 兼容新增（`source_refs[].kind` 枚举加值 `note`；既有三值形状与语义零变化）
- [ ] 补充说明（无——描述随枚举同步澄清）
- [ ] breaking（→ v0.2）

## 影响范围

| 维度 | 内容 |
| --- | --- |
| 契约文件 | `schemas/review-question.schema.json`：`kind` 枚举 `[resource, assignment, web, note]` + 描述补充（note=用户备注原文，locator 记行号）；`examples/api/review-paper.json`：qst_7c1a03 增一条 `note` 出处（示范形状）；`examples/validate_examples.py`：出处校验显式分支——note 要求 `locator=备注第N行` 且 N 在备注行数内、资源/作业/URL 字段全 null；web 分支显式化；未知类型报错 |
| 智能体影响 | A：无（题库候选不产 note 引用）；B：适配层已按此形状实现（`_map_source_ref`），冒烟逐题过 schema；C：可选——题目出处渲染「备注第N行」为纯增量（如仍按通用 locator 渲染则零改动）；D：无（其 `QuestionSourceRef(kind="notes", notes_line=N)` 契约不变，由 B 适配层映射） |
| 配置/环境 | 无 |
| 数据 | 无结构变化（题目 `source_refs` 存 JSON，无需迁移） |

## 提案内容

### 1. `source_refs[].kind` 增补 `note`（兼容新增）

- 枚举：`["resource", "assignment", "web", "note"]`。
- `note` 形状：`locator` = `备注第N行`（N ∈ 1..备注文字行数，备注为快照内逐字节原文，`notes.sha256/chars` 留痕可回溯）；`resource_id` / `chunk_id` / `assignment_id` / `url` / `site_name` 均为 `null`。
- 语义：题目（或其约束依据）来自用户备注原文的第 N 行；**不指向任何资源片段、作业或网络候选**，不得借此伪造材料来源。

### 2. 样例与校验更新

- 旗舰样例 `review-paper.json`：仅 qst_7c1a03（按备注「头插法」要求补充）增一条 `note` 出处，其余题目不动——样例本身即体现"同一题可同时有材料出处与备注出处"。
- `validate_examples.py`：`note` 行号越界、携带 ID/URL 字段、未知类型均判失败（比 schema 更强的跨文件校验）。

## 迁移方式

- 既有生产方/消费方零影响：枚举加值，既有三值样例与调用逐字节不变。
- 并入版本：v0.1（冻结批次内兼容新增，处置同 CR001–CR008）。
- 回滚方式：撤枚举值、样例引用与校验分支即可；无数据影响。

## 待决问题

无。

## 评审记录

| 智能体 | 意见 | 日期 |
| --- | --- | --- |
| A | 随本轮并行开发反馈（题库候选不产生 note 引用） | — |
| C | 随本轮并行开发反馈（出处渲染走通用 locator 即零改动） | — |
| D | 随本轮并行开发反馈（引擎 notes 来源不变，B 适配映射） | — |

**B 裁决**：merged——兼容新增并入 v0.1（2026-10-11 补录；契约自检 31 schema、样例校验 24 文件 78 对象全绿）。
