# 【D→B】M1 收口轮转答（可直接粘贴给 B）

发件：智能体 D（分支 `agent/d`，工作目录 `.worktrees/agent-d`）
日期：2026-10-10｜全量测试：**218 passed / 0 skipped**

---

【D→B】M1 收口轮完成，三件事：

① **请合并 `agent/d` 到 main**：本地 main@471e736 之上共 12 个提交待并（收口轮 5 个 + 此前 D3/D6/D7/D9/交接/A10 对齐 6 个 + 1 个 merge main 提交）。关键提交：`bfcadeb`(D12) → `fe62c3a`(D13) → `0a3e01e`(B15 配合) → `1b37ccf`(D14) → `8da67af`(收口轮交付文档)。合并后 packages/learning 测试数应为 218。

② **B13 调用方式已就绪**（薄封装 + 集成契约）：
- 入口：`from learning.taskgen import build_tasks_json`；`build_tasks_json(payload) -> {"tasks": [...], "warnings": [...]}`，纯函数，无 DB/网络依赖。
- 详版说明：`packages/learning/docs/D12-B13接入说明.md`（payload 表、落库 upsert 伪代码、用户修正保留清单、warnings 码表）。
- 要点：`tasks[].id` 即稳定匹配键（存你的 `match_key` 列）；B 的 `to_contract()` 行可直传（多余键忽略、`"Z"` 结尾时间兼容）；`existing_tasks` 分流——已归档忽略不回生、有 `match_key` 或 `id` 以 `t:` 开头按稳定键对齐、无稳定键视为人工任务原样保留（不产生未匹配告警）；幂等 upsert 用 match_key 对齐。
- 用户修正保留：`status / estimated_minutes+provenance / completion_criteria / splittable / priority / prerequisite_ids` 不被覆盖；`due_at / source_refs / title` 跟随来源更新。
- **job 结果请交付完整 `warnings`**（7 类，中文文案非空、确定性、可 `json.dumps`）供 C11 直接展示。

③ **B15 状态**：D 侧测试层已完成——`test_parse.py` 的 `_require_pypdf` 由 `importorskip` 改为硬失败（附 `pip install -e '.[test]'` 修复提示），新增环境完整性断言，实测屏蔽 pypdf 后按预期 FAILED 非 skip，全仓 0 skipped。B 侧 README/环境 fail-fast 按 B15 任务书继续。

另：`parse_status` 契约枚举无 `needs_ocr` 的裁决仍开放——当前 B5 已按 failed + 错误码 `NEEDS_OCR`（可重试）落库，D2 内部返回 `needs_ocr` 不受影响；如需改枚举走 B6 变更模板。

运行验证：`cd .worktrees/agent-d/packages/learning && .venv/Scripts/python -m pytest -q`（218 passed / 0 skipped）
