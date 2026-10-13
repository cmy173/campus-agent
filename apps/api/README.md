# apps/api — 浙大学习工作台后端（智能体 B）

> 2026-10-11 收尾更新：个人使用请按根目录 README 和 `docs/coord/10-收尾修复与使用说明.md` 启动。默认网页为 real；统一启动器使用 personal.db，不 seed。以下旧章节中的样例导入仅用于开发演示；智云已有独立会话、详情和按需字幕接线，当前真实账号联调待确认。


FastAPI + SQLAlchemy 2.0 + Alembic。阶段 1 为本地 SQLite 开发（`var/study.db`，已在根 .gitignore 忽略），生产切 PostgreSQL 只需设置 `STUDY_DATABASE_URL`。

## 现状：M1–M3 收官（2026-10-10）——契约 v0.1 全端点实现且冻结，真实数据链路经回放验证（B17）

course/resource/resource_version/content_chunk/assignment/recording/learning_task/plan(+blocks/unassigned/warnings)/connection/job/availability/task_feedback/idempotency/file/sync_cursor 已建表；迁移 `00a0513ded7f`（核心表）、`ff82c52062dc`（反馈表）、`b41c9e02f7a3`（plan_blocks 复合主键）、`c9f4a7e21b58`（解析片段表）、`d5c8b1f4a2e7`（文件元数据）、`c3f8a1b27e04`（sync_cursors）、`b7e2d94c1a05`（jobs.warnings）；读写端点均走数据库，响应形状与契约一致，**契约 v0.1 端点已全部实现、无 501 stub，并经 B23 终检**（`docs/B-收官交付.md`）。计划 preview/apply/revoke 接入智能体 D 的计划引擎（`packages/learning`，editable 安装）；解析走 D 的 `parse_document`（B5，含导入后自动派发）、任务生成走 D 的 `build_tasks_json`（B13/D12）。真实数据链路：A21 实测快照（冻结证据 sha256 钉死、指纹 323afc775e093580）经端点 → 真 worker 全链落库、55 项核对全过（B17，`tools/b17_replay.py` 与 `docs/B17-真实同步回放记录.md`）。收官项全部完成：B18 会话注入边界、B19 ICS 导出（CR005）、B20 `parse_status` 漂移关闭（CR005）、B21 部署与运行材料（`docs/部署与运行说明.md`，含备份恢复与全新环境实测）、B22 隔离复核 + B23 契约终检（`docs/B-收官交付.md`）。测试基线：后端 158、learning 257 全绿（0 skipped）。

已实现（前缀 `/api/v1`）：

| 端点 | 说明 |
| --- | --- |
| `GET /health` | 存活探测，`{status:"ok", version, time}` |
| `GET /courses`、`/courses/{id}` | 课程列表/详情（`cursor`/`limit` 游标分页） |
| `POST /courses/{id}/source-refs` | 手动绑定平台来源；重复绑定本课幂等 200，已绑其他课 → 409 `CONFLICT` |
| `GET /courses/{id}/resources`、`/assignments`、`/recordings` | 课程下资源/作业/回放 |
| `GET /resources/{id}`、`/resources/{id}/versions` | 资源详情与版本链（版本号降序，`cursor`/`limit`）；`chunk_count` 为派生字段（ready 时=当前版本片段数） |
| `GET /resources/{id}/chunks` | 解析片段列表（仅当前版本，按 ordinal 升序，`cursor`/`limit`） |
| `GET /assignments`（分页，`due_before`/`due_after`/`include_archived`）、`/assignments/{id}` | 作业（全局与详情） |
| `GET /tasks`（`course_id`/`status`/`due_before`/`include_archived` + 分页）、`/tasks/{id}` | 学习任务 |
| `POST /tasks` | 创建手工任务（201；`due_precision` 未给时按 `due_at` 推断） |
| `PATCH /tasks/{id}` | 更新（可选 `If-Match: "<revision>"`，不匹配 412 `REVISION_MISMATCH`；每次更新 revision+1） |
| `DELETE /tasks/{id}` | 归档（204，任务从默认列表消失，`include_archived=true` 可见） |
| `POST /tasks/{id}/lock` | 锁定/解锁生效计划中的任务块（`starts_at` 缺省=该任务全部块） |
| `POST /tasks/{id}/feedback` | 学习反馈（201；`Idempotency-Key` 支持，24h 重放返回首次结果，键复用换体 → 409） |
| `POST /tasks/build` | 手动触发生成候选学习任务（202 queued `build_tasks` job；请求体可省，`course_id` 限定课程范围，缺省全局；同范围已有 queued/running 任务时去抖复用）。资源/作业/回放（+既有任务）经 D3 规则路径生成，按稳定匹配键（`match_key`）落库：重复触发不造重复行、无变化不递增 revision、用户修正（耗时/状态）保留、归档任务不复活。`job.warnings` 携带合并/回退告警（如 `DUPLICATE_CONTENT`/`TASK_VERSION_UPDATED`/`EXISTING_TASK_UNMATCHED`），`result_refs=[{type:"task", id}]` 为本次新建/更新的任务 |
| `GET /preferences/availability` | 可用时间与每日限额 |
| `POST /plans/preview` | 生成候选计划（201；`horizon_start/horizon_end` 缺省=今天起 14 天（用户时区），`respect_locked` 默认保留生效计划的锁定块） |
| `POST /plans/{id}/apply` | 应用候选（200；输入已变 → 409 `PLAN_STALE`；旧生效计划 → superseded；返回 `previous_plan_id`） |
| `POST /plans/{id}/revoke` | 撤销生效计划（200；恢复被其取代的上一版为生效，返回 `restored_plan_id`） |
| `GET /plans`（按版本降序分页，`status` 过滤）、`/plans/current`、`/plans/{id}` | 计划；`current_input_revision` 为实时输入指纹，与 `plan.input_revision` 不同即计划已过期 |
| `GET /connections`、`/connections/{id}`、`/connections/{id}/capabilities` | 平台连接（列表/详情/能力；能力由适配器写入，样例仅 conn_2a9f；`entry_url` 为平台权威登录入口常量，CR001 降级口径） |
| `POST /connections/{id}/sync` | 触发同步（202 返回 queued 的 sync job；支持 `Idempotency-Key`；同连接已有 queued/running 的 sync 任务时去抖返回原任务）。适配器经进程内注册表或 `STUDY_SYNC_ADAPTERS` 解析（B 原生函数或 A3 协议类自动桥接）：逐课程按 `(platform, external_id)` upsert，指纹相同不升版本、`content_hash`/`source_updated_at` 变化才递增版本；删除仅在 `explicit_delete_signal` 时软归档；游标落库续传；条目错误隔离；适配器回传的 `details` 落库前递归剔除会话/凭据键（B18）。disabled → 409 `CONFLICT`；auth_required → 409 `AUTH_EXPIRED`（`retryable=false`，details.action=user_login） |
| `POST /connections/{id}/check` | 触发一次连接检查并回读结果（200 Connection；状态由检查结果驱动，不作假 ok）。检查适配器解析同 sync（`check_connection` 面，类/实例自动桥接）；disabled → 409 `CONFLICT`；适配器未接入 → 502 `DEPENDENCY_UNAVAILABLE`（`retryable=false`）；状态映射 ok/auth_required/network_error/blocked，auth_required 时 `last_error`=`AUTH_EXPIRED` + `recovery_action`（无 action 时回退平台 `entry_url` 常量） |
| `GET /jobs`（创建时间倒序分页，`status`/`type` 过滤）、`/jobs/{id}` | 后台任务历史与详情 |
| `POST /imports` | 手动导入（multipart：`payload` JSON 字符串 + `files` 1..N 个 pdf/pptx/docx/zip；.zip 课件包仅存档与下载、解析终态 `skipped`，CR006），202 返回 queued 的 import job；支持 `Idempotency-Key`；扩展名+魔数校验、大小上限；附件落 `var/files`（blob 路径由 `fil_*` 派生），DB 只存元数据，本地路径不外泄。受理后的 import 任务收尾为待解析资源自动派发 `parse` 任务（同资源同版本去抖） |
| `POST /jobs/{id}/cancel` | 取消任务：queued → 直接落 `cancelled`（条件更新防与 worker 抢占竞态）；running → 置 `cancel_requested`（协作式，响应可能仍 running，处理器在检查点退出）；终态 → 409 `CONFLICT`；未知 → 404 |
| `POST /jobs/{id}/retry` | 重发失败任务（202 新 job；支持 `Idempotency-Key`；仅 failed/partial_failed，其他状态 409；原任务不动）。partial_failed 仅重试失败条目：按 `errors[].details.file_id`/`course_id` 过滤原 `payload`（sync 重试仅含失败课程） |
| `POST /connections/{id}/disable`、`POST /connections/{id}/enable` | 停用/启用连接（均幂等 200）：disable 置 `disabled` 并清 `recovery_action`（保留 `last_error`）；enable 解除停用并按检查适配器立即刷新状态（缺失 → `network_error`+`DEPENDENCY_UNAVAILABLE`，结果非法 → `network_error`+`INTERNAL_ERROR`，不困在 disabled、不造假 ok） |
| `GET /files/{id}` | 下载受控文件（`FileResponse`；元数据与 blob 均存在才 200，否则 404）；本地路径不外泄；下载名走 RFC5987 `Content-Disposition` |
| `GET /search` | 解析片段检索（`q` 必填非空；`course_id`/`kind` 过滤；范围=未归档资源且 `parse_status=ready`、片段属当前版本；大小写不敏感子串，`%`/`_` 按字面量；`score`=命中字符数占比，降序 + chunk id 升序 keyset 分页） |
| `POST /exports` | 导出数据（202 queued export job；支持 `Idempotency-Key`）。`format` 枚举 `[json, ics]`（缺省 json；B19/CR005）：json 按 `include` 选 7 类：courses/resources/assignments/recordings/tasks/plans/feedback（缺省全部）；ics 为生效计划时间块 + 任务截止的 iCalendar（RFC 5545：用户时区墙钟渲染、全天事件 DATE、unknown 截止不导出、75 八位组折行+TEXT 转义），带 `include` → 400。worker 生成产物落 `var/files`，`result_refs=[{type:"export_file", id}]`；取消时不残留孤儿文件 |
| `POST /resources/{id}/reparse` | 重新解析（202；去抖复用同资源同版本排队中的 parse 任务；资源无 `file_id` → 409 `CONFLICT`；旧解析产物在新任务成功前保持可用） |

统一约定：

- 错误信封：所有非 2xx 均为 `{"error": {code, message, retryable, details, request_id}}`（契约 error.schema.json）；`X-Request-ID` 请求头透传，未带则生成。参数校验失败（含非法游标）返回 400 `VALIDATION_FAILED`。
- 契约 v0.1 端点已全部实现，无 501 stub；未知路径与不支持的方法由统一错误信封返回 404 `NOT_FOUND` / 405 `METHOD_NOT_ALLOWED`。
- 游标分页：`cursor` 为不透明 base64；普通列表按主键升序 keyset，计划列表按版本降序、资源版本按版本号降序、片段按 ordinal 升序、任务列表按创建时间倒序（复合游标）；`limit` 默认 50、上限 200。
- 时间统一 UTC 存储与返回（`Z` 结尾）；来源平台 ID 只在 `source_refs.external_id`。
- 幂等：`Idempotency-Key`（8-128 字符）按键+端点+请求体指纹去重，TTL 24h；见 `idempotency_records` 表。
- 计划引擎边界换算（`app/plan_engine.py`）：契约 weekday 1-7 ↔ 引擎 0-6；`date_exceptions.windows=[]` → 当日不可用；`in_progress` 按 `pending` 排程；已取消/已归档任务不入排且其前置依赖被剔除；horizon 含首尾（引擎半开区间 +1 天）。
- 日志脱敏（`app/log_redact.py`）：记录前剔除 URL 查询参数（uvicorn access 日志已挂 filter），防平台 JWT/票据等经查询串进日志；自写日志记录 URL 时用 `strip_query()`。
- 会话注入边界（B18）：同步适配器回传的 `details`（条目错误/任务错误）在落库前递归剔除含 cookie/token/session/password/credential/authorization/secret 的键（`app/sync_engine.py` `scrub_session_material`）；登录与恢复动作只下发 `entry_url`/`recovery_action` 许可字段，任何响应体、job 结果与日志均不含会话物料（回归 `tests/test_b18_session_boundary.py`、`tests/test_login_alignment.py`）。
- 解析流水线（`app/worker.py` 的 `parse` 任务，B5）：读受控文件 → `learning.parse.parse_document`（D2，纯函数；单文件大小上限经 `ParseLimits` 传 `config.MAX_PARSE_BYTES`，超限按 `FILE_TOO_LARGE` 失败、不可重试——PD-1）→ **替换式**写入 ContentChunk（重放/重解析幂等），provenance 记 extractor/extractor_version/job_id（`fact_source=platform`）；`parse_status=ready` 时 `chunk_count`=当前版本片段数（可为 0），否则 null；`extent` 仅 pdf 页数/pptx 幻灯片数（契约枚举）由解析统计派生。契约 `parse_status` 枚举无 needs_ocr：**needs_ocr 按 failed + 错误码 `NEEDS_OCR`（可重试）如实落库，不写成成功**（B20/CR005 明文裁定保留该映射）；可入库但不解析的类型（如 link、.zip 课件包）终态 `skipped`——"不解析"不写成失败、也不伪造成功（CR006），原因保留在任务 errors（`UNSUPPORTED_MEDIA_TYPE`，不可重试）；坏文件等失败保留可读错误与诊断 ID（任务 errors[].details.job_id）。版本已有旧产物时重解析不置 processing、失败也不改动旧产物（“旧解析产物在新任务成功前保持可用”）。解析结果缓存键含 content_hash+解析器/库版本，存 `var/files/parse-cache`（gitignore；失败不缓存，provenance 每次现盖）。解析成功后按资源所属课程自动派发 `build_tasks`（B13；失败不派发，避免估算锁定在默认值）。
- 任务生成（`app/worker.py` 的 `build_tasks` 任务 + `app/build_engine.py`，B13/D12）：DB 行按契约形状直传 → `learning.taskgen.build_tasks_json`（D12 集成入口：纯函数、JSON 进出、契约行可直传）→ 候选任务落库。稳定匹配键写入 `LearningTask.match_key`，行主键仍为不透明 `tsk_*`；`existing_tasks` 传该课程全部任务行、分流由 D 封装承担（归档忽略不复活、手工任务原样保留、稳定键对齐并保留人工修正，来源消失告警 `EXISTING_TASK_UNMATCHED`）；契约字段 `fact_source` 与 D 键 `estimate_provenance` 桥接回传（重建不冲掉用户/平台来源标记）。告警原样落 `job.warnings` 供展示；参数取自 `contracts/config-defaults.json` 的 `taskgen` 段。

## 本地运行（Windows）

```bat
rem 在仓库 worktree 根目录（如 D:\ai\.worktrees\agent-b）执行：
cd apps\api
python -m venv .venv

rem 必须从 apps/api 目录执行安装：requirements.txt 内 -e ../../packages/learning
rem 为相对本文件的路径；[parse] extra 提供 pypdf（缺依赖时环境自检显式失败，见"依赖说明"）
.venv\Scripts\python -m pip install -r requirements.txt

rem 建表 + 装载契约样例数据（会清空现有数据，仅开发用）
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python -m app.seed

rem 启动（数据库默认 ..\..\var\study.db）
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000

rem 启动后台 worker（二选一）：
rem   A) 独立进程（推荐，与 API 分开）：另开一个终端
.venv\Scripts\python -m app.worker
rem   B) API 进程内守护线程（本地开发便捷）：
set STUDY_WORKER_INPROC=1 && .venv\Scripts\python -m uvicorn app.main:app --reload --port 8000

rem 后端测试（自动使用 var\test-study.db，不触碰开发库；期望 0 skipped）
.venv\Scripts\python -m pytest -q

rem 计划引擎测试（packages/learning，与后端共用同一 venv；含 PDF 用例，期望 0 skipped）
cd ..\..\packages\learning
..\..\apps\api\.venv\Scripts\python -m pytest -q

rem 连接器套件（connectors，跨模块；依赖声明见 connectors\requirements.txt，requests 亦已入本 requirements.txt）
cd ..\..
apps\api\.venv\Scripts\python -m pytest connectors\tests -q
```

打开 http://127.0.0.1:8000/docs 查看 OpenAPI 文档。

备份与恢复（SQLite 数据 + 附件目录，步骤已实测一次）、环境变量样例汇总、全新环境验证记录与已知限制/遗留风险清单见 `docs/部署与运行说明.md`（B21）。

环境变量（均可选）：

- `STUDY_DATABASE_URL`：数据库 URL，默认 `sqlite:///…/var/study.db`；
- `STUDY_EXAMPLES_DIR`：契约样例目录，默认仓库根下 `contracts/examples`（seed 数据源）；
- `STUDY_CONFIG_DEFAULTS`：排程默认参数文件，默认仓库根下 `contracts/config-defaults.json`；
- `STUDY_FILES_DIR`：上传附件存储目录，默认仓库根下 `var/files`（gitignore）；
- `STUDY_MAX_UPLOAD_MB`：单文件大小上限（默认 100）；`STUDY_MAX_IMPORT_FILES`：单次导入文件数上限（默认 20）；
- `STUDY_MAX_PARSE_MB`：单文件解析上限（默认 64，十进制 MB；PD-1 协调者代裁——真实大 PDF（42.9MB）此前会被 30MB 护栏永久拒绝，超限解析任务按 `FILE_TOO_LARGE` 失败、不可重试）；
- `STUDY_WORKER_INPROC`：置 `1` 时在 API 进程内启动守护线程 worker（默认关闭，推荐独立 worker 进程）；
- `STUDY_WORKER_POLL_SECONDS`：worker 空转轮询间隔秒数（默认 1.0）；
- `STUDY_SYNC_ADAPTERS`：同步适配器映射（JSON，如 `{"xzzd": "module:attr"}`；attr 可为 B 原生可调用 `fn(connection_info, request)` 或 A3 协议 `PlatformAdapter` 类，自动实例化并桥接；未配置时仅进程内注册的适配器可用）。

依赖说明：`requirements.txt` 通过 `-e ../../packages/learning[parse]` 以 editable 方式安装智能体 D 的计划引擎与解析模块（`zju-learning`，`[parse]` 附带 pypdf 用于文本型 PDF 解析；PPTX/DOCX 解析为纯标准库）。**必须从 `apps/api` 目录执行 `pip install -r requirements.txt`**：`-e` 路径相对本文件解析，从其他目录安装会失败或装错；新 venv 的完整初始化步骤见上一节（2026-10-10 已按该步骤在全新 venv 实测：安装/迁移/seed/测试/启动全通，见 `docs/部署与运行说明.md`）。`apps/api/tests/test_env_integrity.py` 做环境完整性自检（断言 `pypdf`、`learning.parse`、`jsonschema`/`referencing` 可导入）：缺依赖时**显式失败**并给出安装指引，不依赖 `pytest.importorskip` 静默跳过（P1 整改——此前 pypdf 缺失时 D 的 4 项 PDF 测试会假绿掩盖未测试）。Windows 下同时安装 `tzdata` 以支持 `ZoneInfo`；multipart 上传依赖 `python-multipart`（已入 requirements）；`jsonschema` 供测试按 `contracts/schemas` 校验响应体。

跨模块依赖：`connectors/`（智能体 A 的平台连接器）测试与真实联调依赖已声明于 `connectors/requirements.txt`（X5 补：`requests`/`jsonschema`/`referencing`；缺 `requests` 曾致连接器套件收集期整体中断），`requests` 亦已入本 `requirements.txt`（开发/测试依赖）装入同一 venv；`apps/web`（智能体 C）为 Node 工程，见其自身 README。

注意（中文 Windows）：`alembic.ini` 由 configparser 按系统区域编码（GBK）读取，必须保持纯 ASCII，非 ASCII 注释会导致启动崩溃；Python 源码（UTF-8）不受影响。

## 目录结构

```
apps/api/
├─ app/
│  ├─ config.py        # 环境变量与路径（REPO_ROOT/DATABASE_URL/EXAMPLES_DIR/CONFIG_DEFAULTS_PATH）
│  ├─ db.py            # 引擎、SessionLocal、Base、get_db
│  ├─ models.py        # 领域模型（契约对齐，to_contract() 输出契约形状）
│  ├─ schemas.py       # 请求体模型（extra=forbid，与 OpenAPI 对齐）
│  ├─ repo.py          # source_refs 取用、游标分页、404、ID 生成、幂等、If-Match
│  ├─ storage.py       # 受控文件存储（blob 落 var/files，文件名不参与路径，魔数校验）
│  ├─ worker.py        # 后台 worker 与 job 状态机及处理器（import/parse/sync/export；DB 队列、协作式取消、崩溃恢复；python -m app.worker）
│  ├─ sync_engine.py   # 同步引擎（B3）：适配器解析/桥接、sync-result 校验与应用、游标存取、连接状态标记、会话料剔除（B18）
│  ├─ ics.py           # ICS 渲染（B19）：RFC 5545、用户时区墙钟/date 全天事件/折行转义
│  ├─ platforms.py     # 平台常量（B12）：登录入口 entry_url（CR001 裁定 DECISION-3）
│  ├─ log_redact.py    # 日志脱敏：记录前剔除 URL 查询参数（uvicorn access 已挂 filter）
│  ├─ plan_input.py    # 计划输入指纹 compute_input_revision（current_input_revision）
│  ├─ plan_engine.py   # 计划引擎接入：DB→D 引擎映射（边界换算）与候选计划落库
│  ├─ errors.py        # ApiError 与统一错误信封
│  ├─ seed.py          # 装载 contracts/examples 样例到数据库（python -m app.seed）
│  ├─ main.py          # 入口：request-id 中间件、异常处理器、路由装配（404/405 统一信封）
│  └─ routers/         # health/courses/resources/assignments/tasks/plans/preferences/connections/imports/exports/jobs/files/search
├─ migrations/         # Alembic（versions/：核心表 00a0513ded7f、反馈表 ff82c52062dc、plan_blocks 复合主键 b41c9e02f7a3、解析片段 c9f4a7e21b58、文件表 d5c8b1f4a2e7、同步游标 c3f8a1b27e04、job.warnings 列 b7e2d94c1a05）
├─ tests/              # pytest（TestClient + seed 夹具；含环境自检 test_env_integrity、契约一致性 test_contract_conformance + 共享校验器 contract_support、全链路 test_full_chain、B18 会话边界、B19 ICS、PD-3 排程过滤）
├─ tools/              # B17 回放工具 b17_replay.py（A21 冻结证据 → 独立回放库全文链核验 55 项，不碰开发库）
├─ docs/               # B 侧记录：B17 真实同步回放、部署与运行说明（B21）、B-收官交付（B22/B23）、转答
├─ alembic.ini
└─ requirements.txt
```

## 路线图（M1–M3 B 侧；任务书 `docs/coord/tasks/B-M1任务书.md`、`docs/coord/tasks/最终轮-M2M3收官任务书.md`）

- **B1（完成）**：`POST /imports` 幂等上传通道——multipart、类型/大小校验、受控文件存储、202 job。
- **B2（完成）**：后台 worker 与 job 状态机——`app/worker.py`：DB 队列轮询、六态流转（queued/running/succeeded/partial_failed/failed/cancelled）、条目级提交与逐项 errors、协作式取消（`cancel_requested`）、崩溃恢复（启动时 running→queued，按 file_id 幂等重放）、`payload` 保留原参数支持重试；HTTP 内不阻塞。import 处理器：文件→资源（+v1 版本+来源引用）。
- **B3（完成）**：`POST /connections/{id}/sync` 接 A 的 sync-result 信封——适配器解析（注册表/`STUDY_SYNC_ADAPTERS`，A3 协议类自动桥接）、upsert 幂等（指纹相同不升版本）、仅 `explicit_delete_signal` 软归档、游标持久化、条目级错误隔离、同连接任务去抖、连接状态标记（ok/auth_required/network_error）。
- **B4（完成）**：补齐 501——cancel（queued 直接落终态/条件更新防抢占，running 协作式）/retry（failed/partial_failed 重发新 job，partial 仅重试失败条目）/disable/enable（幂等；enable 按检查结果刷新状态，适配器缺失如实降级）/`GET /files/{id}`（受控下载）/`GET /search`（片段检索，score 降序 keyset 分页）/`POST /exports`（7 类数据 JSON 导出，产物走受控文件存储）/`POST /resources/{id}/reparse`（去抖复用排队任务）；至此契约 v0.1 端点全部实现。
- **B5（完成）**：解析流水线产出 ContentChunk——`parse` job 接 D2 `parse_document`（pdf/pptx/docx，纯函数、含内容缓存）；导入收尾自动派发（同资源同版本去抖）；片段替换式落库（可取消、重放幂等）；provenance 记 extractor/extractor_version/job_id；`chunk_count`/`extent` 派生；needs_ocr 按 failed+`NEEDS_OCR` 如实落库；旧产物在重解析成功前保持可用。
- **B6（完成）**：v0.1 契约正式冻结（收齐 A/C/D 三方回执 + `CHANGE-REQUEST.md` 模板），见 `contracts/CHANGELOG.md` 回执索引。
- **B12（完成，CR001 降级口径）**：登录回跳本期不启用（A12 裁定两平台均不可回跳）——`POST /connections/{id}/check` 触发检查并回读（状态由检查结果驱动，不作假 ok）、`Connection.entry_url` 常量下发（服务端平台入口）、`app/platforms.py`；完整回跳（state/白名单/回调）保留待平台登记后启用，见 `contracts/reviews/2026-10-09-CR001-登录回跳.md`。
- **B7–B11（完成）**：开发环境盘点（本 README"本地运行"一节）、权限与隔离测试、契约一致性自测、部署恢复材料与模型配置位、手动闭环集成测试（`tests/test_full_chain.py`）。
- **B13（完成）**：任务生成接入 worker——`build_tasks` 处理器 + parse 成功自动派发 + `POST /tasks/build`；详见上文"任务生成"节。
- **B14（完成，CR002 结案）**：登录/连接建立端点收尾（与 A14 对齐）——受控浏览器会话在本地连接器内、本 API 只调度并记录检查结果（`POST /connections/{id}/check`），登录入口常量 `entry_url` 双用途（连接器打开受控浏览器主路径 + 网页端"前往登录"兜底）；`recovery_action.message` 定稿"登录已失效，请在连接器窗口完成登录后重试"，`AUTH_EXPIRED.retryable=false`；回归 `tests/test_login_alignment.py`（entry_url 常量、动作形状、全响应无会话物料、https-only）；契约补录见 `contracts/reviews/2026-10-10-CR002-受控浏览器会话形态.md`。
- **B15（完成）**：测试环境 fail-fast——`tests/test_env_integrity.py` 环境完整性自检（缺 pypdf/jsonschema 等显式失败）+ 文档初始化步骤；详见"依赖说明"。
- **B16（完成）**：契约-实现一致性回归——`tests/test_contract_conformance.py` 用共享校验器（`tests/contract_support.py`，亦可 `python tests/contract_support.py <schema 文件名> <响应.json>` 独立运行）把实际响应体对 `contracts/schemas/` 逐实体校验（含 B13 `build_tasks` job 与 404/405/400/409 错误信封），并做契约↔实现路由集合双向对账（`test_openapi_route_coverage_bidirectional`：缺项=联调 405/404 风险，多出=漂移，须 CR 补录）；契约 v0.1 已冻结，只允许兼容新增，结构变更走 `contracts/reviews/CHANGE-REQUEST.md`。
- **C2 联调偏差整改（完成，2026-10-10）**：C2 真实模式联调（`apps/web/docs/C2-真实模式联调记录.md`）交 B 的 4+1 项偏差全部闭环——①`PUT /preferences/availability`（全量替换 + `updated_at` 刷新参与计划输入修订；`GET` 未设置时返回默认值，不再 404）；②补齐写端点 `POST /courses`、`POST /assignments`、`PATCH /assignments/{id}`（If-Match 修订校验；`due_at`/`due_precision` 双向一致性，清空 due 自动 `unknown`，写入路径 `fact_source=user`）；③`PLAN_STALE` details 对齐（`{plan_id, input_revision, current_input_revision}`）；④`AUTH_EXPIRED` details 对齐（`{platform, action}`）与 `VALIDATION_FAILED` details（`{fields:[{field, reason}]}`）；路由对账反向多出的 `GET /tasks/{task_id}` 按 CR004 兼容新增补录契约。回归见 `tests/test_write_api.py`（写端点）与 `tests/test_login_alignment.py`（登录对齐）。
- **B17（完成，2026-10-10，P0）**：真实同步回放——A21 冻结的真实 `sync-result`（sha256 钉死；指纹 `323afc775e093580` 独立复算一致）经 `POST /connections/{id}/sync` 端点 → 真 worker 全链落库，55 项核对全过：R1 首轮落库字段逐项一致 / R2 二次同步幂等（不升版本、刷新 observed_at、游标续传）/ R3 去抖+端点幂等（同键逐字节重放、无键合并同 job）/ R4 单项隔离（派生损坏 → partial_failed，其余照常）/ R5 真实失败回放（`RESOURCE_NOT_FOUND` retryable=false、连接不误标）。工具 `tools/b17_replay.py`（独立回放库 `apps/api/var/b17-replay.db` 每次重建，不碰开发库）；记录 `docs/B17-真实同步回放记录.md`；D15 输入已导出（`apps/api/var/b17-a21-export/`）。
- **B17 连带 CR006（完成）**：.zip 课件包入库（/imports 白名单放宽 + 魔数 PK）+ parse 终态 `skipped`（"不解析"不误标失败、不伪造成功）；`contracts/reviews/2026-10-10-CR006-zip课件包入库与skipped口径.md`。
- **B18（完成）**：会话注入边界收口——适配器回传 `details` 落库前递归剔除会话/凭据键（`sync_engine.scrub_session_material`），补 4 项边界测试（`tests/test_b18_session_boundary.py`：递归清洗、落库前清洗、job/连接面清洗、同步响应与日志面无会话物料）。
- **B19（完成，CR005）**：ICS 导出——`POST /exports` `format` 枚举 `[json, ics]`，RFC 5545（用户时区墙钟渲染、全天事件 DATE、unknown 截止不导出、75 八位组折行 + TEXT 转义）；渲染 `app/ics.py`；测试 `tests/test_b19_ics_export.py`；契约补录 CR005。
- **B20（完成，CR005 结案，漂移正式关闭）**：`parse_status` 保留 needs_ocr → `failed`+`NEEDS_OCR` 映射（方案①），并在契约描述（`contracts/schemas/resource.schema.json`）显式写明；不再悬置。
- **PD-1/PD-2/PD-3（完成，协调者代裁落地，`docs/coord/decisions/01-产品决策裁定.md`）**：解析上限 64MB 单一常量（`config.MAX_PARSE_BYTES` / `STUDY_MAX_PARSE_MB`）；mp4 等视频 `kind=other` 映射 `watching` 全链路验证；已提交作业保留生成但标注平台已提交且不排程（`tests/test_pd3_submitted_exclusion.py`）。
- **B21（完成，2026-10-10）**：交付材料——`docs/部署与运行说明.md`：部署与运行（本地单机 SQLite）、环境变量样例、**备份恢复步骤并实测一次**（PASS：两种备份方式 integrity ok + 计数一致 + blob sha256 一致；恢复后服务启动、课程/资源/文件下载 HTTP 全链验收）、全新环境按 README 启动并跑通全部测试（新 venv：迁移 7 个 revision、seed、api 158 / learning 257 全绿、uvicorn 冒烟通过）、已知限制与遗留风险清单。
- **B22（完成，2026-10-10）**：权限与隔离复核——单用户本地形态声明；本地路径不外泄（`fil_*` 白名单 + 下载 404 边界）；会话/凭据物料不下发网页/不落库/不进日志（B18 + 登录对齐 + 日志脱敏测试证据）；开发数据仅经显式 `python -m app.seed` 装载。证据汇总见 `docs/B-收官交付.md` §B22。
- **B23（完成，2026-10-10）**：契约-实现一致性终检——`contracts/tools/validate_contracts.py`（19 schema + OpenAPI 结构）、`contracts/examples/validate_examples.py`（17 文件 / 61 对象）、路由集合双向对账（`tests/test_contract_conformance.py::test_openapi_route_coverage_bidirectional`，并入 158 全绿）三路通过；契约 v0.1 冻结状态与 CR001–CR006 记录一致（`contracts/CHANGELOG.md`）。结果见 `docs/B-收官交付.md` §B23。
