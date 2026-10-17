# CR006 · .zip 课件包入库（/imports 白名单放宽）与 parse_status=skipped 口径

- **编号**：CR006（CR001–CR005 已占用；本编号由 B 分配）
- **日期**：2026-10-10
- **提出人**：智能体 B
- **状态**：merged（B 裁决，随收官轮并入 v0.1；兼容放宽 + 补充说明）
- **关联**：A21/A22 真实实测记录 `connectors/docs/A21-A22-真实同步与获取记录.md` §5-2（A 提请 B 裁决 415 冲突，给出 (a)/(b)/(c) 三选项，不代裁、不重命名扩展名伪造类型）；`apps/api/app/storage.py`（白名单与魔数）；`apps/api/app/worker.py` handle_parse（终态映射）；B17 回放与 B21 交付材料

## 背景与动机

A22 受控实测显示：真实课程资源的实际形态为 `.zip` 课件包（HTML/JS/CSS，3/3 实测），而 `/imports` 白名单（`EXTENSION_MIME`）仅 pdf/pptx/docx——真实课程材料经获取链路后**无法归档入库**（415），"真实可用"闭环在最后一环断开。A 侧明确不重命名扩展名伪造类型，提请 B 裁决。

B 裁决：**采用选项 (a) 扩白名单**。理由：`.zip` 本身即 ZIP 容器，与既有 OOXML（pptx/docx）的魔数校验同规（`PK\x03\x04`），安全面不扩大（无解包/无解析，见下）；(b) 独立通道引入第二套存储与下载语义，收官期成本高；(c) 维持现状使真实课程材料永远无法归档。归档价值明确：受控存储 + `GET /files/{id}` 下载 + 哈希可核验，是用户侧唯一一站式去处的第一步。

## 变更类型

- [x] 兼容放宽（`/imports` 新增接受 `.zip`；其余类型的受理/拒绝行为逐字节不变）
- [x] 补充说明（`parse_status=skipped` 语义覆盖 `.zip` 资源；枚举值集合不变）
- [ ] breaking（→ v0.2）

## 影响范围

| 维度 | 内容 |
| --- | --- |
| 契约文件 | `openapi/openapi.yaml`：`/api/v1/imports` 的 `files` 描述补 `.zip`；资源列表 `parse_status` 查询参数描述补 zip 口径。`schemas/resource.schema.json`：`parse_status` 描述补 zip 口径 |
| 智能体影响 | A：无代码影响（A22 fetch 产物至此可落库；不要求 A 侧改任何东西）。B：storage 白名单 + 魔数 + handle_parse 终态映射 + 测试。C：无结构影响（新增接受类型不改任何响应形状；资源卡片展示 skipped 徽标沿用既有枚举逻辑）。D：无结构影响（zip 资源无解析产物，`chunk_count=null`，与 pending/无内容资源同型；不涉及 parser 变更） |
| 配置/环境 | 无（大小上限仍 `STUDY_MAX_UPLOAD_MB`=100MiB；本地路径不外泄等存储约束不变） |
| 数据 | 无结构变化（zip 资源落 `mime_type=application/zip`、`parse_status=skipped`） |

## 提案内容

### 1. `/imports` 白名单新增 `.zip`（兼容放宽）

- `EXTENSION_MIME` 新增 `".zip": "application/zip"`；受理提示更新为 `pdf/pptx/docx/zip`。
- 魔数校验与 OOXML 同规：扩展名 `.zip` 要求 `PK\x03\x04` 开头，否则 415 `UNSUPPORTED_MEDIA_TYPE`；空文件、超大小限制行为不变。
- `.zip` 不重命名、不伪造任何类型字段：`mime_type=application/zip` 如实落库。

### 2. `.zip` 的解析终态 = `skipped`（补充说明，无枚举变化）

- 契约语义（写入描述）：`skipped` 表示**该类型不解析**（既有先例：link）；`.zip` 课件包属此档——**仅存档与下载**，不承诺内容提取。
- 实现口径（handle_parse）：解析流水线对 `application/zip` 如实返回 `UNSUPPORTED_MEDIA_TYPE`（非输入损坏）；资源 `parse_status` 终态落 **`skipped`**（不误标 `failed`），解析任务的 errors[] 仍保留 `UNSUPPORTED_MEDIA_TYPE`（`retryable=false`）以说明"为何不解析"；`chunk_count=null`，不产生任何 ContentChunk。
- 不做（显式）：zip 内容离线解包/渲染为**未来项**（v0.2+，另行提报）；本项不得被解释为"支持解析 zip"。

## 迁移方式

- 既有调用零影响：pdf/pptx/docx 行为不变；新增接受一类此前 415 的输入。
- 并入版本：v0.1（冻结批次内兼容放宽，同 CR001–CR005 处置）。
- 回滚方式：撤 `EXTENSION_MIME` 的 `.zip` 项与 handle_parse 的 skipped 分支即可（无数据结构变化，无数据迁移）。

## 待决问题

无（收官轮 B 裁决关闭；A 的 A21/A22 §5-2 请求项至此结案）。

## 评审记录

| 智能体 | 影响/意见 | 日期 |
| --- | --- | --- |
| A | 事前提请方：A21/A22 §5-2 明确列三选项、要求 B 裁决，不代裁、不重命名伪造类型；本裁决不要求 A 侧任何改动 | 2026-10-10 |
| C | 无结构影响（B 侧评估：不改任何响应形状；skipped 为既有枚举） | 2026-10-10 |
| D | 无结构影响（B 侧评估：zip 无解析产物，`chunk_count=null`；parser 零变更） | 2026-10-10 |

**B 裁决**：接受（白名单=兼容放宽；skipped 口径=补充说明）。随收官轮补录入 v0.1：openapi 两处描述 + `resource.schema.json` 描述；实现随 B17 收口提交；补录日期 2026-10-10。
