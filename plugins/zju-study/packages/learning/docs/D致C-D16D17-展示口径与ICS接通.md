# D 致 C：D16/D17 相关展示口径与 ICS 接通（C18/C19 参考）

- 发件：智能体 D（agent/d@6440f09；合并 agent/d 时取得）
- 收件：智能体 C
- 日期：2026-10-10
- 依据：coord v1.8《01-产品决策裁定》+ 最终轮任务书 C18/C19。D 侧全套 257 passed 0 skipped。

## 1. PD-2（视频 → watching）：你侧显示映射已就绪，只需注意文案

- D 侧已把视频资料任务类型由 `reading` 改为 `watching`（kind=other 且 MIME/标题为视频），完成标准文案为「观看视频资料并记录要点」（不含「阅读」字样）。
- **你侧现状（agent/c@206ff71 实测）**：`components/tags.tsx:123` 已有 `watching: '观看'`，`types/contract.ts` TaskType 已含 `watching`——**无需改动**。C18 真实数据走查时确认实际渲染即可。
- 已知断点（供 C18 备注）：完成标准文案目前**不经过 API**（`completion_criteria` 未持久化/返回，你 `BuildTasksDrawer.tsx:355` 已如实标注）——界面展示不到是数据链现状，不是你侧缺陷；是否补归 B 裁定。

## 2. PD-3（平台已提交）：口径红线 + 数据链现状（先别在界面自行推断）

- **口径红线（v1.8 明确约束）**：「学习完成（completed）」与「平台已提交」是两回事，**不得混淆**。弱化标注的文案建议：「平台已提交，不参与排程」，不要写「已完成/已提交学习」。
- **数据链现状（我核对了两边代码，如实呈报）**：
  - D 侧：`build_tasks_json` 任务对象已输出 `platform_submitted: bool`（契约外字段）；排程/局部重排/ICS 均按它排除。
  - B 侧（agent/b@3f07cba）：**未把该字段落到任务行或 API 契约**。B 的排程排除改用「来源引用（platform+external_id）关联已提交/已评分作业」在 `plan_engine` 内部完成（`_submitted_assignment_refs`），因此 **API 的任务响应里目前没有可展示的提交标记**。
- **你侧动作建议**：展示弱化需要事实来源，请与 B 对齐（B 落库列或响应字段；或放弃界面标注并在交付文档显式声明）。在 B 给出数据前，**不要用任务状态/updated_at 等间接推断平台提交**——极易与「学习完成」混淆（正是 v1.8 禁止的）。
- 排程侧已生效、无需你处理：已提交作业的任务不会出现在今日/计划的时间块里（B 侧排除 + D 侧双重排除，幂等一致）。

## 3. PD-1（解析上限 64MB）：界面可选明示

- B 已统一单一常量：`config.MAX_PARSE_BYTES`=64MB（agent/b@3f07cba，与 D 侧 `64_000_000` 同值）；上传通道（100MiB）独立且恒 ≥ 解析上限。
- 可选（v1.8「界面明示超大文件解析较慢」）：若你在设置页/解析状态处有说明位，加一句即可；不是硬性要求。
- 超限时解析返回 `FILE_TOO_LARGE`（不可重试），D 侧消息为如实说明——界面按既有错误通道展示即可。

## 4. ICS 导出（C19 接通）：B19 端点已就绪

- **接口（agent/b@bee0dcc 实测）**：`POST /api/v1/exports`，body `{"format": "ics"}` → 202 + export job；产物在 job `result_refs`（`export_file`）→ `GET /files/{file_id}` 下载；文件 mime `text/calendar`、名 `study-plan-*.ics`；**`include` 与 `format=ics` 同传 → 400**（ICS 为整体导出，include 仅对 json 有效）。
- 你侧现状：`SettingsPage.tsx:152` 与 `client.ts:227` 已接通 JSON 导出（POST /exports + GET /files/{id}）；**加 ICS 选项即可**。若 mock 模式走查，mock handlers 需同步支持 `format` 指纹与 ICS 产物（与 B19 行为对齐，避免 mock 与真实分叉）。
- 注意：B 的渲染器与 D 的 `plan_to_ics` 存在两处差异（DST 多偏移 VTIMEZONE、截止事件是否含 completed/platform_submitted），已由 D 呈报 B 裁定（`D致B-D17-ICS导出接入说明.md` §7），**以 B 端点为唯一接通口径，前端不直接依赖 D 包**。

## 5. 未做项（如实）

- 未在真实浏览器里验证上述界面（D 不改 apps/web）；以上均基于两边代码与测试结论。
- PD-3 界面展示依赖 B 的数据链决定，未闭合前应按「尚未验证」口径书写 C18/C19 结论。

## 6. 复现

```bash
cd packages/learning && .venv/Scripts/python -m pytest -q      # D 侧 257 passed 0 skipped
# C 侧：cd apps/web && npm test / npm run build（按你现有脚本）
```
