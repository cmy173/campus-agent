# real-2026-10-10 样例边界声明（PROVENANCE）

- 生成：2026-10-10（A5b 受控验证会话内）
- 来源：智能体 A 的 A5b 登录态**只读**实测（见 `connectors/docs/A1-接入能力验证报告.md` §3.5 与 `A5b-受控验证记录与证据摘要.md`）
- 用途：供智能体 D（D10：minutes_per_unit / 兜底时长复核）与智能体 B（契约回归/fixtures）使用
- 声明：全程只读；未批量下载；未接触任何凭据；平台标识（课程/文件/作业 ID、标题、真实日期）均已脱敏

## 1. 覆盖范围

| 集合 | 条数 | 说明 |
|---|---|---|
| resources.json | 28 | 逐附件一条（uploads 元素级）。course-1 取前 20 条课件活动（总 38 条）的全部附件；course-2 全部 8 条课件活动 |
| assignments.json | 62 | course-1 全部 8 条作业 + course-2 全部 54 条作业 |
| raw-key-unions.json | - | 两门课 `coursewares`/`homework-activities` 载荷的字段键全集（活动级/`data.*`/`uploads[].*`）与分页信封；只有键名，无值 |

课程：`crs_5a1b01`（简称 A）、`crs_5a1b02`（简称 B）——均为虚构占位，与平台真实 ID 无对应。

## 2. 真实性边界（实测 vs 脱敏处理）

| 字段类 | 处理 | 说明 |
|---|---|---|
| `size_bytes` | **实测原值** | 28 条：15,743 – 867,519,546 字节 |
| 文件扩展名 / `kind` | **实测扩展名 → 适配器 `map_resource_kind` 映射** | mp4→other（4）、pdf/doc/docx→document（22）、pptx→slides（2） |
| `access_level` | **实测许可标记 → 适配器三态真值表** | allow_download=true→full（23）；false+origin_allow_download=true→preview_only（5：4 个视频 + 1 个 pptx） |
| 时间（`due_at`/`source_updated_at`） | 实测格式（ISO 8601 UTC `Z` + 毫秒）；**数值整体平移 -30 天** | 保格式类与时分秒，不保真实日期；禁止与真实日程关联 |
| 标识（ID/标题） | 全部替换为虚构占位 | 平台 ID/真实文件名/课程名绝不入库；`external_id` 为虚构 |
| `instructions` | 恒 `null` | course-1 作业有 description（49–1841 字符）；course-2 观测条目为空串；正文不收录 |
| `mime_type` | 恒 `null` | **平台元数据无 MIME**；`uploads[].type` 为粗分类（实测值 document/video），适配器仅透传 MIME 形态值 |
| `extent_value`/`extent_unit`/`chunk_count` | 恒 `null` | 平台元数据**无页数/幻灯片数字段**（键全集见 raw-key-unions.json）；篇幅只能来自解析（D2），不能来自元数据 |
| `content_hash`/`file_id`/`fetched_at` | 恒 `null` | M1 不落文件内容获取；B 入库后由 B 侧生成 |
| `created_at`/`updated_at`（记录侧） | 捕获时间原值 | 记录自身时间戳（非平台时间） |

## 3. 关键实测发现（供 D10 / B）

1. **截止时间格式**：`due_at` 均为 ISO 8601 UTC `Z` + 毫秒（如 `2026-10-05T13:15:00.000Z`），全部为精确时刻 → `due_precision=datetime`（62/62）。观察值 `15:59:00.000Z` ≡ 北京时间 23:59，符合"当日截止"语义。
2. **提交状态**：列表载荷为 `submitted`(bool) + `submitted_status`(字符串，观测为空串)；62 条中 3 条 `submitted=true`（均位于 course-2）。未见 `graded` 样本。
3. **作业类型**：`data.homework_type` 实测值恒为 `file_upload`（62/62）。
4. **附件粗分类**：`uploads[].type` 实测值 `document`/`video`（非 MIME）。
5. **多附件活动**：单活动可含多附件（course-1 某活动含 10 个文档；course-2 某活动含 pptx 等 ≥2 个）；样例按**逐附件**展开。当前适配器实现为"活动 → 首个存活附件"单资源映射（差异已报 B 知悉，见 `A致B` 转答）。
6. **数量实况**：course-2 单课 54 条作业（同批创建、同一截止）；course-1 单课 38 条课件活动——估算/排程需容忍大数量级。
7. **分页信封**：`start`/`end`/`page`/`page_size`/`pages`/`total`；越界 page 被钳制到末页（样例集信封见 raw-key-unions.json）。

## 4. 截断与重放

- course-1 coursewares 总 38 条，本样例含前 20 条（已覆盖全部类型与许可组合）；course-2 coursewares 8 条全量。
- course-2 作业 54 条全量（第 21–54 条与第 1–20 条同批共享相同截止/开始值，`submitted` 真值位置已按实测）。
- 再生成脚本与原始捕获未入库（含脱敏过程）；如需复现请经用户授权的新一轮只读会话。

## 5. 校验

`python connectors/tests/validate_samples.py` —— 28 项 resource + 62 项 assignment 对 `contracts/schemas/` 全部通过（2026-10-10）。
