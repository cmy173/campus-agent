# 本地 REST API

学院通知正文复用 `GET /api/campus/query/college_notices` 与 `POST /api/campus/sync/college_notices`，查询参数只有 `limit`（整数 1–5，默认 3）；POST 体为 `{"query":{"limit":3}}`。身份、学院和来源由服务端决定，无法用 `owner_id`、`college`、`url` 覆盖。当前只匹配已接入的竺可桢学院来源。

结果新增可选 `selection`，含 `college`、`status`（matched/missing/unsupported/ambiguous）、`profile_fetched_at`、`profile_stale`。记录提供正文、正文哈希、附件链接、原始发布日期和来源。缓存与部分错误可同时返回，首页有限窗口始终标为 partial。完整语义见 [学院通知正文](college-notices.md)。

教务未知自定义 HTTP 响应（如尚未核实语义的 921）在旧同步诊断中标记 UPSTREAM_STATUS_UNKNOWN，reason=HTTP_CUSTOM_STATUS，并立即暂停而保留当前 pending 学期。仅 500–599 计入 SERVER_ERRORS_PAUSED；当前版本 `2026-10-12.custom-status`。新统一结果对此仍用 PROTOCOL_CHANGED 表示响应无法确认，不把状态码猜成限流、认证失效或服务器宕机。

通行证取消入口 POST `/api/auth/passport/cancel`，JSON `{"flow_id":"当前浏览器流程"}`。需要既有同源请求头，绑定该浏览器的流程 Cookie；已签发会话的迟到取消还核对对应本机会话摘要。只撤销此流程，不删除已有资料；其他浏览器或无效流程不能取消。认证失败响应新增安全分类 AUTH_CONTEXT_INVALID/CAPTCHA_REQUIRED/ACCESS_REJECTED/RATE_LIMITED，取消为 FLOW_CANCELLED；不回显校方正文或凭据。详见 [通行证失败恢复](passport-login.md)。

2026-10-12 新增统一校园能力入口，旧接口保持兼容：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/campus/connectors` | 已注册能力、认证/权限、分页/增量声明和内存会话状态 |
| GET | `/api/campus/query/{capability}` | 缓存查询；参数由各能力严格校验，身份取服务端会话 |
| POST | `/api/campus/sync/{capability}` | 显式远端读取；JSON `{"query":{},"platform":"zdbk_public"}`，需要 CSRF |
| GET | `/api/campus/notices` | 公开教务通知摘要的离线业务入口 |
| POST | `/api/campus/disconnect/{platform}` | 清该站点会话并保留缓存及上传资料 |

新结果统一含 records/source/platform/fetched_at/last_attempt_at/freshness/completeness/next_cursor/warnings/error。平台失败可返回 HTTP 200 的带缓存结果，必须检查 `error.code` 与完整性；参数错误为 HTTP 400，本地认证/CSRF 仍为 401/403。只有 `sync` 请求网络，过期 `query` 不自动登录。能力、查询范围、错误码、元数据语义及调用示例见 [连接器指南](connectors.md)。未实现的能力返回 UNSUPPORTED，不转为任意网络请求。

v0.2 仅允许回环地址访问，无 CORS。写请求需 `X-Campus-Request: 1`；已登录写请求还需 `X-CSRF-Token`，从登录或 `/api/me` 获取。会话 cookie 为 HttpOnly、SameSite=Strict；新通行证登录的本机会话最多 30 天，旧本地接口 12 小时。校园会话只在进程内最多一小时。

| 方法 / 路径 | 功能 |
|---|---|
| GET /api/health | 本机健康状态 |
| POST /api/auth/register | JSON username、password；创建独立本地账号 |
| POST /api/auth/login | 本地登录 |
| POST /api/auth/passport/prepare | 主入口；准备浏览器绑定的五分钟 CAS 登录，返回 flow_id 与按需验证码，不要求已有资料会话 |
| POST /api/auth/passport/login | username、password、authcode、flow_id、link_legacy（可选）；验证通行证、建立资料身份、自动连接教务并执行首批同步，返回 username、csrf、mode、campus；绑定旧资料需原会话和 CSRF |
| POST /api/campus/reconnect | 复用当前通行证会话即时换取教务票据并重新同步；不接收密码或任意 service URL |
| POST /api/auth/logout | 撤销当前会话 |
| GET /api/me | 用户名与 CSRF token |
| GET /api/model | 模型配置状态，不返回密钥；`notice` 子对象为通知整理提供方状态 |
| POST /api/documents | 原始字节；X-Filename 为 URL 编码名称，X-Document-Kind 为 notice/material |
| GET /api/documents | 当前账号资料与状态 |
| GET /api/documents/{id} | 资料详情、带 ref 与位置的 chunks、官方来源 `source` 与 `analyses` |
| GET /api/documents/{id}/download | 下载原件 |
| DELETE /api/documents/{id} | 级联删除原件、片段、待办、来源与分析 |
| POST /api/documents/{id}/analysis | JSON `mode:rules\|model`，model 还需 `consent:true`；触发一次整理并返回当前状态 |
| POST /api/college-notices/import | JSON 仅 `record_id`；由服务端按当前账号缓存构造资料、官方来源与附件清单 |
| GET /api/notice-analyses | 当前账号全部通知类资料的来源、整理状态与结果，附模型配置 |
| GET /api/tasks | 当前账号全部待办 |
| PATCH /api/tasks/{id} | JSON state:pending/done/ignored、due:带时区 ISO 日期或 null；标记已核对 |
| POST /api/chat | JSON query、可选 use_model；返回 answer/citations/steps/mode 及可选 warning/tasks |
| GET /api/activities?target=2 | 目标 0–30 的虚构推荐 |
| GET /api/audit | 最近 50 条本账号操作记录 |
| DELETE /api/data | 清除本账号资料、派生内容和审计，保留账号 |
| GET /api/campus | 当前账号校园连接状态与已保存快照 |
| POST /api/campus/prepare | JSON consent:true；准备五分钟 CAS 表单，返回 flow_id 与可选验证码 data URL |
| POST /api/campus/login | JSON flow_id、username、password、authcode（可选）、consent:true；尝试一次并探测本科教务权限 |
| POST /api/campus/sync | JSON `{}` 开始全部学期同步；`resume:true` 续传，`captcha` 可选；不再接受 year / semester 限定查询 |
| POST /api/campus/captcha | 获取当前教务会话的查询验证码 |
| GET /api/campus/academics | `day=YYYY-MM-DD`、`term=年份/页面原始编码`（数字 1–999 或 1\|秋、1\|冬、2\|春、2\|夏）可选；返回所选周 agenda、独立当前日 today_agenda、均绩汇总 grades，仅读取当前账号快照 |
| POST /api/campus/calendar | JSON year、semester（保留页面原始值）、campus:"main"、refresh（可选布尔）；从固定学校来源更新校历，失败使用同学期缓存或默认日期并保留错误原因 |
| DELETE /api/campus | 断开校园连接并删除本账号校园快照 |

同步每次最多查询六个学期。响应 `auto_continue:true` 表示可自动发起下一次 `resume:true` 请求；出现验证码、超时等错误则暂停。`can_resume` 表示当前内存连接仍有待查询学期，`snapshot.sync_progress` 提供 `completed`、`total`、`pending`。断开或服务重启后续传会话失效。

`snapshot.courses.terms` 按学期保留 `year`、原始 `query_year`（可选，缺省使用 year）、原始 `semester`、`range_source:page|fallback`、原始名称 `term`、`data`、`fetched_at`、`stale`（含成功获取的空课表）。`courses.data` 是供其他模块读取的扁平课程列表，每条含所属学期、获取时间和过期标记。`complete` 仅指教务课表页当前全部具体学期已查询成功且没有无法处理的选项、降级范围或部分响应；占位/全部选项不查询。列表仍可包含之前缓存但本次不再开放的历史学期，这些记录保留 `stale:true`。基本信息和成绩保持独立快照，不进入模型输入。

`courses.catalog` 合并历次已发现的学期及旧课表记录，`available` 只代表本轮目录是否包含该学期；不会因为本轮学期列表变少删除记录，也不盲目请求已不开放的学期。`sync_progress.total/completed` 只表示本轮查询/更新数量，页面另显示本机已保存的学期数及有课学期数。`CACHED_TERM_RANGE` 表示本轮范围较窄并保留已有缓存，整体不标记本轮完整。

同一学期曾有课程、后来返回空课表时，`EMPTY_TIMETABLE_CACHE` 保留原课程及 fetched_at，设置 stale 和 source_status:cache，并将该学期列入 failed；首次取得空课表仍可正常保存为无课程学期。有效非空响应仍替换旧课程，避免退课/换课后残留；已识别部分响应合并未返回的缓存行，缓存行保留各自原获取时间。courses.attempted_at 记录更新尝试时间，courses.fetched_at 为已有学期最近成功获取时间，失败不会伪装成新获取。

`snapshot.profile` 的 `data` 仍只有六个基本字段；新增 `field_sources` 按字段提供 `{source,url,fetched_at,stale}`，URL 不含查询参数。`complete` 表示六个字段均有值，`missing_fields` 列出空字段；可用部分照常展示，完整性不代表本次全部更新。顶层 warnings 新增 profile / profile_eta / profile_zdbk 阶段。ETA 与已有学号不一致时拒绝合并；某来源失败不阻断其他教务查询。

学年页面值兼容 `2026` 与 `2026-2027`，内部 year 为起始年份，发送课表查询时使用原始 query_year。列表不可用时先读取成绩，再从已有学期、成绩学年和年级构造有限范围；完全没有依据时尝试当前及前七学年、每年四季，不声称范围完整。失败范围最多 180 项。sync_progress.failed 保存失败/部分学期；单学期协议错误可跳过继续，网络、验证码和会话错误仍暂停。

学业 agenda 的 `dated` 表示具有可用日期规则，并非官方核对通过；`source_status:live|cache|fallback|unavailable` 区分来源（未知学期无可用日期时为 unavailable）。calendar.verified 仍只表示学校来源核对通过，usable 允许默认日期展示。事件 `provisional` 和 `assumptions` 表示日期推算或缺失字段默认规则。默认日期不生成虚构课程或停课调课信息；成绩口径不变。

`/api/me` 返回 `mode:passport|offline|legacy`。有有效本机会话时默认打开缓存；无会话时不会凭用户名打开数据。新入口的 prepare 每分钟最多 6 次、login/reconnect 最多 3 次。CAS flow 同时绑定 HttpOnly cookie 和请求 flow_id；失败流程消耗后需重新准备，不自动重试密码。旧 register/login 接口保留兼容，前端仅提供旧资料登录，不作为主要入口。

上传首次返回 202，同账号相同内容返回 200 和 duplicate=true。状态 queued → processing → ready/failed。重启时未完成任务标为失败，可删除重传。相同内容去重不修改类型，选错资料类型时删除后重传。

`POST /api/college-notices/import` 只接受 `record_id`；`owner`、来源 URL、附件 URL 与标题都不是入参，全部由服务端在当前账号的通知缓存中重新校验后构造，缓存中没有该记录返回 404，标识格式非法返回 400。资料内容为“标题行 + 官方正文”，附件不进正文，另存为结构化清单（`ref`、`name`、`url`、`parsed:false`）。解析完成后自动写入离线规则结果，`GET /api/documents/{id}` 与 `GET /api/notice-analyses` 返回 `source` 与 `analyses`。

`POST /api/documents/{id}/analysis` 的 `mode:rules` 不需要同意也不联网；`mode:model` 必须 `consent:true`，未配置提供方返回 409，未同意返回 400，非通知类资料返回 400，未解析完成返回 409。请求立即返回当前状态，整理在后台线程执行，界面按 `status`（running/ready/partial/failed）轮询。`analyses[].error` 为 `{code,message}`，`code` 取 `TIMEOUT`、`RATE_LIMITED`、`AUTH_REJECTED`、`NETWORK`、`REDIRECT`、`EMPTY`、`TRUNCATED`、`RESPONSE_TOO_LARGE`、`UPSTREAM`、`NOT_CONFIGURED`、`CONFIG_INVALID`、`INTERNAL`、`INTERRUPTED` 或 `CONTRACT_*`（`CONTRACT_SCHEMA`、`CONTRACT_EVIDENCE`、`CONTRACT_REF`、`CONTRACT_URL`、`CONTRACT_YEAR_FABRICATED`、`CONTRACT_TIME_FABRICATED`、`CONTRACT_DERIVATION_REQUIRED`、`CONTRACT_CONFLICT_UNRESOLVED`、`CONTRACT_SIZE`、`CONTRACT_ENUM`、`CONTRACT_DATE`、`CONTRACT_INVALID_JSON`、`CONTRACT_EMPTY`）。错误消息为固定文案，不含模型响应正文、密钥或请求头。失败保留上次成功的 `payload` 与 `result_at`。

`analyses[].payload` 含 `notice_summary`、`coverage`(full/partial)、`coverage_note`、`events`、`open_questions`，模型结果另含 `extractor`、`model`、`prompt_version`、`evidence_corrected`、`input_coverage`。`events[]` 为 `{ref,title,summary,audience,actions,location,location_needs_attachment,attachment_refs,times,open_questions,evidence}`，`times[]` 为 `{kind,raw,value,precision,derived,derivation,conflict}`，`evidence[]` 为 `{ref,quote}`。附件链接只由服务端从 `source.attachments` 按 `ref` 映射，payload 中不含 URL。

待办新增 `origin`(rules/model)、`event_ref`、`analysis_key`、`evidence`（含 `ref`、`quote`、`chunk`、`locator`）、`time_kind`、`time_precision`、`location`、`needs_attachment`、`attachment_refs`、`derived_note`；`state` 增加 `ignored`。重新整理按 `(event_ref,time_kind,title)` 合并：已核对、已编辑、已完成或已忽略的事项保留，未处理候选就地更新，新结果不再支持的未处理候选被删除。每账号每小时最多 12 次模型整理与 60 次通知导入。

每用户最多 100 份资料，全局最多 10 个排队/解析任务；两条线程调度解析进程，单进程超时 30 秒。原件存于 SQLite BLOB，派生内容通过外键关联。

身份来自服务端会话，不信任请求中的 owner_id。无权对象返回 404。文件内指令不会触发工具或网络动作。错误返回 detail 字符串。

校园接口同样要求本地登录、写请求头与 CSRF。校园错误用 HTTP 409 和 `detail:{code,message,diagnostic?}` 返回，避免校园会话失效造成本地账号退出。传输错误的 diagnostic 仅含 stage、method、固定 host/path、reason 和 elapsed_ms；不含 URL 查询参数、表单、凭据、cookie、代理地址或响应正文。reason 为 TIMEOUT、DNS、TLS_CERTIFICATE、TLS_HANDSHAKE、CONNECTION_RESET、CONNECTION_REFUSED、HTTP_INTERRUPTED 或 NETWORK；操作期限另为 OPERATION_DEADLINE。学期发现错误还可包含 semester_codes（短数字编码或脱敏的非数值标记），reason 为 TERM_OPTIONS / UNSUPPORTED_TERM_OPTIONS。同步可以部分成功，snapshot 中每部分含 fetched_at、stale、complete，顶层 warnings 区分 service_login / terms / grades / timetable 阶段并可携带 diagnostic；不能只凭 HTTP 200 判断全部同步成功。有警告的同步审计记为 incomplete。旧校园接口的准备登录和验证码每分钟最多 6 次，登录和同步每分钟最多 3 次，按本地账号计数，不自动重试密码。`DELETE /api/data` 也删除校园快照和内存会话；本地退出只清除校园会话，保留快照。

当前 cookie 在本机 HTTP 下不设置 Secure，不能直接照搬公网；数据库不加密，解析子进程不是操作系统沙箱。详见 security.md。

2026-10-10 增量：学期参数兼容数字代码后带单季/组合季节及“季”“学期”“季学期”后缀，始终原样查询。ETA 访问拒绝新增 ETA_ACCESS_REJECTED，diagnostic 包含 http_status（901/922/429）。未知学期的 option_shapes 仅包含长度、是否含竖线/季节字和是否默认，不包含原始值或标签。

响应大小增量：ETA 数据接口上限 8,000,000 字节，登录跳转不读取正文；其余数据接口上限保持 2,000,000 字节。RESPONSE_TOO_LARGE 的 diagnostic 新增 limit_bytes、observed_bytes、content_kind 及固定路径；observed_bytes 仅表示已读到的上限加一，不声称为完整响应大小。学期参数还兼容页面双数字原值（如 1|0），不重写为单数字或季节码。
