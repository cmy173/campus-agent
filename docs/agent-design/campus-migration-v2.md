# 浙大连接器与历史数据迁移基础

日期：2026-10-14。已完成可运行迁移增量；真实学校登录试用和历史资料的完整领域转换仍待完成。本文不替代整体 Goal 验收。

## 已迁入的实现

校园协议实现位于 `connectors/campus`，不导入旧 `campus_agent` 包，也不启动旧服务器。原有纯协议路径暂为模块别名，用来让已有回归测试直接测试迁后的实现；不保留两份协议实现。

| 旧能力 | 新实现 | 使用方式与范围 |
| --- | --- | --- |
| CAS、ZDBK、ETA 与身份核对 | `connectors/campus/campus*.py`、`connectors/campus/connectors` | 本科教务菜单、课表、成绩、本人资料；保留换票、验证码、错误分类、分平台会话与缓存合并规则 |
| 校历、课程安排、学分/绩点 | `connectors/campus/{academic_defaults,academics,calendar_sources,calendar_extract}.py` | 学期/校历来源、默认值与缓存分别声明；不自行把未知日期变成确定日期 |
| 校级通知与学院正文 | `connectors/campus/connectors/{notices,college_notices}.py` | 校级入口仅近期摘要；学院专用来源目前只有竺可桢学院，其他学院明确 unsupported，不猜网址 |
| WebVPN | `connectors/campus/webvpn_adapter.py`、`CampusWorkspace.webvpn_entry` | 迁入既有认证/令牌映射；修正 Cookie processor。产品采用持久浏览器登录与代理页面操作，未实现 Python 教务透明代理，也没有宣称旧自动同步已实现这种代理 |
| 通知进入学习资料 | `apps/api/app/campus_domain.py` | 缓存通知正文进入现有 Resource/版本/片段，支持材料检索、任务生成、复习业务；附件/图片未读声明保留 |
| 历史私有数据 | `apps/api/app/campus_migration.py`、`tools/migrate_campus_data.py` | 只读源库、明确单个 owner 映射、原始记录与文件字节可重入导入、受保护回滚 |

新增依赖 `cryptography==50.0.2`、`cffi==2.1.1`、`pycparser==3.11` 已进入 API 的版本锁和 Windows/Python 3.12 wheel 哈希锁。新连接器只迁入 WebVPN 线上协议加密函数，不迁入旧设备盐/密码解密体系。

## 身份、缓存与浏览器接入

新系统是单人本机工作区，校园服务内部 owner 固定为 `local`，不复用旧本地用户名、密码散列或学校 Passport 用户表。旧账号映射在导入批次中保留，学校身份来自实际连接验证。

校园独立数据位置为 `$CAMPUS_AGENT_HOME/campus/workspace.sqlite3`，与领域数据库及原始旧库分离。包含校园缓存和历史导入记录，不创建旧登录/用户/凭据表。应用启动不会自动选择旧 owner，不写旧数据库。

`get_campus_workspace().adopt_browser_cookies(cookies: list[dict]) -> dict` 是私有 Python 接入函数，不能注册为模型工具，也不能接受公网 JSON Cookie 上传。父进程从自身受信浏览器读取 Cookie 后调用。函数仅接收 CAS/ZDBK/ETA 和 `.zju.edu.cn`；父域限定到 CAS，不复制其他网站 Cookie。先进行真实教务换票、菜单读取和可用的本人身份核对，成功后才替换当前连接；已有学生 ID 不能被其他或无法确认的身份覆盖。返回状态不含 Cookie。

浏览器配置保存登录态；HTTP 连接器 Cookie 仅驻内存。连接器进程重启后需从浏览器再衔接，不表示学校永不过期。失败保留之前连接和校园缓存。用户明确清除学校缓存后才可以切换学校身份。

精确挂载点：

```python
# apps/api/app/main.py，路由装配期（不是 lifespan 内）：
from .routers.campus import install_campus_router
install_campus_router(app, agent.browser_owner)
# 默认 /api/v1/agent/campus，复用网关身份、Origin 与 Cookie 规则

# lifespan finally
from .campus_workspace import close_campus_workspace
close_campus_workspace()
```

挂载放在装配期是集成验收的结论：lifespan 内挂载会让运行时与 `app.openapi()`/契约导出看到
不同路由表，契约回归会把正常路由误报为实现漂移（本次实际发生并已修复）。`app/__init__.py`
在包初始化时调用 `config.ensure_repo_root_on_path()`，使 `connectors.campus.*` 按仓库根解析，
测试与生产同一路径。

浏览器私有 Cookie 导出与上述函数的产品接线，由主集成增量负责；本文测试未假装此接线已经完成真实登录。

## 8 个新增领域工具

清单已冻结，当前全部领域工具总数 24。SDK 暴露的模型名均为 `campus_` 加下面名称的点改下划线，例如 `campus_campus_query`。

| 名称 | 参数 | 结果与行为 |
| --- | --- | --- |
| `campus.status` | 无 | 学校连接、缓存与官方登录入口；不联网 |
| `campus.query` | capability、query、可选 platform | 缓存 courses/grades/profile/notices/college_notices，保留完整性、时间与错误 |
| `campus.sync` | 同 query | 只读访问校方并写本地缓存；未登录返回 AUTH_REQUIRED，失败保留原缓存 |
| `campus.academics` | 可选 day、term | 基于已获取课表/校历和成绩的课程安排与统计 |
| `campus.notice.read` | record_id | 读取先前展示的通知正文与来源，不接受调用者任意 URL |
| `campus.notice.import` | course_id、record_id | 将已有缓存通知正文写入明确课程，重复请求复用、正文变化新增版本；本地操作自动执行 |
| `campus.imported.read` | 无；或 batch_id + document_id | 列批次/资料 ID，读原始片段、任务、来源和整理记录 |
| `campus.webvpn.entry` | service=cas/zdbk | 返回固定登记代理入口，交给浏览器导航；映射需真实门户复核 |

platform 为 `zdbk`、`eta`、`zdbk_public`、`ckc_public`，并受 capability 合法组合限制。model 无法传入 owner、密码、Cookie 或任意域名。内层 query 使用迁入的严格 Pydantic 契约；畸形参数返回明确错误。

典型通知链路：status → 浏览器登录/私有衔接 → sync(profile) → sync(college_notices) → notice.read → notice.import(明确课程) → materials.read/search → tasks.build。通知来源日期不被导入时刻替换；只导入正文不意味着附件已经解析。新版本保留旧片段、旧版本，材料读取以版本检查防止过期依据。

## 实际旧库清点与映射

本次仅只读打开 `data/campus.sqlite3`：3 个旧 owner、4 个 documents、61 个 chunks、2 个 tasks、1 个 document_sources、1 个 notice_analyses、1 个校园 snapshot、1 个 connector cache、0 个校园凭据。原库没有独立笔记、反馈或个人安排表；不得为不存在的数据臆造迁移。新领域数据库中的反馈/计划仍由新系统保留。

| 源表 | 处理 |
| --- | --- |
| documents | 原始字段、错误/警告、内容 SHA、完整 BLOB 字节保留；新下载入口可取回原文件 |
| chunks | document、position、locator、text 原样保留，按 position 展示 |
| tasks | 原始截止、原始日期文本、state/reviewed、证据、时间精度、来源与附件字段原样保留；不把 pending 自动当作学校尚未提交 |
| document_sources | origin、学院、正文 hash、附件、未读标记原样保留 |
| notice_analyses | 全部整理结果、状态、诊断、模型/提示版本及用量原样保留 |
| campus_snapshots / connector_cache | 默认只留历史副本；显式 `--school-cache` 可映射到 local 工作缓存，标为 stale，保留原始获取时间 |
| users/passport_accounts/sessions/campus_credentials/audit | 不复制用户名/密码散列、学校账号标识、会话、凭据或认证审计；源库完整备份仍保留 |
| 未知表 | 列在 unmapped，导入直接拒绝，先做真实字段映射再迁移 |

旧数据当前作为可读历史资料区导入，尚未全部变成普通 courses/materials/learning_tasks。转换还需明确课程关联与时间估计映射；旧 task 没有新任务必需的 estimated_minutes，不能任意补时间而宣称完成。真实旧数据导入仍未执行，先明确选哪个旧 owner；工具已支持受控库完整运行。

## 命令与回滚

以下 `<...>` 必须替换；目标路径必须不同于源路径，不运行旧 Store 构造器。inventory 不输出用户名、学校标识、密码或 Cookie。

```powershell
apps/api/.venv/Scripts/python.exe tools/migrate_campus_data.py inventory --source data/campus.sqlite3
apps/api/.venv/Scripts/python.exe tools/migrate_campus_data.py import --source <已备份源库> --target <目标校园workspace.sqlite3> --owner <旧opaque-owner-id>
# 明确要复用历史学校缓存时加 --school-cache
apps/api/.venv/Scripts/python.exe tools/migrate_campus_data.py rollback --target <目标校园workspace.sqlite3> --batch-id <mig_...>
```

只读事务提供一致逻辑快照，含 WAL。批次键基于该 owner 的非凭据完整内容；同一快照/映射重放不重复记录，原库变化成为新批次。导入原始记录与工作缓存写入同一目标 SQLite 事务；任一冲突整体回滚。目的校园缓存不同则拒绝覆盖，可先只导入历史资料。

rollback 删除该批次历史记录，撤销该批次新增缓存；原已有且相同的缓存保留。若缓存已经被后续同步改变，拒绝回滚，避免丢掉新工作。回滚不删除源库/备份，不删除普通领域资料或已有计划；`campus.notice.import` 产生的普通材料属于独立本地操作，可用领域资源归档机制退出活动资料。源库所有数据始终可从备份取回。

## 证据与剩余项

独立临时库验证：`apps/api/tests/test_campus_migration.py` **9 passed，4.48 秒**。覆盖完整字节、owner 隔离、秘密不复制、精度/待确认状态保留、重复导入、回滚/新缓存保护、未知表阻断、私有 Cookie 域隔离/失败不丢原会话、重启缓存留存而会话不假复活、严格工具参数/API 400，以及通知→实际材料读取→真实任务生成→版本更新。

迁后已有协议回归：**162 passed + 72 subtests，71.67 秒**，执行 `tests/test_campus.py test_campus_profiles.py test_campus_network.py test_connector_service.py test_college_notices.py test_academics.py test_academic_defaults.py`。合成 CAS/教务/学院响应与本地数据库验证，完全没有真实学校账号登录，不能代替真实学校验收。

每次 Python 测试运行前均明确设置 PYTHONUTF8=1、独立 STUDY_DATABASE_URL/STUDY_FILES_DIR/STUDY_SECRETS_DIR/CAMPUS_AGENT_HOME；没有对真实库建表、drop 或导入。新增依赖 wheel 哈希锁已生成 53 项。

下一增量：持久浏览器 Cookie 私有衔接、真实浙大只读试用、历史资料/任务转普通领域与受保护撤销、教务课程映射、其他学院专用来源（或通用浏览器抓取），以及旧服务器/UI最终退役。旧 WebVPN 的原自动同步循环不迁入第二份调度器，应由统一新调度模块调用 campus.sync。
