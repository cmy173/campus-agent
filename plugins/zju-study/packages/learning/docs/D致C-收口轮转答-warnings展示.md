# 【D→C】M1 收口轮转答（可直接粘贴给 C）

发件：智能体 D（分支 `agent/d`）｜日期：2026-10-10

---

【D→C】M1 收口轮完成，与 C11/renderer 相关的两点：

① **warnings 可直接展示**（C11 依赖）：`build_tasks_json` 输出的 `warnings` 为 7 类，逐条 `{code, message, task_ids, details}`：
- 码值：`DUPLICATE_CONTENT / TASK_VERSION_UPDATED / UNSUPPORTED_EXTENT / MISSING_DURATION / DUPLICATE_TASK_ID / EXISTING_TASK_UNMATCHED / HINT_UNRESOLVED`。
- `message` 为完整中文文案（非空、确定性、可 `json.dumps`），**可直接渲染，无需前端推断或拼接**；已由测试固化（`tests/test_b13_integration.py::test_all_warning_codes_have_display_ready_messages`）。
- 交付路径：B13 接入后随 job 结果返回（完整 warnings 在生成结果里，落库阶段以 B13 实现为准）。

② **`needs_ocr` 状态以上游数据为准**：扫描件解析结果由 D2 返回 `needs_ocr`（非失败）；B5 当前按 failed + 错误码 `NEEDS_OCR`（可重试）落库——片段抽屉/资源详情的"需 OCR"态请按 B 的落库映射展示，前端不做推断。解析护栏补充：`needs_ocr` 判定为"可提取文本 < 20 字符"启发式（三格式一致，已固化边界测试），极短真实文本可能被误标，属已知限制。

其余集成契约（计划变化摘要 `changes.summary_lines`、未安排项 `suggestion` 等）同 M1 交接 §6，均以上游数据为准。
