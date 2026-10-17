# CR005 · ICS 导出格式枚举 + NEEDS_OCR 口径显式化

- **编号**：CR005（CR001–CR004 已占用；本编号由 B 分配）
- **日期**：2026-10-10
- **提出人**：智能体 B
- **状态**：merged（B 裁决，随 B19/B20 并入 v0.1；兼容新增 + 补充说明）
- **关联**：B19 ICS 导出（`apps/api/app/ics.py`、`apps/api/app/worker.py` `_export_ics`）；B20 parse_status 契约漂移关闭（`apps/api/app/worker.py` handle_parse needs_ocr 映射）；收官轮任务书 B19/B20；D17（ICS 全天/精确时间区分校验）

## 背景与动机

1. **ICS 导出（B19）**：首版 `POST /exports` 仅支持 `format=json`；收官轮要求计划与任务截止可导出为 iCalendar 供用户导入系统日历。实现已落地（用户时区渲染、UTC 存储、全天事件与精确时间区分、产物经 `GET /files/{id}` 下载），属**兼容新增**：`format` 枚举加值 `ics`，既有 `json` 调用零影响。
2. **NEEDS_OCR 口径（B20）**：契约 `parse_status` 枚举（pending/processing/ready/failed/skipped）无 needs_ocr，而解析流水线（D2 `ParseStatus.NEEDS_OCR`）对扫描件会产出该状态。实现自始采用映射：**needs_ocr 如实落 `failed` + 任务错误码 `NEEDS_OCR`（可重试）**，不把"未提取到文本"写成成功。属契约-实现口径漂移的**正式关闭**：选方案 ①（保留现映射），在本版契约描述中显式写明，不新增枚举值。

## 变更类型

- [x] 兼容新增（format 枚举新增值 `ics`——评估后并入当前版本）
- [x] 补充说明（NEEDS_OCR 口径不改结构，仅澄清语义；parse_status 枚举值集合不变）
- [ ] breaking（→ v0.2）

## 影响范围

| 维度 | 内容 |
| --- | --- |
| 契约文件 | `openapi/openapi.yaml`：① `/api/v1/exports` summary/description 扩为双格式；② `CreateExportRequest.format` 由 `const: json` 改为 `enum: [json, ics]`；③ `include` 描述加"仅对 format=json 有效（携带于 ics 即 400）"；④ 资源列表 `parse_status` 查询参数补描述；`schemas/resource.schema.json`：`parse_status` 描述补 NEEDS_OCR 口径 |
| 智能体影响 | A：无；B：实现已落地（ics.py + worker `_export_ics` + exports 路由）；C：可增"导出为日历（.ics）"入口（纯增量，无强制）；D：无 |
| 配置/环境 | 无（时区解析复用 `resolve_user_tz`：availability 时区名；Windows 无 tzdata 时 +08:00 固定回退已如实记录） |
| 数据 | 无结构变化（导出产物为受控文件记录，`text/calendar`；既有表不变） |

## 提案内容

### 1. `POST /exports` 支持 `format=ics`（兼容新增）

- `CreateExportRequest.format`：`enum: [json, ics]`，默认 `json`（原 `const: json` 为单值约束，放开为枚举新增 `ics`，不构成 breaking）。
- 语义：`ics` 导出当前 **active 计划** 的全部定时块 + 全部任务的截止时间，合并为单个 iCalendar（RFC 5545）文件：
  - 定时块：`DTSTART;TZID=<用户时区>` 本地墙钟；任务截止 `due_precision=datetime` 同此；`due_precision=date` 为全天事件（`DTSTART;VALUE=DATE` + 次日 `DTEND`，半开区间）；`due_precision=unknown` 不导出（不猜测）。
  - UID 稳定：`{block.id}@study-workbench` / `{task.id}@study-workbench`（重复导出幂等合并）。
  - 不导出：candidate 计划、cancelled/archived 任务、已取消计划；无 active 计划时仍导出任务截止。
- `include` 参数**仅对 `format=json` 有效**；`format=ics` 时携带即 `400 VALIDATION_FAILED`（ICS 为计划与截止的整体视图，无类别过滤语义）。
- 产物仍为受控文件：响应仅含 `file_id`，经 `GET /files/{file_id}` 下载（所有权检查；本地路径不出 API）。
- 幂等：指纹含 `format`（同参数重复提交复用任务）。

### 2. `parse_status` 的 NEEDS_OCR 口径显式化（补充说明，无结构变更）

- **选方案 ①（保留现映射）**：`parse_status` 枚举值集合不变（pending/processing/ready/failed/skipped），不新增 `needs_ocr` 值。
- 显式语义（写入契约描述）：**需要 OCR 的扫描件如实落 `failed`，原因以任务错误码 `NEEDS_OCR` 表达（`retryable=true`），`chunk_count` 恒为 `null`**；不得写成 `ready`/成功。
- 依据：D2 解析器产出 `ParseStatus.NEEDS_OCR` 为内部工程状态；对外保持既有五值枚举可兼容所有已冻结样例与 C 端徽标逻辑；错误码通道（job errors）已能表达可重试原因（与 CR003 warnings 同型思路）。

## 迁移方式

- 既有客户端不受影响：`format` 缺省 `json` 行为逐字节不变；`parse_status` 枚举与样例零结构变化。
- 并入版本：v0.1（冻结批次内兼容新增/补充说明，同 CR001–CR004 处置）。
- 回滚方式：ICS 无数据/结构影响，回滚仅需撤下 openapi 枚举与实现段；NEEDS_OCR 为描述澄清，无回滚项（实现映射不曾改变）。

## 待决问题

无（NEEDS_OCR 已按收官轮任务书 B20 在方案 ①/②中选定 ①）。

## 评审记录

| 智能体 | 意见 | 日期 |
| --- | --- | --- |
| A | 无影响（ICS 属 B/D 侧产物；时区来源为 availability 字段） | 2026-10-10 |
| C | 纯增量（可增日历导出入口；`format` 缺省不变） | 2026-10-10 |
| D | 消费方（D17 校验全天/精确时间区分；NEEDS_OCR 映射与其 parser 状态一致） | 2026-10-10 |

**B 裁决**：接受（ICS=兼容新增；NEEDS_OCR=补充说明）。随 B19/B20 补录入 v0.1：openapi `format` 枚举 [json, ics] + 双格式描述 + `include` 限定；parse_status 两处（openapi 查询参数、resource.schema.json）写明 NEEDS_OCR 口径；补录日期 2026-10-10。
