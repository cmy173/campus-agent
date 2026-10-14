# 历史文档索引（docs/history）

这里保存**已被 V2 取代、但仍需追溯**的首轮材料。历史文档里的启动命令、路径与接口**不再可执行**；
当前状态、迁移台账与验收证据以 `docs/` 下的 V2 文档为准。

## 当前权威文档（不在本目录）

| 主题 | 文档 |
| --- | --- |
| 执行合同 | [../agent-design/goal-v2-personal-assistant.md](../agent-design/goal-v2-personal-assistant.md) |
| 验收总表（条件 1–10、真实/受控区分、已知限制） | [../agent-v2-acceptance.md](../agent-v2-acceptance.md) |
| 进度与增量记录（唯一权威进度） | [../agent-v2-progress.md](../agent-v2-progress.md) |
| 迁移与退役台账 | [../agent-v2-migration.md](../agent-v2-migration.md) |
| 性能与改造前后对照 | [../agent-v2-performance.md](../agent-v2-performance.md) |
| 工具面与按需发现 | [../agent-design/tools-v2.md](../agent-design/tools-v2.md)、[../agent-v2-tool-discovery.md](../agent-v2-tool-discovery.md) |
| UI 改版指南 | [../agent-design/ui-redesign-v2.md](../agent-design/ui-redesign-v2.md) |

## 本目录的三组历史

### `first-round-docs/`（本工作树的首轮说明文档，2026-10-14 归档）

这些文档描述首轮（pre-V2）的产品规划与验收，已被 V2 合同与上面的权威文档取代；
对应的可执行实现（`campus_agent` 包、旧静态 UI、旧启动脚本、`notice_*` 抽取、旧 `llm.py` 转发、旧 `storage.py`）
已按 `docs/agent-v2-migration.md` 的台账退役。

- `vision.md`、`requirements.md`、`mvp.md`、`roadmap.md`、`open-questions.md`：首轮范围与路线
- `integration-review.md`：首轮集成复核
- `agent-acceptance.md`：首轮验收记录（V2 验收见 [../agent-v2-acceptance.md](../agent-v2-acceptance.md)）
- `agent-progress.md`、`agent-progress-history.md`：首轮进度记录（V2 进度见 [../agent-v2-progress.md](../agent-v2-progress.md)）

### `first-round-legacy/`

首轮工作台/旧服务器时期的安装与运行说明（含 `local-setup.md`），以及本组历史的总说明。

### `first-round-source/`

首轮**另一个工作树**（`campus-agent-core`）的文档快照，用于比对该轮的设计与进度；不是本仓库的运行文档。

## 未归档的首轮文档

部分首轮文档仍在 `docs/` 顶层保留，因为它们描述的**校园连接器行为在当前实现中依然生效**
（例如 `connectors.md`、`campus-login.md`、`eta-profile.md`、`college-notices.md`、`notice-extraction.md`、
`webvpn-*.md`），或被代码/工作流直接引用（`campus-login.md`、`webvpn-analysis.md`）。
归档它们需要先确认引用，属未完成的收尾事项。

## 用户数据与快照

首轮源码与用户数据的可恢复快照在仓库之外：`D:/AI/campus-agent-goal-backups/20261013-v2-start/RESTORE.md`。
快照包含私有数据，不能发布。
