# 架构设计

2026-10-12 通知整理增量：新增 `notice_contract`（版本化提示词与严格输出校验）、`notice_model`（可替换的提供方适配器，当前 DeepSeek Chat Completions + `json_object`）、`notice_analysis`（应用服务：离线规则与模型整理、身份与版本重校验、候选事项按身份合并）。模型提取适配器与校园平台适配器完全分离：前者只用固定 HTTPS 常量和服务端密钥，不读校园 Cookie，也不受 `CAMPUS_*_NETWORK_POLICY` 影响；后者不变。模型调用在数据库事务外执行，写入前重新校验账号、资料存在性、正文版本与 `run_id`，因此删除资料、切换账号或被更新一次运行取代后的迟到响应会被丢弃。持久层新增 `document_sources`（官方来源、平台记录 ID、正文版本、结构化附件清单）与 `notice_analyses`（按账号+正文版本+提示词版本+模型配置唯一），`tasks` 增量加列并新增 `ignored` 状态。资料与附件文字仍是不可信数据：不改变系统指令、不触发工具、不产生链接，全部候选事项初始待核对。边界、契约、恢复语义、已知限制与三类验证记录见 [通知整理](notice-extraction.md)。

学院通知增量：`CampusService` 在账号锁内从服务端基本信息缓存匹配适配器的 `college_names`，`ckc_public` 只解析固定 WebPlus 列表和正文；`PublicPageTransport` 统一管理无凭据公开页面传输。“待办与截止”页面调用统一 query/sync，显式保存正文时复用已有资料、检索和待办流程。详细边界见 [学院通知](college-notices.md)。

2026-10-12：校园连接器已实现统一 `CampusService.query/sync/connection_status/disconnect`，拆分 CAS、网络、平台协议、数据契约与 SQLite Repository。学业看板及通知摘要 API 使用统一服务，旧登录和分批课表续传保留兼容。增加无校园认证的公开教务通知摘要，真实读取与持久缓存闭环已验证；教务/ETA 本轮只做合成迁移回归。当前职责、依赖图、同步边界和扩展步骤见 [连接器架构与指南](connectors.md)。下面带旧日期的接口建议及进展为历史记录；校园现行错误码和结果以新契约为准。

2026-10-10 当前状态：本机通行证、ETA 六项基本信息及课表已进行一次本人测试账号联调，全套 158 项测试通过；接口超时/拒绝时保留缓存。正式授权、其他账号与整体试点验收仍需独立验证。以下保留原日期的阶段记录。

## 决策

2026-10-09 增量：加入独立的本机校园只读连接。`campus.py` / `campus_routes.py` 管理 CAS、教务会话与同步；`campus_snapshots` 按账号保存白名单字段快照。校园凭据不进入模型或文档检索。正式校方 SSO 注册和真实账号验收尚未完成，详见 campus-login.md。

通行证入口增量：`passport_routes.py` 负责浏览器绑定的匿名登录流程、验证后建立资料身份、首批自动同步及旧资料显式关联；`passport_accounts` 连接通行证登录标识与原 owner。浏览器会话用于默认恢复离线资料，CAS 会话用于实时换取已注册业务站点票据，两者生命周期独立。`static/passport.js` 处理自动续传、离线入口与旧资料绑定，详见 passport-login.md。

v0.2 已采用模块化单体：FastAPI + 原生 Web UI + SQLite。原件作为 BLOB 保存，解析在独立进程执行，页面轮询状态。单一业务路由调用本地检索、待办与 Mock 推荐；可选 OpenAI 仅生成带来源回答，不执行工具。原阶段 0 CLI 仍可独立运行。

当前核心实体为 users、sessions、documents、chunks、tasks、audit；以下 Course、CreditRule、Consent、ToolRun 等是后续设计，并未全部建表。多用户部署、可靠任务恢复及 OS 沙箱仍未实现。

```mermaid
flowchart TD
  U[Web 对话与上传] --> A[身份校验与 API]
  A --> O[单编排器与工具白名单]
  O --> R[检索与带引用回答]
  O --> B[通知提取与活动规则]
  O --> C[Connector 注册表]
  C --> M[Mock / 用户授权导入]
  A --> P[受限文件解析任务]
  P --> D[原件 / 段落 / SQLite]
  R --> D
  B --> D
  O --> L[脱敏审计与任务状态]
```

## 数据契约

未来持久模型均含 id、owner_id、created_at、updated_at；原型仅包含 Source 和 Activity 的必要子集。

| 实体 | 核心字段与约束 |
|---|---|
| Document / Chunk | 文件 hash、格式、存储 key、页码/段落、解析版本、来源 ID |
| DocumentSource | 平台、记录 ID、官方来源、学院、标题、发布日期、正文 SHA、结构化附件清单（ref/name/url/parsed）、未读标记 |
| NoticeAnalysis | 资料、正文版本、提取器、提示词版本、模型、运行 ID、状态、结果、错误码、尝试次数、用量 |
| Notice / Task | 来源 ID、正文、deadline（可空）、原始时间文本与精度、时间类别、地点、提取方法、证据引用、确认状态（含忽略） |
| Course / CreditRule | 学年、专业、类别、先修、替代关系、规则出处及版本 |
| Activity | 起止、报名截止、类别、名额、资格、学分、规则来源及有效期 |
| ToolRun | 请求 ID、身份、工具、参数摘要、状态、错误码、耗时 |
| Consent | 操作与参数摘要、有效期、单次 token、撤销状态 |

数据流：上传 → 校验 → 解析 → 按页分段 → 建索引 → 检索用户可见段落 → 模型生成 → 验证引用 ID → 展示。解析失败时记录原因，禁止返回“成功导入”。检索结果必须来自服务端权限过滤后的集合。

## Connector 契约

声明 id、operations、permissions、writable、auth、sync、errors、availability。正式接口建议 invoke(operation, params, context) 返回 records、cursor、fetched_at、warnings；context 的用户身份由服务端构造，不能信任模型生成的 owner_id。

错误分类：AUTH_REQUIRED、FORBIDDEN、RATE_LIMITED、UNAVAILABLE、INVALID_DATA、UNSUPPORTED。只读超时最多重试两次并退避；无权限不重试；写请求不能盲目重试。当前 Mock 为快照，无网络错误与增量游标实现。

## 编排与恢复

状态机：received → planned → running → succeeded / failed；未来写操作插入 awaiting_confirmation 与 verifying。每个步骤限制调用次数、时长和输出大小。模型只提出工具调用，由程序验证 schema、权限和配额后执行。

阶段 0 的 Agent.run 只实现关键词路由、只读函数调用和步骤标签，不实现 LLM 规划、可靠任务队列或持久恢复。正式版本分离工具计划和权限判定，不能依赖关键词作为安全策略。

## 扩展条件

只有写入并发、备份和运营需求明确时迁移 PostgreSQL；只有全词检索召回不足且有评测依据时增加向量检索；只有长任务恢复需求出现时引入持久任务队列。多 Agent、微服务和多校租户均不在当前范围。

2026-10-09：`academic_defaults.py` 独立提供原始学年/季学期参数兼容、有限探测范围、同学期缓存与默认日期规则。学业视图直接消费教务课程，日期可用性与官方核对状态分开；默认规则及事件 assumptions 可供未来模块复用，未接入模型。
