# V2 功能迁移与退役台账

新源码已在主目录运行，旧 `campus_agent` 应用已于 2026-10-14 删除。校园协议只有一份实现
（`connectors/campus`），产品入口只有一套（`tools/dev.py` → `apps/web` + `apps/api` +
`apps/agent-runtime`）。
完整路由和数据库计数见 agent-v2-baseline.json；复制来源见 agent-v2-adoption.json。
本轮退役的实际执行与验证见 [agent-v2-progress.md](agent-v2-progress.md)。

| 旧能力或入口 | 新系统去向 | 数据处理 | 状态 / 退役条件 |
| --- | --- | --- | --- |
| `campus_agent/server.py`、`__main__.py`、`launch.py`、`start.*` | `tools/dev.py` 唯一启动器 | 不改旧数据库 | **已退役**：旧服务器与旧入口删除；launch.py/start.* 仍委托新启动器 |
| `campus_agent/static/` Python 静态主界面 | React `/chat` 与任务工作区 | 源码在 Git 与快照 | **已删除**（10 个静态文件 + 4 个前端 `.cjs` 测试）；旧 URL 由 `workspaceRoutes.ts` 跳转映射承接 |
| `campus_agent/{academics,academic_defaults,calendar_sources,calendar_extract,campus,campus_auth,campus_college_sources,campus_common,campus_crypto,campus_endpoints,campus_network,campus_profiles,campus_repository,campus_service,connector_contracts,profile_fields,timezone,webvpn_adapter,connectors/}` | `connectors/campus/*`（同名单份实现） | 缓存与快照由 `connectors/campus/campus_store.py` 承载 | **已单份化**：旧包内这些模块此前仅剩别名，现已删除；回归测试直接导入 `connectors.campus.*` |
| `campus_agent/storage.py`（旧 Store） | `connectors/campus/campus_store.py` + `apps/api` SQLAlchemy 模型 | 旧库只读导入，保留完整备份 | **已删除**：`campus_migration.py` 明确不实例化旧 Store；历史导入测试自带冻结 schema |
| `campus_agent/llm.py`（旧模型转发） | 唯一 Harness 运行时（`apps/agent-runtime`） | 不迁移 | **已删除**：不再存在第二套模型主循环 |
| `campus_agent/{notice_analysis,notice_contract,notice_model,parser_worker,services}.py` | `campus.notice.import` → 新资料/版本/片段 → `tasks.build` 真实任务引擎 | 历史整理结果按原字段导入 | **已删除**：新链路由 `apps/api/tests/test_campus_migration.py::test_notice_to_material_read_and_real_task_engine_is_versioned` 覆盖 |
| `campus_agent/core.py`（demo 推荐/活动） | 本地任务与计划引擎 | demo 数据不导入 | **已删除**：无产品调用点 |
| `campus_agent/{campus_routes,passport_routes}.py`（旧本地账号 + CSRF） | 单人本机身份（网关 owner cookie + Origin 校验） | 不迁移密码/会话 | **已删除**：V2 为单人本机工作区，旧多账号注册/CSRF 语义由契约明确不再提供 |
| `campus_agent/campus_sync.py`（旧自动同步循环） | 本机调度模块 `apps/api/app/agent_bridge/schedules.py` | 保留旧加密数据，重新授权 | **已删除**：不保留第二份调度器 |
| `campus_agent/campus_crypto.py`（旧设备盐/凭据解密） | 浏览器持久登录 + `adopt_browser_cookies()` | 旧凭据不迁移，用户重新登录 | **已删除**：`connectors/campus/campus_crypto.py` 只保留 WebVPN 线上协议加密 |
| 校级/学院通知与附件 | `campus.query/sync/notice.read/notice.import` + 文件工作区 | 保留来源与未读声明 | **已迁移**：缓存、正文、附件链接与日期精度保留；其他学院明确 unsupported |
| 上传 documents/chunks/tasks | 新 materials/tasks 及来源映射 | 只读清点、可重入导入、受保护回滚 | **已迁移且已对真实旧库执行**：`tools/migrate_campus_data.py` + 9 项导入/回滚测试；真实 owner 与结果见下 |
| 旧测试与验证脚本（`tests/test_web.py` 等 6 个、`tests/frontend-*.cjs` 4 个、`scripts/verify_*.py` 3 个） | `tests/` 保留的迁移后回归 + `apps/api/tests` + `apps/web` 测试 | 历史在 Git 与首轮快照 | **已退役**：可迁移断言已改挂到 `connectors.campus`（191 项 + 80 subtests），仅测旧服务器/旧静态页的断言随实现删除 |
| 旧依赖与 CI | `.github/workflows/tests.yml` 重写为 API 套件 + 迁移后根回归 + 契约新鲜度 + 前端构建/测试 | — | **已更新**：不再引用 `campus_agent/static/*.js` 或旧 unittest 入口 |
| 首轮文档 | `docs/agent-design/`（现行）+ `docs/history/`（历史） | 历史在 Git | 进行中：`local-setup.md` 已归档到 `docs/history/first-round-legacy/`，受影响文档加迁移横幅 |

恢复入口：D:/AI/campus-agent-goal-backups/20261013-v2-start/RESTORE.md。
快照包含私有数据，不能发布。旧成果本地提交 08ccece，未推送。

**尚未完成**：真实浙大只读试用（需要用户在本机浏览器完成学校登录）、每轮 runner 启动的性能回退修复。

## 真实旧库导入与回滚演练（2026-10-14，已执行）

- 源库 `data/campus.sqlite3`（只读打开）。`inventory` 实测 3 个 owner，其中只有
  `4e7ff483e5be4013b59e15be39d62a37` 有数据（documents 4、chunks 61、tasks 2、campus_snapshots 1、
  connector_cache 1、notice_analyses 1），它也是唯一有 passport 记录的 owner。其余两个为空。
- 真实导入（显式 `--owner`，映射到 `local`，`--school-cache`）：
  `{"batch_id": "mig_5c5b71…", "replayed": false, "status": "imported", "school_cache_imported": true,
  "credentials_imported": false, "domain_promotion": "explicit course and time estimate required",
  "notes_feedback_plans": "No such tables in supported legacy schema; unknown tables block import"}`。
  **凭据不迁移**（旧密码/会话不复用，用户重新登录）。
- 重入：同一源+owner 再导入 → `replayed: true`，不产生重复记录。
- 回滚：用 import 打印的 `mig_…` 批次 → `removed: true`；再回滚一次 → `removed: false`（幂等）。
- 该导入同时作为条件 4 场景 s2 的校园数据来源：模型真的读到了导入的课表/成绩/通知/校历缓存
  （见 `docs/agent-v2-progress.md` 的真实场景小节）。证据 `var/migration-rehearsal-20261014/`。
- 本轮修掉一个真实操作陷阱：`--batch-id` 之前可以被传给 `import` 而被**静默忽略**（import 的批次 id 由
  源指纹+owner+school-cache 派生），随后用这个自造 id 回滚会得到 `removed: false`，看起来像回滚成功。
  现在 `import` 明确拒绝 `--batch-id`，`rollback` 拒绝非 `mig_<32 hex>` 的 id 并以 exit 2 报错；
  已实测两种错误用法都报错、正确流程仍为 `removed: true` → `removed: false`。
