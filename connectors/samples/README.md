# 脱敏标准化样例（connectors/samples）

- 本目录样例为**虚构脱敏数据**：无真实账号、真实课程 ID、真实文件或真实签名链接。
- 例外：`real-2026-10-10/` 子目录为 **2026-10-10 登录态只读实测**生成的真实脱敏样例（数值与字段形态实测、标识全部脱敏、日期整体平移 -30 天）；边界声明见该目录 `PROVENANCE.md`。
- 用途：M0 阶段让 B/C/D 用同一套样例对齐数据模型、界面态与计划输入（总纲 M0 门槛："相同样例可被各模块读取"）。
- 契约基线：**契约 v0.1（B 冻结，见 `contracts/`；freeze 落点 main@f5b4e46，现含 main@cb99446 增补）**。字段名已按冻结对账表对齐（`size_bytes`/`mime_type`/`source_updated_at`、`left/right_course_id`、`mode` 等）；`parse_status` 等 B/D 侧字段不出现于适配器载荷。
- 校验：`python connectors/tests/validate_samples.py`（对 `contracts/schemas/` 全量校验；2026-10-09 通过，19 份 schema）。

## 文件清单

| 文件 | 内容 |
|---|---|
| capabilities.json | 两个平台的能力结构（三态：supported/unsupported/unknown/preview_only），2026-10-09 登录态实测后已升级部分状态 |
| courses.json | 3 门课程 + 1 组跨平台映射候选（含证据与置信度，字段 `left/right_course_id`） |
| resources.json | 7 个资源：含同名新版本、重复文件、受限预览 |
| assignments.json | 4 个作业：已延期、无截止时间、已提交、未提交 |
| recordings.json | 3 个回放：无字幕（pending）、有字幕（ready）、字幕能力未验证（unknown） |
| transcript-sample.json | 字幕时间轴片段结构（`start_seconds/end_seconds/text/translated_text`，供 D 使用） |
| sync-result.json | 一次增量同步信封：mode + cursor + upserts + 显式删除 + 单项错误 + stats（部分成功语义） |
| check-connection.json | 连接状态样例：ok 与 auth_required（含用户恢复动作），对齐 connection-state.schema.json |
| errors.json | 5 类错误：AUTH_EXPIRED / PERMISSION_DENIED / RESOURCE_NOT_FOUND / NETWORK_ERROR / RATE_LIMITED |
| real-2026-10-10/ | 真实脱敏样例（A5b 实测 2026-10-10）：2 门课 28 条附件资源 + 62 条作业；raw-key-unions.json 为平台载荷字段全集（键名）；详见 PROVENANCE.md |

## 场景覆盖矩阵（对照总纲 §6 验收样例）

| 场景 | 位置 |
|---|---|
| 两门课程 | courses.json（crs_3f8a21、crs_51c9d0；另有 crs_9b6c40 用于映射候选） |
| 跨平台课程映射候选（低置信度交用户确认） | courses.json → mapping_candidates（confidence 0.86） |
| 重复文件（同内容不同 ID） | resources.json res_01b2 / res_01b3（content_hash 相同） |
| 同名新版本 | resources.json res_01a7（version=2）；sync-result.json 中作为 upsert 出现 |
| 已延期作业 | assignments.json asg_a101（due_at 2026-10-01，已过） |
| 无截止时间作业 | assignments.json asg_a102（due_at=null，不猜测） |
| 截止时间变化可追溯 | sync-result.json asg_a101 upsert（due_at 10-01 → 10-05；新旧值由 B 保留历史） |
| 无字幕回放仍可用 | recordings.json rec_r101（pending）；rec_r103（unknown） |
| 有字幕回放 + 时间戳结构 | recordings.json rec_r102 + transcript-sample.json |
| 登录失效可区分 | check-connection.json（auth_required）+ errors.json AUTH_EXPIRED |
| 无权限 / 不存在 / 网络失败 / 限流 | errors.json 五条（retryable 区分） |
| 单项失败不抹除整门课程 | sync-result.json（errors 1 条，upserts/deletions 照常） |
| 显式删除 vs 空响应 | sync-result.json deletions（reason=explicit_delete_signal）；语义见 A4 §4 |
| 受限资源仅预览 | resources.json res_01d4（access_level=preview_only） |
| 能力三态（未验证不承诺） | capabilities.json |

## 2026-10-09 登录态实测对齐说明（脱敏）

当日以本人账号对两平台做了**只读**观察（未下载文件、未提交作业、未记录真实数据值），样例按实测形态更新：

- **xzzd（学在浙大）**：课程列表（POST /api/my-courses，page 分页）、章节/活动（/modules、/activities）、课件列表（coursewares，page_size 生效）、作业（homework-activities；提交状态为 submitted(bool) + submitted_status(字符串，可空)；A5b 校正：不存在 `status_code` 字段）、活动详情（/api/activities/{id}）。文件元数据在 `uploads[]`（含 `allow_download`/`origin_allow_download`）；A5b 实测下载 mint 端点 `/api/uploads/{id}/download-url-for-ai` 可用（受 `allow_download` 门控：许可非真 → 403）且单文件只读探测 200（≥50 分钟跨整点桶有效）——**M1 策略仍不落下载**；`/oss/download?token=` 形态已废弃。
- **zykt（智云课堂）**：场次列表（catalogue XML：sub_id/title/status/start_at=epoch 秒）、场次详情（get-sub-info：`duration` 秒、多轨 `video_list`、`is_allow_video_drag` 场级开关、非 m3u8）、转写（search-trans-result：`BeginSec/EndSec/Text/TransText` 分段）、PPT（search-ppt，含 `created_sec`）、弹幕（danmu，`play_time`）。
- **时间格式换算**（供 B/D 参考，勿混用）：xzzd 平台字段为 ISO 8601 UTC（`Z` 结尾，毫秒精度），适配器**按平台原值（UTC）入库**（A5b 校正：不再声称"内部标准 +08:00"；契约对偏移不作强制，本目录 mock 样例的 `+08:00` 仅为格式示例）；zykt 多处为 epoch 秒字符串，适配器边界换算。
- **平台字段映射**（适配器内部）：xzzd `uploads[].size → size_bytes`、`uploads[].type` 为粗分类（实测值 document/video，非 MIME；仅当形如 MIME 时透传 → `mime_type`，否则 null）、活动 `updated_at → source_updated_at`；zykt 转写 `BeginSec/EndSec → start_seconds/end_seconds`、`TransText → translated_text`。

## 字段归属

**契约 v0.1 冻结**（可直接使用，落点见 `contracts/schemas/`）：
- Course / Resource / Assignment / Recording / SourceRef；
- 同步信封（sync-result：`mode`/`cursor`/`upserts`/`deletions`/`errors`/`stats`）；
- ConnectionState（check_connection 输出）、Capabilities、FetchOutcome、MappingCandidate 结构；
- 错误五元组 code/message/retryable/details/request_id；游标不透明约定。

**尚为 unknown（未实测或连接器策略不提供，见 capabilities.json）**：xzzd 受限资源真实拦截文案、显式删除信号样本、字幕/回放入口（未观察到）；zykt 附件下载、作业、删除信号（负面证据，未二次确认）。附件下载 mint 链路已实测但 M1 策略不落（见上）。

## 各智能体使用建议

- **B**：作为导入接口、幂等键与错误模型的 fixtures；`sync-result.json` 可直接跑幂等与删除语义的契约测试。
- **C**：渲染能力态（supported/unknown/…）、登录恢复引导（auth_required → entry_url）、同步"部分成功"展示。
- **D**：transcript-sample.json 为字幕时间轴结构；分块与摘要由 D 定义；`pending`/`unknown` 不得当作 `ready` 处理。

## 使用注意

1. 时间为 ISO 8601 带 +08:00 偏移；数据库存 UTC 由 B 转换。
2. 内部 `id` 与平台 `external_id` 是两类标识，禁止混用。
3. `content_hash` 为合成的 64 位十六进制示例值；`null` 表示未获取，不是"空内容"。
4. 样例中的平台入口 URL 为真实域名 + 虚构 ID，仅作格式示例，不指向真实资源。
5. `parse_status`/`file_id` 等 B/D 侧字段不出现在适配器载荷中（入库后由 B 设置）。
