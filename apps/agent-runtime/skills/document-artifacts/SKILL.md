---
name: document-artifacts
description: 把已读取的资料整理成可下载的中文 PDF、Excel 或 Markdown 成果，保留来源和真实数据类型。
---

先用文件或网页工具读取用户指定材料，确定需要的内容与格式。不要把工具调用日志、应用内部路径或未核实的日期复制进成品。

本项目**不向模型提供 Shell / 代码执行**，但提供固定的文档导出能力 `artifacts.export`：
渲染程序、输出目录和字体都由服务端决定，你只提交数据。它可产出真正的 `.pdf`、`.xlsx`
和 `.md`，成功后返回本会话可下载的文件 ID、名称、字节数与 sha256。

`artifacts.export` 的输入只有数据：

- `title`（必填）、`subtitle`、`file_stem`（可选，仅文件名，不能含目录或 `<>:"/\|?*`）。
- `formats`：`pdf` / `xlsx` / `md` 的任意组合；`pdf`、`md` 需要 `sections`，`xlsx` 需要 `sheets`。
- `sections`：`heading`、`paragraphs`、`bullets`、`table{columns,rows}`；表格每行单元格数必须等于列数。
- `sheets`：`name`（1–31 字符、唯一、不含 Excel 禁用字符）、`columns`、`rows`。
- 单元格只接受文字、数字、布尔、`null` 和明确的日期对象 `{"date":"2026-10-20"}`。
- `anchors`：来源锚点（`source`/`resource_id`/`version`/`locator`/`chunk_id`/`note`），会渲染进“来源”。

明确不允许：

- Python/JS/Shell 代码、命令、表达式、模块名、可执行程序、任意输出目录、字体路径、远端 URL；
- 任意公式。以 `=` 开头的字符串按**普通文本**原样保留，不是公式，也不会有计算结果；
- 猜测缺失事实。材料没给日期就写“未知”，不要用当前日期补全；没有来源就不要编造来源。

实践建议：

- 报告、清单：`formats=["md"]` 或 `["pdf","md"]`；表格用 `sections[].table`，未知值写“未知”。
- 需要给表格工具时：`formats=["xlsx"]`，数字保持数值类型、日期用 `{"date":...}`。
- 交付后无需再手工 `publish_file`：导出成功即已发布到本会话文件区，可直接下载。
- 表格很大时按行数上限拆分文档，不要靠压缩内容绕过限制。

执行与超时（服务端实际强制）：

- 渲染在服务端自有的子进程里执行，你无法选择或替换程序；渲染截止时间为 15 秒，加上进程回收仍
  严格短于域工具的 20 秒围栏，所以你不会先看到工具超时而后端还在渲染。
- `ARTIFACT_TIMEOUT` 表示渲染子进程已被**终止并回收**，本次**没有任何文件交付**。必须如实说明
  “本次没有交付”，不得把超时写成成功；确有必要时可以缩小内容后再试一次，但仍然以真实回执为准。
- 分批发布是**全有或全无**：服务端先渲染出全部格式，再逐个发布；发布中途失败会撤回本次新建的文件。
  此时 `ARTIFACT_PUBLISH_FAILED` 且 `retryable=true`，表示本会话没有新增可下载文件，可以安全重试同一调用。
- 若错误详情里仍列有 `details.delivered` / `retained_file_ids`（已发布且无法撤回，`retryable=false`），
  这些文件就是**真实可下载的交付物**：不要再重复同一导出调用，也不要说“什么都没交付”，直接使用它们
  并如实说明批次未完成。

复习卷生成的身份与复用（服务端实际强制）：

- `reviews.generate` 的业务身份 = 本会话 + 课程 + 模式 + 服务配置 + 冻结材料快照指纹 + 备注 + 约束。
  `wait_seconds` 是观察窗口，不是身份的一部分。
- 同一业务请求会**复用**已受理的 job，而不会重复受理或重复计费。复用会在响应里标明
  `data.reused=true`，`data.reused_reason` 只取以下三个字面值之一：
  - `identical_request_in_flight`：同一会话、同一业务指纹，原 job 仍在 `queued`/`running`；不新建 job，也不重复计费；
  - `identical_request_completed`：同一会话、同一业务指纹，原 job 已 `succeeded`；直接返回那份已完成试卷
    （`data.paper.revision` 是真实版本），不再为同一份卷重复付费；
  - `same_call_id`：同一可信调用 ID 的服务端重放（通常先由本轮工具缓存直接应答）。
- 看到 `data.reused=true` 时**不要再次调用 `reviews.generate`**：用返回的 `job`/job ID 走 `jobs.read`
  观察真实状态，再用 `reviews.read` / `reviews.export` 使用**同一** paper/revision。复用既不是成功也不是
  失败，只以 job 的真实状态为准。
- 只有用户明确要求“再出一份全新试卷”时才传工具参数 `new_generation=true`（这是工具参数级信号，不是界面选项）。
- 先前的 job 为 `failed` / `cancelled` / `partial_failed` 时不会复用，会正常新建，便于恢复。
- 观察真实进度要省模型轮数：真实 worker 的实测耗时是 15–45 秒且波动大，单次工具调用（域围栏 20 秒）不可能覆盖，
  所以正确的做法是 `jobs.read` 带 `wait_seconds=14` 有界等待 2–3 次，而不是每次立刻返回、反复轮询。
  等待窗口按该 job 的受理时刻计算并有服务端总上限，重复调用不会重置；已超过上限时立即返回真实状态并给出 warning。
  仍未终态就如实说“后台仍在生成”，不要因此重复 `reviews.generate`，也不要声称已完成。
  同步类（如 `connections.sync`）等其他 job 只读取当前状态，不等待。

复习卷相关：PDF/XLSX 只负责把已有内容排版。出卷本身走 `reviews.preflight` →
`reviews.generate` → `jobs.read` → `reviews.read` → `reviews.export`，不要用
`artifacts.export` 编造题目、答案或材料依据。失败会返回结构化错误（`INVALID_INPUT`/`ARTIFACT_TIMEOUT`/
`ARTIFACT_RENDER_FAILED`/`ARTIFACT_PUBLISH_FAILED`/`DEPENDENCY_UNAVAILABLE`）。
