# Agent 工具目录与使用文档

日期：2026-10-13。本文定义 **36 个拟议工具**，不是已经注册的可执行工具。先把现有业务封装，再接 Agent；不把几十个工具全部塞进每轮模型上下文。

实现标记：**A** = 最新发布包中已有业务接口，需增加工具适配；**B** = 当前 Campus Agent 工作区已有能力，需要跨实现适配；**C** = 新增能力。下文统一结果、授权上下文、部分字段转换本身属于新增适配工作，即使标 A 也不是零开发。

路径约定：A 类路由均省略 `/api/v1` 前缀，源码位于发布包 `apps/api/app/routers/`；B 类路由写完整路径。路由参数以实际 Pydantic/HTTP 契约为准；以下是拟议模型接口，不能原样当成现有 HTTP 请求体发送。

## 1. 共用契约

每个工具注册：`name`、`version`、`description`、`input_schema`、`output_schema`、`effects`、`required_scopes`、`timeout`、`retry_policy`、`idempotency_policy`。输入 schema 拒绝未知字段。工具名在本文用点分形式；接入 Harness 时映射为模型提供方支持的合法名字，如 `campus_materials_search`，由注册表维护唯一映射。

服务端注入 `principal`、`conversation_id`、`request_id`、时区、预算与授权引用。它们不是可由模型伪造的用户字段。参数中的 ID 必须来自此前有效结果或经鉴权的当前工作区。模型不能传 Cookie、密码、API key、SQL、绝对路径或任意目标 URL。

```json
{
  "status": "ok",
  "data": {},
  "evidence": [],
  "coverage": {"scope": "current_term", "complete": true, "missing": []},
  "freshness": {"fetched_at": null, "stale": false},
  "warnings": [],
  "next_cursor": null,
  "job_id": null,
  "error": null
}
```

这是示意模板；真实来源未知时 `complete` 必须为 false，`fetched_at=null` 不能解释为新鲜。`status` 枚举为 `ok | partial | queued | needs_user | error`。工具适配不得把旧接口里不存在的来源时间、完整性、权限保证默认填成成功。

错误格式：`{code, message, retryable, recovery_action}`。拟议统一错误包括 `AUTH_REQUIRED`、`SCOPE_DENIED`、`UNSUPPORTED`、`NOT_FOUND`、`STALE_VERSION`、`TIMEOUT`、`RATE_LIMITED`、`NO_EVIDENCE`、`BUDGET_EXCEEDED`、`INVALID_INPUT`。保留内部原始码供诊断，不把长堆栈和密钥带回模型。

调用示例全部为合成 ID，不能直接操作真实账号。下面每个条目的“返回”指 `data` 内容，且必须附带共用结果中的来源与状态。

### 副作用类别

- **R**：读取本地信息；不改变业务对象。
- **N**：读取远端/刷新缓存，可能改变本地同步状态。
- **P**：创建预览或派生候选，不改变生效安排；仍可能写本地预览记录。
- **W**：本地业务变更；检查明确意图、对象和版本，提供回执。
- **G**：可能付费或向模型发送资料；在已授权范围和预算内执行。

读失败最多有限重试；写或 G 调用超时先查询 job/幂等记录，不能直接再发。列表统一 `cursor?:string, limit?:integer(1..50)`，适配器翻译后端分页；达到预算必须返回 partial，不能把第一页当全部。

## 2. 连接与课程（T01–T06）

### T01 `connections.status` — A / R

- 用途：判断有哪些连接、能力和最近同步状态。不是登录，也不刷新学校数据。
- 输入：`connection_id?:string`；示例 `{}`。
- 返回：连接摘要、支持的能力、缓存时间、需用户完成的恢复动作；不返回凭据。
- 映射：`GET /connections`、`GET /connections/{id}`、`GET /connections/{id}/capabilities`；`connections.py`。
- 前置/失败：本地个人身份有效；未知连接返回 NOT_FOUND。缺少连接器服务标 UNSUPPORTED/依赖未就绪，不让 Agent 无休止重试。

### T02 `connections.check` — A / N

- 用途：检查已选择平台当前会话是否可用；连接异常或用户完成官方登录后使用。
- 输入：`connection_id:string`；示例 `{"connection_id":"conn_x"}`。
- 返回：状态、能力和 `recovery_action`。
- 映射：`POST /connections/{id}/check`。
- 前置/失败：仅检查已授权平台；AUTH_REQUIRED 时呈现官方登录操作并等待，不让模型索要校园密码。检查通过不代表所有课程都同步完成。

### T03 `connections.sync` — A / N

- 用途：刷新该连接下已绑定的课程数据；用于“更新课程作业”等明确请求或有效同步授权。
- 输入：`connection_id:string`；示例 `{"connection_id":"conn_x"}`。
- 返回：job ID、实际同步范围；完成后由 T31 查询结果，再重读业务对象。
- 映射：`POST /connections/{id}/sync`；同连接已有排队/运行任务时复用。首版工具不声称支持任意单课程刷新。
- 前置/失败：已有绑定课程与会话；无绑定范围先发现/导入课程。部分失败保留缓存；登录失效暂停，不把“已入队”说成“已同步”。

### T04 `courses.list` — A / R

- 用途：查找本地已导入课程，解析“高数”“这门课”。
- 输入：`query?:string, term?:string, cursor?:string, limit?:integer`；示例 `{"query":"微积分"}`。
- 返回：候选课程 ID、名称、学期、教师、平台来源；匹配不唯一返回候选，不自动选第一项。
- 映射：`GET /courses`、必要时 `GET /courses/{id}`。query/term 的匹配逻辑由适配层补齐；只在已读取范围内筛选并标明是否完整。
- 前置/失败：没有本地课程时返回空集合及可用的连接选项，不自动创建课程。

### T05 `courses.discover` — A / N

- 用途：读取学校平台课程列表，补足未导入的课程。
- 输入：`connection_id:string, term?:string, cursor?:string`；示例 `{"connection_id":"conn_x","term":"2026-2027-1"}`。
- 返回：平台课程候选、external ID、名称/学期/教师、分页与发现结果句柄。
- 映射：`GET /connections/{id}/courses`。结果句柄是适配层新增，保存服务端已发现的对象，供导入使用。
- 前置/失败：平台支持课程发现且已登录；UNSUPPORTED 时给手动入口，不伪造发现结果。只发现，不导入。

### T06 `courses.import` — A / W

- 用途：将用户选定或明确授权范围内的已发现课程加入本地，建立来源映射。
- 输入：`discovery_id:string, external_ids:string[]`；示例 `{"discovery_id":"dis_1","external_ids":["course_17"]}`。
- 返回：导入/复用课程 ID 与绑定结果。
- 映射：`POST /connections/{id}/courses/import`。适配器从发现记录取 title/term/teacher，不能信任模型补写学校事实。
- 前置/失败：发现记录属于当前身份且未过期；重复 external ID 复用。现有实现存在同名信息匹配路径，工具适配须额外审查跨平台合并歧义，不将其直接视为可靠身份映射。

## 3. 资料与回放（T07–T12）

### T07 `materials.list` — A / R

- 用途：查看一门课有哪些资料及解析状态，用于缩小检索/出题范围。
- 输入：`course_id:string, cursor?:string, limit?:integer`；示例 `{"course_id":"crs_1"}`。
- 返回：resource ID、标题、版本、类型、parse_status、来源与可读取范围。
- 映射：`GET /courses/{id}/resources`。
- 前置/失败：课程属于当前主体；资源只有元数据时不能宣称已获得正文。

### T08 `materials.import` — A / W

- 用途：导入用户在会话或工作区选择的文件并解析。
- 输入：`course_id:string, attachment_ids:string[]`；示例 `{"course_id":"crs_1","attachment_ids":["upload_1"]}`。
- 返回：job ID、接受的文件及拒绝原因。
- 映射：`POST /imports` 的 multipart `payload/files`；附件 ID → 临时文件流是新增上传适配，不接受模型提供任意文件路径。
- 前置/失败：附件属于当前会话，类型、大小与解包限制按后端校验；利用现有 Idempotency-Key。解析失败只说明未读到正文，不生成“已理解”摘要。

### T09 `materials.search` — A / R

- 用途：在已有正文中寻找回答依据；问题解释前先检索。
- 输入：`query:string, course_id?:string, cursor?:string, limit?:integer`；示例 `{"query":"实验报告","course_id":"crs_1"}`。
- 返回：片段 ID、正文、resource ID、版本与 locator。
- 映射：`GET /search`。当前底层为子串匹配；语义检索、查询改写及排序改进另做，不在文档中冒充已实现。
- 前置/失败：仅 ready 的当前版本资料；NO_EVIDENCE 时可换关键词或请求补充材料，不凭空作答。

### T10 `materials.read` — A / R

- 用途：展开命中片段的上下文，核验条件、附件和多日期含义。
- 输入：`resource_id:string, expected_version:integer, chunk_ids?:string[], cursor?:string, limit?:integer`；示例 `{"resource_id":"res_1","expected_version":2}`。
- 返回：限定长度的正文片段、定位、实际版本、未覆盖部分。
- 映射：`GET /resources/{id}`、`GET /resources/{id}/chunks`；指定片段过滤、输出预算与版本锁定由适配器补齐。
- 前置/失败：版本变化返回 STALE_VERSION 并重新检索；不能悄悄用新文本解释旧引用。原文打开是前端动作，不需要再次付费调用模型。

### T11 `recordings.list` — A / R

- 用途：查找某课程回放场次。
- 输入：`course_id:string, cursor?:string, limit?:integer`；示例 `{"course_id":"crs_1"}`。
- 返回：场次 ID、标题、时间、已有来源链接与权限状态。
- 映射：`GET /courses/{id}/recordings`。
- 前置/失败：空列表表示当前同步范围内无记录，不推断这门课从未录制。

### T12 `recordings.transcript` — A / N

- 用途：读取已同步回放的可用字幕，定位讲解。
- 输入：`recording_id:string, start_seconds?:number, end_seconds?:number`；示例 `{"recording_id":"rec_1","start_seconds":600,"end_seconds":900}`。
- 返回：字幕片段及起止秒、语言、平台来源；适配器按请求切片并标明覆盖。
- 映射：`GET /recordings/{id}/transcript`，现有接口可能访问远端；不能当成本地纯读。
- 前置/失败：有已支持的字幕来源与有效会话；无字幕时返回 UNSUPPORTED/无内容，不启动未实现的音频转写或批量下载视频。

## 4. 事项与任务（T13–T17）

### T13 `assignments.list` — A / R

- 用途：读取学校作业事实，包括截止与提交状态。
- 输入：`course_id?:string, due_before?:datetime, due_after?:datetime, cursor?:string, limit?:integer`；示例 `{"course_id":"crs_1"}`。
- 返回：作业 ID、截止精度、平台提交状态、来源；未知截止单列，不能被日期筛选无声排除。
- 映射：`GET /assignments` 或 `/courses/{id}/assignments`；时间参数翻译和未定日期补查由适配层负责。
- 前置/失败：缓存旧时明确提示；已提交作业不能继续当未交催促，不能使用本地任务完成状态替代平台事实。

### T14 `tasks.list` — A / R

- 用途：查询本地学习任务与状态，用于今日汇总和计划。
- 输入：`course_id?:string, status?:enum, due_before?:datetime, cursor?:string, limit?:integer`；示例 `{"status":"pending"}`。
- 返回：任务 ID、revision、标题、预计时长、截止精度、前置关系和来源。
- 映射：`GET /tasks`、必要时 `/tasks/{id}`。
- 前置/失败：区分没有任务和没有数据；不能用全部 pending 列表直接回答“今天”，还需 T20 的生效时间块和当前日期。

### T15 `tasks.build` — A / W

- 用途：按已有资料生成或更新本地学习任务，供排程使用。
- 输入：`course_id?:string`；示例 `{"course_id":"crs_1"}`。
- 返回：job ID；完成后查询新增、更新、保留与告警统计。
- 映射：`POST /tasks/build`；现有稳定 match_key 与同范围在途任务复用。
- 前置/失败：需要用户整理/规划任务的明确目标；此接口会写任务，不能把它包装成无副作用预览。默认缩小到相关课程，不为了回答一个截止问题重建全部任务。

### T16 `tasks.update` — A / W

- 用途：按明确指令修改本地任务名称、估时、截止或状态。
- 输入：`task_id:string, expected_revision:integer, changes:object`，仅开放后端允许字段；示例 `{"task_id":"tsk_1","expected_revision":2,"changes":{"estimated_minutes":40}}`。
- 返回：新 revision 和变更摘要；保留用户修订与来源事实区别。
- 映射：`PATCH /tasks/{id}`，版本映射到现有 If-Match。首期撤销记录需要工具层新增，不能假定现有 PATCH 自动可撤销。
- 前置/失败：目标唯一且用户明确要求；版本冲突先重读，不能覆盖刚发生的变更；禁止通过此工具修改官方作业事实。

### T17 `tasks.feedback` — A / W

- 用途：记录完成、部分完成或本次未做，并决定是否建议重排。
- 输入：`task_id:string, outcome:completed|partial|skipped, actual_minutes?:integer, note?:string`；示例 `{"task_id":"tsk_1","outcome":"completed","actual_minutes":35}`。
- 返回：feedback、更新任务和 replan_suggested。
- 映射：`POST /tasks/{id}/feedback`；由适配器生成并持久化 Idempotency-Key。
- 前置/失败：用户明确报告对应任务情况；重复调用不得重复记反馈。“完成学习”不等于向学校提交作业。

## 5. 时间与计划（T18–T23）

### T18 `availability.read` — A / R

- 用途：读取用户设置的可用时间，不把空白时段默认视为全天可用。
- 输入：`{}`；返回：可用时段、时区与版本摘要。
- 映射：`GET /preferences/availability`。
- 前置/失败：没有设置时询问本次需要的最少时间约束，例如“这周哪些晚上有空”，不逼用户完成整个偏好表。

### T19 `availability.update` — A / W

- 用途：保存用户明确表达的长期可用时间。
- 输入：`expected_revision:string, availability:object`；示例 `{"expected_revision":"av_1","availability":{"timezone":"Asia/Shanghai","windows":[]}}`，仅示意外层结构，内层需按现有 schema 翻译，不能把空列表当默认值发送。
- 返回：保存后的设置与变更摘要。
- 映射：`PUT /preferences/availability`；工具层需补版本摘要/并发保护和用户输入规范化。
- 前置/失败：一次性的“今晚有空”应作为本轮约束，不永久覆盖每周偏好；旧引擎若不支持临时约束，先返回限制，不能偷偷改全局设置。

### T20 `plans.read` — A / R

- 用途：读取当前生效安排或指定方案，用于回答日程、承接“把刚才的改一下”。
- 输入：`plan_id?:string`；示例 `{}`。
- 返回：计划、时间块、未排入事项、锁定状态和输入版本。
- 映射：`GET /plans/current` 或 `/plans/{id}`；无生效计划可以返回 null，不视为整个助手不可用。

### T21 `plans.preview` — A / P

- 用途：按任务、可用时间和锁定约束生成候选计划。
- 输入：`horizon_start:datetime, horizon_end:datetime, respect_locked:boolean=true`；示例 `{"horizon_start":"2026-10-13T00:00:00+08:00","horizon_end":"2026-10-20T00:00:00+08:00","respect_locked":true}`。
- 返回：candidate plan ID、input_revision、时间块、未安排原因、与当前计划差异。
- 映射：`POST /plans/preview`；差异可复用前端思路下沉到适配层。
- 前置/失败：需要任务与时间依据；容量不足返回未安排原因，不延长用户可用时间。现有引擎尚无任意自然语言约束覆盖的保证，需要增加约束适配后才能承诺“今晚只用一小时”。

### T22 `plans.apply` — A / W

- 用途：应用已经展示且处于用户授权范围内的具体候选方案。
- 输入：`plan_id:string, expected_input_revision:string`；示例 `{"plan_id":"plan_2","expected_input_revision":"rev_7"}`。
- 返回：生效计划和被替代的上一计划 ID。
- 映射：`POST /plans/{id}/apply`；既有后端检查 input_revision，工具层再核对用户接受的方案摘要与版本。
- 前置/失败：不能把模型自填同意字段当授权。PLAN_STALE 映射为 STALE_VERSION，重新预览并展示变更，不自动接受变化后的方案。

### T23 `plans.revoke` — A / W

- 用途：用户要求撤销当前安排时恢复上一生效方案（若存在）。
- 输入：`plan_id:string`；示例 `{"plan_id":"plan_2"}`。
- 返回：撤销结果与 restored_plan_id。
- 映射：`POST /plans/{id}/revoke`。
- 前置/失败：只能针对当前生效对象；操作后重读核验。不会回滚学校平台提交，也不会撤销已经完成的学习事实。

## 6. 复习产物（T24–T30）

### T24 `reviews.materials` — A / R

- 用途：读取可用于当前课程复习的材料和版本。
- 输入：`course_id:string, cursor?:string, limit?:integer`；示例 `{"course_id":"crs_1"}`。
- 返回：可用材料、解析状态与可选范围；不是仅按文件名判定内容。
- 映射：`GET /courses/{id}/review-materials`。
- 前置/失败：正文缺失明确列出，不把未读附件发送成功当作材料齐全。

### T25 `reviews.preview` — A / P

- 用途：冻结用户希望练习的范围，预检发送材料与参数；不开始付费生成。
- 输入：`course_id:string, material_selection:object, requirements:object, notes?:string`；示例 `{"course_id":"crs_1","material_selection":{"resource_ids":["res_1"]},"requirements":{"question_count":5}}`。
- 返回：预览句柄、fingerprint、实际材料、缺失内容、provider 摘要与限制/告警。具体题型等参数由适配器合法化，用户未表达的重要选择需在预览中可见。
- 映射：`POST /review-papers/preflight`，适配器构造现有 ReviewRequestIn 并保存完整请求。预览句柄持久化是新增能力；现有接口只返回 fingerprint 等结果。
- 前置/失败：模型与材料有效；缺少成本估算就标“未知”，不可伪造金额。预检无正文应停下或缩小范围。

### T26 `reviews.generate` — A / G+W

- 用途：按已展示的预览创建复习卷。
- 输入：`preview_id:string`；示例 `{"preview_id":"review_preview_1"}`。
- 返回：job ID；未完成前不返回虚构试卷内容。
- 映射：`POST /review-papers/generate`；从服务端预览记录恢复请求，fingerprint → If-Match，幂等键由运行时持久化。
- 前置/失败：预览、外发范围与预算授权仍有效；材料变化重新预检。模型超时先查 job，不能另起一次重复付费生成。

### T27 `reviews.list` — A / R

- 用途：查找已有复习卷，避免重复生成。
- 输入：`course_id?:string, cursor?:string, limit?:integer`；示例 `{"course_id":"crs_1"}`。
- 返回：paper ID、标题、可用版本与状态。
- 映射：`GET /review-papers`；失败/未完成记录不能显示为可用成品。

### T28 `reviews.read` — A / R

- 用途：读取已有卷及指定版本，解释题目、进行修订前核验。
- 输入：`paper_id:string, revision?:integer`；示例 `{"paper_id":"pap_1","revision":1}`。
- 返回：题目、答案/解析、引用与版本。练习模式下默认 UI 不直接展开答案。
- 映射：`GET /review-papers/{id}` 及版本查询；输出过长时按题目分页/裁剪属于适配层工作，必须说明未读部分。

### T29 `reviews.revise` — A / G+W

- 用途：按明确修改目标创建新版本，保留旧卷。
- 输入：`paper_id:string, based_on_revision:integer, preview_id:string, instruction:string`；示例 `{"paper_id":"pap_1","based_on_revision":1,"preview_id":"review_preview_2","instruction":"减少计算题难度"}`。
- 返回：修订 job ID；完成后由 T28 读取新版本。
- 映射：`POST /review-papers/{id}/revisions`，翻译现有 ReviewRevisionRequest、If-Match 和 Idempotency-Key。
- 前置/失败：基准卷已生成、预览与实际修改请求一致；不可声称底层一定只局部改题，现有引擎若重做整卷，应先展示影响范围。

### T30 `reviews.export` — A / R

- 用途：导出指定已有版本供阅读/打印。
- 输入：`paper_id:string, revision?:integer, format:markdown|student|answers`；示例 `{"paper_id":"pap_1","revision":1,"format":"student"}`。
- 返回：受控下载引用、MIME 类型和文件名，不返回本地任意路径。
- 映射：`GET /review-papers/{id}/export`。
- 前置/失败：目标版本存在；已有导出不等于支持服务器生成 PDF，当前打印页另由 UI 打开。

## 7. 长任务控制（T31–T33）

### T31 `jobs.read` — A / R

- 用途：恢复同步、解析、生成等后台任务。
- 输入：`job_id:string`；示例 `{"job_id":"job_1"}`。
- 返回：状态、进度、错误、结果对象引用和可恢复动作。
- 映射：`GET /jobs/{id}`。
- 前置/失败：当前主体拥有 job；NOT_FOUND 不能自动创建替代任务。由运行时订阅或退避查询，避免模型循环每秒调用。

### T32 `jobs.cancel` — A / W

- 用途：执行用户针对具体任务的取消指令。
- 输入：`job_id:string`；示例 `{"job_id":"job_1"}`。
- 返回：是否已取消/正在取消/无法取消与最终状态。
- 映射：`POST /jobs/{id}/cancel`。
- 前置/失败：只取消关联 job；完成与取消存在竞争时以业务最终状态为准。不能保证撤销已发生的模型费用或删除已有产物。

### T33 `jobs.retry` — A / W 或 G+W

- 用途：对明确可重试的失败任务执行恢复。
- 输入：`job_id:string`；示例 `{"job_id":"job_1"}`。
- 返回：复用/新任务 ID 与对应关系。
- 映射：`POST /jobs/{id}/retry`。
- 前置/失败：读取失败原因和 retryable，修复缺失依赖；模型任务需要仍有效的预算与外发授权。登录错误、参数错误不能盲重试；重新预览才可解决的版本错误不走此工具。

## 8. 校园能力适配（T34–T36）

### T34 `campus.query` — B / R

- 用途：读本人基本信息、教务课表、成绩或已接入学院通知缓存。
- 输入：`capability:profile|courses|grades|college_notices, query:object`；示例 `{"capability":"college_notices","query":{}}`。
- 返回：类型化记录、来源、缓存时间、覆盖范围和异常。profile 仅取当前任务需要的字段，成绩不为回答通知而额外读取。
- 映射：`GET /api/campus/query/{capability}`；日期化课表可复用 `/api/campus/academics`。工具 query 需逐能力映射到现有查询契约，不能把任意对象透传。
- 前置/失败：旧版已验证身份与新会话的显式映射；未接入学院返回 UNSUPPORTED，不退回猜测学院或查其他账号。刷新走受控业务适配，不能以 query 隐式提交校园密码。统一校园刷新工具待账户桥接完成后再注册。

### T35 `notices.import` — B / W

- 用途：保存已读取的校园通知，保留正文、附件清单及版本。
- 输入：`record_id:string`；示例 `{"record_id":"ckc:3212562"}`，仅作格式示例。
- 返回：document ID、重复导入标记和解析/候选状态。
- 映射：`POST /api/college-notices/import`，现有服务端从缓存取正文，不能让模型指定 URL 替换原文。
- 前置/失败：当前账号已获授权且记录存在；正文更新需新版本，重复导入不能重复制造待办。保存附件名称不等于解析了附件。

### T36 `notices.analyze` — B / P 或 G+W

- 用途：提炼已保存通知的事件、对象、时间、地点和待核对内容。
- 输入：`document_id:string`；示例 `{"document_id":"doc_1"}`。
- 返回：分析状态/引用、事件、待核对字段、附件缺口及结果时间。
- 映射：`POST /api/documents/{id}/analysis`、`GET /api/notice-analyses`。规则/模型策略由服务端配置和既有授权决定；旧接口 consent 字段只能由验证后的授权适配产生。
- 前置/失败：分析结果可能落库并生成候选，不能宣称完全无写入。所有模型候选保持待核对，不自动变成已确认截止；模型失败保留旧结果并标时间，规则零候选不等于通知没有事项。

## 9. 有意不开放给模型的流程

| 现有/未来流程 | 处理方式 |
|---|---|
| 登录、验证码、WebVPN 凭据、模型密钥 | 账户/设置安全表单或官方登录页；Agent 只接收恢复状态 |
| 启停连接、删除数据、解除账户映射 | 显式设置流程；后续若工具化需独立权限和准确作用范围 |
| 手工创建课程、来源绑定 | 首期在资料工作区保留；高置信映射由 T06 处理，歧义留给用户 |
| 创建独立提醒、校园待办确认/编辑、成绩分析 | 已有基础的分别适配；本轮 36 工具未覆盖全部增删改查，不假称已能端到端完成这些动作 |
| 导出通用 ICS | 发布包有 `/exports`；在统一日程类型和导出授权明确后单列工具 |
| 任意网页抓取、学校报名/提交、群发消息 | 首期无对应模型工具；保留官方入口供用户操作 |
| Shell、SQL、任意文件读写、自动装插件 | 不属于学生版校园工具集合 |

## 10. 开发者接入与验收模板

每新增一个工具必须交付：本页条目/独立说明、严格输入输出 schema、实现适配器、权限与幂等规则、成功样本、空结果样本、过期/越权/失败样本、UI 呈现规则。说明文档告诉 Agent 何时使用；服务端才真正执行约束。

首批仅注册 T01、T04、T07、T09、T10、T13、T14、T20、T31，先验证只读答案。其他按阶段与能力检测加载。这里的分批注册不代表缺少数据时可跳过覆盖说明。

推荐契约测试：未知字段拒绝、伪造 ID/跨主体拒绝、分页完整性、来源版本、unknown 时间保留、写入幂等、模型调用前授权、超时查询恢复、取消竞争、错误脱敏。已有 API 测试保留；不能用“模型最终说成功”作为工具执行成功标准。
