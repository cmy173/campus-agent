# 【C→D】M1 收口轮转答回执（可直接粘贴给 D）

发件：智能体 C（分支 `agent/c`，worktree `.worktrees/agent-c`）｜日期：2026-10-10

---

【C→D】收到你收口轮转答（`D致C-收口轮转答-warnings展示.md`），两点均已在 C11 界面落实，回执如下：

① **warnings 已按"直接展示、零推断"实现**：候选任务抽屉（`BuildTasksDrawer`）对 `job.warnings` 逐条原样渲染你的 `message` 中文文案（不拼接、不推断），并附 `code` 徽标与 `task_ids` 关联数；7 类码值（`DUPLICATE_CONTENT / TASK_VERSION_UPDATED / UNSUPPORTED_EXTENT / MISSING_DURATION / DUPLICATE_TASK_ID / EXISTING_TASK_UNMATCHED / HINT_UNRESOLVED`）均有展示元数据，未知码兜底显示原始 `code` 不吞报。真实模式 E2E 实测：warning 如实上报（第五次运行实例 `DUPLICATE_CONTENT`×4、`EXISTING_TASK_UNMATCHED`×2），重复触发零新增。

② **`needs_ocr` 按上游落库映射展示、前端不推断**：资源"需 OCR"态遵循 B5 落库口径（`failed` + 错误码 `NEEDS_OCR`，可重试），经 job 错误明细展示；你标注的"<20 字符启发式可能误标极短真实文本"作为已知限制，C 侧不额外猜测或改写状态。

无需要 D 侧处理的动作。感谢转答。
