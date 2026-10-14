# 通知整理事件与待核对事项

更新：2026-10-12。学院通知保存到资料库后，除既有离线规则提取外，可选用 DeepSeek 把整篇通知整理成事件、时间与待核对事项。所有模型产出初始为“待核对”，不自动报名、提交或发送任何内容。

## 边界

- 模型只接收：通知标题、正文段落（带段落 ID）、发布日期、学院名称、平台记录 ID、附件 ID 与文件名。不接收校园 Cookie、CAS ticket、账号密码、个人档案、附件 URL 或官方原文链接。
- 接口地址是 `campus_agent/notice_model.py` 中的常量 `https://api.deepseek.com/chat/completions`。环境变量、浏览器和通知内容都不能改写目标地址；请求不跟随跳转，避免凭据被转发到其他主机。模型网络策略不受 `CAMPUS_NETWORK_POLICY` / `CAMPUS_PUBLIC_NETWORK_POLICY` 影响。
- 通知正文与附件名称都是不可信数据。资料中出现的指令没有执行权限，不能改变系统提示、调用工具或索取密钥。
- 附件只保存名称与官方链接，本期不下载、不解析、不 OCR。时间地点只存在于附件中时返回“需查看附件”，不猜测。
- 学院匹配只决定通知来源，不代表用户符合通知资格；资格疑问进入待核对问题。
- 未配置模型时，通知仍可保存、检索并离线规则提取。

## 配置

在启动服务的终端设置（`.env` 不会自动加载）：

```
DEEPSEEK_API_KEY=<your key>
DEEPSEEK_MODEL=deepseek-flash      # 可选，默认值；仅允许字母数字与 . _ : -
NOTICE_MODEL_PROVIDER=deepseek     # 可选，只接受已登记提供方
```

设置后重启服务。`GET /api/model` 的 `notice` 字段返回 `configured`、`provider`、`model`、固定 `endpoint`、`prompt_version`、`sends`（发送内容说明），不返回密钥。模型名非法时 `configured=false` 并给出原因。

界面启用云端整理前必须勾选同意项，勾选后每次请求仍显式携带 `consent:true`；未同意返回 400。每账号每小时最多 12 次模型整理、60 次通知导入。

## 官方协议核对（2026-10-12）

依据 [DeepSeek 官方文档](https://api-docs.deepseek.com/) 与 [JSON 输出说明](https://api-docs.deepseek.com/guides/json_mode/)：

- `POST https://api.deepseek.com/chat/completions`，`Authorization: Bearer <key>`。
- 结构化输出用 `response_format: {"type":"json_object"}`；官方要求提示词中出现 “json”，并说明可能偶发返回空内容。该提供方**没有** OpenAI Responses 式 `json_schema` 严格模式，因此 schema 写入提示词，由服务端 `notice_contract.validate` 强制校验。
- 文档列出的模型为 `deepseek-flash`、`deepseek-v4-pro`（均 1M 上下文，支持 JSON 与工具）；旧名称仍被接受并路由到当前模型。默认取 `deepseek-flash`。
- 响应读取 `choices[0].message.content`、`choices[0].finish_reason`（`length` 视为截断）与 `usage.prompt_tokens/completion_tokens/total_tokens`。
- 只发送 `model`、`messages`、`stream:false`、`max_tokens`、`response_format`；不发送未核对的可选参数。

## 提示词与数据契约

版本常量 `notice_contract.PROMPT_VERSION = 'notice-extract-v1'`，随结果一起保存。提示词全文见 `campus_agent/notice_contract.py` 的 `SYSTEM_PROMPT`，schema 见 `SCHEMA`（同一份定义既写进提示词，也用于服务端校验）。要点：

- 跨段落归并同一事件；不同场次/校区/人群拆成不同 event，保持“时间—地点—对象”对应。
- `kind` 只能取 `registration_deadline`、`activity_start`、`activity_end`、`assembly`、`other`。发布日期禁止写入 `times`。
- 缺年份、缺月日或缺时分时 `value` 为 `null` 并保留 `raw` 与 `precision`（`datetime|date|month|text`）；禁止补造年份，禁止补造 23:59，禁止把“下周五”换算成具体日期。
- 时间矛盾时两条都保留、`conflict:true`，并在 `open_questions` 写明；不得自行消解。
- 每个 event 至少一条 `evidence`（段落 ID + 可定位原文摘录）；`attachment_refs` 只能用输入附件 ID；禁止输出任何 URL。
- 未提供附件正文，禁止声称已阅读附件。
- 信息缺失返回空值并提问，不用常识或往年惯例填补。

服务端校验（不通过即判为失败，不写入结果、不生成候选事项）：结构恰好匹配、字段长度与数组上限、枚举、日期合法性、`value` 的年月日与时分必须出现在 `raw` 或该事件已验证的 `evidence` 摘录中、跨段落合并必须 `derived:true` 并写明依据、`conflict` 必须伴随待核对问题、段落与附件 ID 必须存在、摘录必须能在指定段落定位、模型自撰字段不得包含链接、总长度上限 64 KB。摘录逐字正确但段落归属错误时，服务端改正归属并计数 `evidence_corrected`，不静默接受错误引用。

## 数据模型

新增表（`storage.py`，随启动幂等创建）：

- `document_sources`：`document`(PK)、`owner`、`platform`、`record_id`、`origin`、`college`、`title`、`published_at`、`body_sha256`、`attachments`(JSON：`ref`/`name`/`url`/`parsed`)、`has_unread_content`。附件 `ref` 为 `att_` + URL SHA-256 前 16 位，跨导入与重新整理稳定。
- `notice_analyses`：主键 `(document, body_sha256, extractor, prompt_version, model)`，含 `run_id`、`status`(`running|ready|partial|failed`)、`payload`、`result_at`、`error`(JSON：`code`/`message`)、`attempts`、`usage`。同一账号、正文版本、提示词版本与模型配置只有一行，重复执行更新该行。

`tasks` 增量列（旧库自动 `ALTER TABLE` 补齐，旧数据默认 `origin='rules'`）：`origin`、`event_ref`、`analysis_key`、`evidence`(JSON)、`time_kind`、`time_precision`、`location`、`needs_attachment`、`attachment_refs`(JSON)、`derived_note`。`state` 新增 `ignored`，用于“忽略”而不删除。

段落引用 ID 为 `p{位置+1}`，由服务端按 chunk 位置生成；`GET /api/documents/{id}` 的每个 chunk 返回 `ref`，界面原文对话框显示 `ref · 定位`，便于与证据对照。

## 服务调用示例

```python
from campus_agent.connector_contracts import UserContext
from campus_agent import notice_analysis

# 1. 由服务端按当前账号缓存构造导入（前端只能提供 record_id）
platform, record = app.state.campus_service.cached_college_notice(UserContext(owner), 'ckc:3212562')
content = (record.title + '\n\n' + record.body).encode('utf-8')   # 附件不进正文
with store.connect() as db:
    doc_id, duplicate = store_document(db, owner, record.title + '.txt', 'notice', content)
    notice_analysis.save_source(db, owner, doc_id, platform, record, now())

# 2. 解析完成后自动写入离线规则结果；按需触发模型整理
service = app.state.notice_analyses
state = service.request(owner, doc_id, 'model', consent=True)   # 'rules' 不需要同意
view = service.read(owner, doc_id)                              # 或 service.list_all(owner)
```

HTTP 入口：`POST /api/college-notices/import`（`{"record_id": "ckc:3212562"}`）、`GET /api/notice-analyses`、`GET|POST /api/documents/{id}/analysis`（`{"mode":"model","consent":true}` 或 `{"mode":"rules"}`）。

## 工作流与恢复语义

- 模型调用在数据库事务之外执行：读取资料与段落的事务先关闭，写入结果时另开事务。测试用一个真实并发写入验证不会阻塞。
- 写入前重新校验：资料仍存在、仍属于该账号、`sha` 与本次分析的正文版本一致、`run_id` 未被更新的一次运行取代。任一不满足即丢弃结果，不复活已删除资料，不写入其他账号。
- 失败不覆盖上次成功结果：`payload` 与 `result_at` 保留，只更新 `status='failed'` 与 `error`，界面同时显示上次结果与失败原因，可重试。
- 重复整理按 `(event_ref, time_kind, title)` 身份合并：用户已核对、已编辑、已完成或已忽略的事项原样保留，未被处理过的候选就地更新，新结果不再支持的未处理候选被移除。重试不会产生重复候选。
- 服务重启时 `running` 标记为失败（`INTERRUPTED`），保留上次成功结果。
- 正文超过单次预算（24000 字符）时只分析前缀，`coverage='partial'` 且由服务端强制写入未纳入的段落范围；模型自称 `full` 也会被覆盖为 `partial`。
- 候选事项生成规则：`registration_deadline`、`activity_start`、`assembly` 各自成为一条候选（不合并成一个 due）；`activity_end` 与 `other` 只在事件卡展示；没有可执行时间的事件，用第一条动作生成一条无截止时间的候选。`precision` 不是 `datetime` 时 `due` 为空，只保留 `raw_date` 与精度。

## 已知限制

- **真实模型输出未验证**：本机没有配置有效 `DEEPSEEK_API_KEY`。已用真实端点验证连通性与 401 错误分类（见下），但没有取得一次真实成功补全，因此提示词在真实模型上的质量、延迟与 token 用量未测。
- 模型若把 JSON 包在 ```json 代码块中会被判为 `INVALID_JSON` 并失败，不做宽松剥离。
- 数值巧合：若事件依据里恰好出现与错误时刻相同的数字（例如正文含“50米”而模型给出 `:50`），跨段落合并校验可能放过；界面始终显示 `raw` 原文与推导依据，最终由人工核对。
- 附件正文、图片、表格内容不在本期范围；分组、名单、泳道等只在附件中的信息一律标记为未解析。
- 规则提取沿用原关键词与保守日期逻辑，对没有“截止/报名/提交”等词的通知（如游泳通级测试规程）不产生候选事项，只给出附件未解析等待核对问题。
- 单账号资料上限 100 份、单次整理最多 40 条候选事项、事件最多 12 个。
- 旧版前端把“官方来源/发布日期/提示”写进 TXT 正文再上传。新版资料内容为“标题行 + 官方正文”，来源改为结构化保存，因此同一通知的 SHA 与旧资料不同：重新导入会生成一份新资料，旧资料及其已核对待办保留，需要用户自行删除旧资料。旧库的表结构迁移是增量的，资料、片段、待办与审计数据全部保留（已在真实库副本上验证）。

## 验证记录（三类分开）

1. **真实学校网页读取**：`python scripts/verify_notice_extraction.py --keyword 游泳 --output docs/notice-extraction-validation-2026-10-12.json`。2026-10-12 在跟随系统策略下用无凭据请求读取竺可桢学院办公网首页 5 条通知，命中《浙江大学竺可桢学院2026-2027秋冬学期游泳通级测试规程》（`ckc:3212562`，发布 2026-10-10，正文 759 字、26 段、1 个 PDF 附件）。使用临时数据库与合成学院字段，不是新的真实校园登录验证；附件未下载、未解析。
2. **模拟模型测试**：`python -m unittest tests.test_notice_analysis`（43 项）覆盖跨段归并、多场次对应、截止与活动时间分离、缺年/缺时、矛盾日期、附件才有细节、无附件、正文提示词注入、伪造引用与链接、空/截断/超时/限流/无效 JSON、账号隔离、重复整理去重、文档更新、已审核事项保留、删除与切换账号后的迟到结果、模型调用不持事务、候选事项生成与上限。前端 `node --test tests/frontend-notice-analysis.test.cjs`（9 项）覆盖同意开关、事件卡、证据跳转、失败与部分结果展示、切换账号忽略迟到响应。
3. **真实模型调用**：**未验证成功补全**。已在浏览器中用无效密钥触发一次真实请求，`https://api.deepseek.com/chat/completions` 返回 HTTP 401，适配器分类为 `AUTH_REJECTED`，界面显示失败原因、保留离线规则结果、不生成候选事项，页面与数据库中都不含密钥。取得授权密钥后可运行 `python scripts/verify_notice_extraction.py --keyword 游泳 --use-model --output <报告>`，报告会记录延迟、token 用量、输入是否含附件 URL 与官方链接、经校验的事件与候选事项；模型输出不是标准答案，仍需按报告中的 `manual_checklist` 对照原文人工核对。

界面验证在临时数据目录（`CAMPUS_DATA_DIR=.test-tmp/ui-demo`）中用本地合成账号完成：真实通知同步 → 保存到资料库 → 来源卡与附件清单（标记未解析）→ 离线规则整理 → 同意开关门控 → 真实端点 401 失败态。未修改用户既有资料库。

## 游泳通级测试：人工核对字段清单

依据真实公开正文（行号为正文内非空行序号，完整清单见 `docs/notice-extraction-validation-2026-10-12.json` 的 `manual_checklist`）。下表“原文依据”为逐字摘录，不是模型输出；`human_verdict` 需人工填写。

| 字段 | 原文依据（逐字） | 旧规则提取 | 新离线规则 | 需模型/人工确认 |
|---|---|---|---|---|
| 报名截止 | 正文未出现报名或截止表述 | 无 | 无 | 本通知没有报名截止；不得由发布日期或测试日期推导 |
| 测试日期与起止 | L3「2026年10月11日8:30-14:00。」 | 无 | 无 | 起止为 08:30–14:00；14:00 是结束时间，不是截止 |
| 检录时间 | L16「8:20开始检录，请各位同学携带有效的实体证件（学生证、身份证、校园卡三证之一）进行检录，并自备泳衣、泳帽、干毛巾。」 | 无 | 无 | 该段无日期，需与 L3 跨段合并为 2026-10-11 08:20，且必须标记为推导 |
| 测试开始 | L17「3.测试环节：8:30开始测试，设测试起点及测试终点。」（同段后续为分组出发说明） | 无 | 无 | 与 L3 的 8:30 一致；不得与检录 8:20 混为一条 |
| 地点 | L4「二、测试地点」+ L5「浙江大学紫金港游泳馆50米标准池（浅水区）」 | 无 | 无 | 标签与值分处两段，需要跨段归并 |
| 适用对象 | L10「未选游泳课的竺可桢学院2026级大一新生（包括未完成游泳通级测试的竺院学生）。」 | 无 | 无 | 学院匹配不等于本人符合资格，需人工判断 |
| 携带物品 | L23「1.参与测试学生需随身携带学生证、身份证、实体校园卡之一进行检录。」、L24「2.佩戴泳帽、泳镜，穿着泳装。」 | 无 | 无 | 属于需要用户执行的动作 |
| 附件 | L25「九、测试秩序分组表见附件：26-27学年秋冬学期竺院游泳通级测试检录表.pdf」 | 无 | 待核对问题：附件未下载、未解析 | 分组与秩序只在 PDF 中；**附件未读取**，不得声称已知分组 |
| 发布日期 | 页面 `发布时间：2026-10-10` | — | — | 只作为来源元数据，禁止当作活动时间 |

旧规则在这条真实通知上产生 **0 条**候选事项（正文没有“截止/报名/提交”等关键词），新离线规则同样不产生候选，但会保留结构化来源、附件清单与“附件未解析”的待核对问题。事件、分场次时间、地点与适用对象的归并依赖模型整理，其真实输出尚未验证。
