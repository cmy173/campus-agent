# Harness 第二轮返修收尾交付说明（N1）

2026-10-15。对象：`docs/audit/harness-independent-rework2/acceptance.md` 的 N1 最小返修条件。
本轮结论由独立验收方判定；本文只陈述实际改动、命令与原始结果，**不声明"已通过最终验收"**、不声称"无阻塞可合并"。

## 1 范围

只修 N1 一处：**"索引行存在"曾被当作"retained 可下载"**。未重构 Harness、未新增权限、未取消隔离、未改工具 schema、未改模型策略、未改出卷链路、未改迁移。

判定原则一句话：**retained 必须由"正常会话下载入口能真正取到"来决定，而不是索引行存在。**

## 2 修改点

### `apps/api/app/agent_bridge/files.py`

- 新增 `servable(cid, file_id) -> bool | None`：**唯一**的"这个 id 现在能否被正常下载入口取到"的判定，复用的正是 `get()` 的同一套检查（索引行存在 + `storage_name` 落在本会话文件目录内 + 非链接/非重解析点 + 内容确实存在）。`True` 可服务，`False` 确认不可服务，`None` 无法核实。
- `discard_state()` 改为以 `servable()` 为依据：索引在但内容缺失 → `rolled_back`（不可交付），不再误判为 retained。
- `discard()` 索引 DELETE 失败分支（原 `files.py:179–181` 的直接 `return 'retained'`）改为：先 `servable()`——
  - 内容确实可服务 → `retained`（如实，且真的能下载）；
  - 内容已缺失 → `cleanup_pending`，并尝试 `_forget_orphan_row()` 清掉那条永远 404 的残留行；清不掉也仍报 `cleanup_pending`（**已知的待清理状态，不是未知**，因此不再降级为 `uncertain`）。
- 删除原 `indexed()`（只有 `SELECT 1` 的弱判定），避免后来者再用弱判定替代下载校验。
- 修复了一个真实缺陷：`_forget_orphan_row()` 曾在已打开的 `BEGIN IMMEDIATE` 事务内嵌套开连接，导致 `database is locked`；现在删除尝试在事务外进行（自测发现，非验收要求）。

### `apps/api/app/agent_bridge/artifacts.py`

- `_rollback_details()` 的二次核对已随 `discard_state()` 一起升级为下载校验（原 `artifacts.py:391–415` 只做索引 SQL）。
- 批次失败 details 新增 `conversation_id`：此前 `delivered` 为空时没有任何会话标识，消费方无法对任何 id 做下载核验，只能整体丢弃清理状态（这正是 N1 在 HTTP 层的伴随缺口）。

### `apps/api/app/routers/agent.py`

- `error_envelope(exc, files=None)` / `sanitized_delivery_details(exc, files=None)` 现在用**工具刚用过的同一个文件存储**核验：
  - `retained_file_ids`、`delivered` **只保留 `servable is True` 的 id**；
  - `cleanup_pending_file_ids` **只保留 `servable is False` 的 id**（确认不可服务），`None`（无法核实）既不当可下载，也不当已确认残留；
  - 拿不到存储句柄时**什么都不广告**（保守方向）。
- `internal_tool` 在构造错误信封时传入 `tools._file_store(ctx)`，即该次调用实际使用的存储。

## 3 验证（同一冻结源码；清单 `rework3-source-hashes.json`，28 个文件）

| 项目 | 结果 | 说明 |
|---|---|---|
| **N1 独立探针** `harness-independent-rework2/test_retained_content.py` | **1 passed**（本轮前为 1 failed） | 复现命令见下 |
| 上两轮六项独立边界 | **6 passed** | `test_acceptance_boundaries.py`、`test_review_boundaries.py`、`test_replay_wait.py` |
| 独立边界合计 | **7 passed** | 三项文件同次运行 |
| `artifacts.export` 全量 | **43 passed**（含 2 个本轮新增 N1 回归） | 隔离工作区 |
| 完整 API | **698 passed / 5 skipped**，276.93 s，exit 0 | 隔离工作区；5 项为既有主机符号链接限制 |
| 十二类确定性 | **12/12 passed，74 checks，exit 0**；E03 子集 177 passed / 2 skipped；E03 源码前后哈希一致、个人数据根指纹一致 | `rework3-deterministic.json` |
| runtime Node | **85 passed / 0 failed** | `apps/agent-runtime` `npm run test:audit`，exit 0 |
| Agent 契约 | current：**51 paths / 54 schemas** | `export_agent_contract.py --check`，exit 0 |
| judge v4 自测 | 12/12，无模型调用（未改判定代码） | 沿用上轮 |

新增的本项目回归测试（`apps/api/tests/test_artifacts_export.py`）：

- `test_r2_an_index_row_alone_never_proves_content_is_downloadable`：索引在、内容已删时 `servable() is False`、`discard_state() == 'rolled_back'`、`get()` 抛 `NOT_FOUND`、`discard()` 返回 `cleanup_pending`。
- `test_r2_index_delete_conflict_with_missing_content_is_not_advertised`：端到端走 HTTP 桥——索引删除冲突 + 内容不可用 → `delivered`/`retained_file_ids` 都不含该 id，它出现在 `cleanup_pending_file_ids`，`retryable=False`，且该 id 真实下载为 404。

### 复现命令

```powershell
# N1 独立探针（修复前 1 failed，现应 1 passed）
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py `
  docs/audit/harness-independent-rework2/test_retained_content.py -q -p no:cacheprovider --tb=short

# 上两轮六项边界
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py `
  docs/audit/harness-independent/test_acceptance_boundaries.py `
  docs/audit/harness-independent/test_review_boundaries.py `
  docs/audit/harness-independent-rework/test_replay_wait.py -q -p no:cacheprovider

# 文件发布/回滚/下载 + 上轮重放
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py `
  apps/api/tests/test_artifacts_export.py apps/api/tests/test_agent_gateway.py `
  apps/api/tests/test_conversation_files.py apps/api/tests/test_agent_file_scope.py `
  apps/api/tests/test_agent_feedback.py apps/api/tests/test_reviews_agent.py -q -p no:cacheprovider

# 完整 API
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py apps/api/tests -q -p no:cacheprovider

# 十二类确定性（隔离工作区）
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/verify_next_round_e2e.py `
  --out docs/audit/harness-next-round/rework3-deterministic.json
```

## 4 关于真实模型证据

验收文档明确：本轮只修文件状态判定时，复验集中在新增探针、文件发布/回滚/下载与必要短链，**不要求再次盲跑完整付费 3×3**。

- 本轮的 9/9（`rework2_live:a1fa2c2057e6`）与独立抽测 3/3 **属于修改前的源码阶段**，本轮**没有**重跑付费真实模型，也**不把旧证据冒充本轮证据**。
- 本轮改动只触及错误/回滚/异常交付路径：正常导出、出卷、读卷、下载的代码路径未变，工具 schema 未变（契约 51 paths / 54 schemas 不变），模型策略与出卷链路未变。
- 若验收方要求本轮源码的真实模型完整证据集，请在下一次验收前告知，我会按原预算（12 模型请求 / 40 工具 / 150 秒）另建同版本 3×3，不拼接旧阶段记录。
- 阶段摘要：`artifacts.py`、`files.py`、`routers/agent.py` 已变，因此**实时证据阶段 `a1fa2c2057e6` 不再能代表当前源码**；这是阶段性事实，不是"证据仍然有效"的声明。

## 5 限制与不确定性

- 判定权在验收方：本文只给改动与原始结果，不声明"最终验收通过"或"无阻塞可合并"。
- **9/9 与 3/3 均为小样本且早于本轮修改**，不构成生产成功率；worker 用量与真实货币费用仍 `unknown`。
- 未做真实校园账号 / WebVPN / 学在浙大私有数据验证；合成流程通过不等于真实平台可用。
- 前端与 Node 之外未重跑：前端 `AgentPage.test.tsx` 本轮未改（哈希与上轮一致 `c4465e5d8933`），故未重复前端套件（上轮 193 passed / 34 files）；Node 侧已重跑 85/85。
- 确定性 12 类中 E03 第一次（`rework3-det-1`）因我在运行期间改 `artifacts.py` 文档字符串被判 `bookkeeping_failures_only`（该次场景检查全过、177 passed）；作废后干净重跑 `rework3-det-2` 12/12 通过。两次记录都保留。
- 未提交 commit、未创建 PR；工作区仍混有此前的 V2、校园网络、UI 与历史证据未提交内容，验收提到的 PR 范围整理、脱敏与实际 CI 仍未完成。

## 6 回滚

改动集中在 3 个产品文件（`agent_bridge/files.py`、`agent_bridge/artifacts.py`、`routers/agent.py`）与 1 个测试文件，无 schema/迁移变化（alembic 单头仍 `a7c3e5b91d24`）。按 `rework3-source-hashes.json` 逐文件还原即可；回滚后 N1 会恢复为"索引在即广告可下载"，独立探针会重新失败。
