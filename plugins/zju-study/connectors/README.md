# connectors（智能体 A：平台采集与同步）

> 2026-10-11 收尾更新：个人使用请按根目录 README 和 `docs/coord/10-收尾修复与使用说明.md` 启动。默认网页为 real；统一启动器使用 personal.db，不 seed。以下旧章节中的样例导入仅用于开发演示；智云已有独立会话、详情和按需字幕接线，当前真实账号联调待确认。


负责学在浙大、智云课堂的接入调研、适配器、增量同步与故障恢复。目录所有权见《00-协作总纲》§3。

## 当前阶段：收官轮（v1.7 最终交付轮）。A20–A22 完成；A23 按任务书 P3 裁剪**未执行**（显式声明）

**M1 与 M2 起步轮已全部并入 main；收官轮把"可行"变成"可用"：A20（受控浏览器会话 → transport → XzzdAdapter → B 的常驻服务 + 进程桥接，已并入 main@a8df35e）、A21（一门真实课程真实同步：两次归一化指纹逐字节一致、失败路径实测）、A22（真实资源获取：full 资源真实下载、重取 sha256 一致、PD-1 64MB 护栏实测）。结论、观察条件、证据路径与未做项见 `docs/A21-A22-真实同步与获取记录.md`（本模块收官主文档）。** 真实同步/获取均为受控会话只读低频（≤0.5 rps、单课程、单资源逐项），凭据不经我方代码、仅内存持有；实测证据与暂存件全部在仓库外（%LOCALAPPDATA%）。**zykt 全平台未真实化**（A23 未执行）——仍为骨架 + 合成测试，勿据此认为可用。适配器测试仍全部为离线合成响应（脚本化 transport，未触网、无真实凭据）。

| 状态 | 内容 |
|---|---|
| 已实现 | `adapters/base.py`（五操作面协议 + AdapterError）；`adapters/mock.py`（样例驱动、确定性可重放；场景 default/empty/version_update/partial_failure/auth_required/network_error/blocked）；`adapters/transport.py`（可注入 HTTP 传输；会话由调用方持有，适配器不接触凭据）；`adapters/xzzd.py`（A5：时间 ISO→UTC 归一 / access_level 真值表 / status_code→submission_status / denial 不抛错 / 分页信封消费 / 错误映射；恒 mode=resync）；`adapters/zykt.py`（A6 骨架：epoch→ISO / video_list 归一 / XML 解析 / 字幕映射 / 回放到期消失非故障）；`sync.py`（A7 两阶段编排：有界退避 / AUTH_EXPIRED 不循环 / 0.5rps 限速 / 协作式取消 / 单项隔离）；`redact.py`（记录前脱敏统一入口 + logging 兜底过滤器）；`browser/session_runner.py`（A20–A22 会话侧服务：serve/check/sync/fetch/list-courses；共享可见性缓存接通 B 两阶段 sync→fetch；下载暂存）；`bridge.py`（A20：B 进程侧 stdlib HTTP 桥接 check/sync/fetch 到会话服务）；`adapters/xzzd.py` 的 `fetch_resource` 真实下载链路（A22：发签 → 媒体链流式下载 → sha256 暂存件；PD-1 64MB 元数据+中途双重护栏） |
| 已验证（未登录层） | 两平台登录入口与 SSO 跳转链路（docs/A1 §3.1–3.2） |
| 已验证（登录态只读，2026-10-09） | xzzd 课程/章节/活动/课件/作业接口与时间格式；zykt 场次目录/回放详情/转写字幕/PPT/学习记录（docs/A1 §3.3–3.4） |
| 契约 | v0.1 已冻结（B，`contracts/`）；A 侧**正式确认回执**（接受）见 `contracts/reviews/2026-10-09-A-契约v0.1确认回执.md`，细节见 docs/契约v0.1-A侧确认.md |
| 跳转式登录（A12） | 已交付设计（docs/A5）：**两平台均不可回跳（工程判定）→ 降级方案**升级为「受控浏览器 + 持久化 profile」（A14 v0.2 §4）；CR002 已由 B 裁决并入契约 v0.1 |
| 受控浏览器（A17，2026-10-10） | **可行（实测）**：`browser/controlled_login.py` 原型——独立持久化 profile + 可见 Edge 窗口、用户在官方登录页自行登录（凭据不经我方代码）、cookie 仅键名读取（值不落盘）、只读重放 200（页面内与 requests 链双证）、跨浏览器重启可复用；失效形态＝302 重定向链至登录页（`auth_required`）。命令/时序/判定依据见 `docs/A6-受控浏览器登录可行性验证.md` |
| 会话失效与处置（A18，2026-10-10） | 平台登出端点直接导航**不**使会话失效（两次独立观测）；"会话失效"形态经**凭证清理**取得（`reset-session` 子命令，仅限独立 profile）；处置后本地无可用会话物料 |
| 真实同步与获取（A21/A22，2026-10-10） | **已实测（真实数据）**：A21 真实课程两次同步 stats 6/6/0/0、归一化指纹逐字节一致（剔除全部 observed_at 与游标后 `323afc775e093580`）、不存在课号 → `RESOURCE_NOT_FOUND`；A22 三个 full 资源（.zip 课件包，2.4–3.7MB）真实下载成功（sha256 留痕、ZIP 内容核验）、同资源重取 sha 一致（确定性）、PD-1 64MB 护栏真实复现（1MB 参数化 → denied + message 明示）；完整证据、观察条件与未做项见 `docs/A21-A22-真实同步与获取记录.md` |
| 样例校验 | `connectors/samples/` 9 份经契约 schema 全量校验通过（`connectors/tests/validate_samples.py`） |
| 模拟验证（全部离线合成） | **205 项全绿**：mock 契约 30（`test_adapter_contract.py`）；同步编排 21（`test_sync.py`）；脱敏 21（`test_redact.py`）；xzzd 60（`test_xzzd.py`，含 A22 下载链、协议 isinstance、schema 校验）；zykt 骨架 28（`test_zykt.py`）；受控浏览器 12（`test_controlled_browser.py`，判定真值表/脱敏/开关）；会话运行器 15（`test_session_runner.py`）；桥接 12（`test_bridge.py`）；传输层 4（`test_transport.py`，本地 stdlib HTTP 真实数据流）；环境自检 2（`test_env_integrity.py`，X5：requests/jsonschema 缺失显式失败，不静默跳过）；**mock 输出标注 `connector_version=0.1.0-mock`、xzzd 真实适配器 `0.2.0-xzzd`；测试全绿≠真实采集成功（真实结论以 A21/A22 文档为准）** |
| 未验证 | **zykt 全平台未真实化**（A23 按任务书 P3 未执行；真实会话/受控会话均未联调，保持"未实测"口径）；真实受限（preview_only）资源的 fetch denial 分支未遇真实样本（A5b 已实测发签 403 门控形态；分支链路为合成测试覆盖）；真实空课（零 upsert 条目）未遇；version_update / `deleted` 置位未自然观测；会话闲置/绝对阈值、异地失效、更高频率阈值（A1 §6）；`.zip` 资源与 B `/imports` 白名单（pdf/pptx/docx）冲突待裁决（见收官文档 §5）。**逐项清单与所需输入见 docs/A1 §5.1 与 §6 及收官文档"尚未验证"一节** |

## 依赖与测试运行（X5 补注，2026-10-11，B 执行协调者指派项）

- **依赖声明**：`requirements.txt`（本目录，此前无声明）——`requests`（`tests/test_transport.py` 模块级导入；缺失曾致套件在收集期整体中断、表象像"代码坏了"）、`jsonschema`/`referencing`（`tests/validate_samples.py` 契约样例校验）、`pytest`（运行器）。安装：`python -m pip install -r connectors/requirements.txt`。
- **全套件（仓库根，全离线）**：`python -m pytest connectors/tests -q`（期望 **205 passed + 7 subtests**；或 `python -m unittest discover -s connectors/tests`）。
- **环境自检**：`tests/test_env_integrity.py`——缺上述依赖时显式失败（fail-fast）并给出安装指引，不静默跳过。
- **真实受控浏览器会话阶段**（A17/A20–A22，会话侧独立环境）另需 `playwright`（全部延迟导入，离线测试不依赖），用法见 `docs/A6`、`docs/A21-A22`。

## 目录结构

```
connectors/
  README.md                    本文件
  requirements.txt             测试与联调依赖声明（X5 补：requests/jsonschema/referencing/pytest）
  docs/
    A1-接入能力验证报告.md      调研 + 实测结论、逐项验证清单、剩余验证步骤
    A2-连接方式选型.md          路线对比、推荐架构与安全要求
    A3-适配器接口草案.md        经 B 审核冻结为契约 v0.1 的接口提案
    A4-增量同步与故障恢复设计.md 变更识别、游标、删除语义、重试退避、取消恢复
    A5-跳转式登录设计.md          A12/A14：回跳可行性结论、降级方案 v0.2（受控浏览器 + 持久化 profile）、B12 端点建议
    A6-受控浏览器登录可行性验证.md A17/A18：结论（可行）、六条判定对照、启动命令、时序、失效形态、证据文件表
    A9-A到B上传通道对齐.md        A9：/imports 通道形态、字段映射、幂等键规则、凭据红线（含未验证声明）
    A10-为D提供字幕与文档输入契约.md A10：字幕字段语义、pending/unknown 约定、文档输入路径
    A21-A22-真实同步与获取记录.md 收官轮：结论、四级状态、观察条件、证据摘要、交付 B（415 冲突待裁）、
                               可复现命令、未做/未验证显式清单
    契约v0.1-A侧确认.md          A 侧确认、审核跟进落实、交叉验证结果
  samples/                     脱敏标准化样例（含场景矩阵，见 samples/README.md）
  browser/                     controlled_login.py：受控浏览器登录原型（A17；Playwright + 独立持久化
                               profile；verify/logout-test/reset-session 子命令；仅脱敏 JSON 证据）；
                               session_runner.py：会话侧连接器服务（A20–A22；serve/check/sync/fetch/
                               list-courses 子命令；共享可见性缓存；下载暂存；证据仅脱敏 JSON）
  bridge.py                    A20：B 进程侧桥接——stdlib HTTP 转发 check/sync/fetch 到会话服务
  adapters/                    base.py 五操作面协议；mock.py 样例驱动确定性实现；
                               transport.py 可注入 HTTP 传输（会话由调用方持有）+ 流式下载（A22）；
                               xzzd.py 学在浙大真实适配器（A5；A22 真实下载链路）；zykt.py 智云课堂骨架（A6）
  sync.py                      同步编排（A7）：两阶段分离、有界退避、限速、取消、单项隔离
  redact.py                    记录前脱敏统一入口（URL 查询参数/Cookie/Authorization/JWT/CAS 票据）
  tests/                       validate_samples.py（样例契约校验）；fakes.py（脚本化 transport）；
                               test_adapter_contract.py（30）；test_redact.py（21）；test_sync.py（21）；
                               test_xzzd.py（60）；test_zykt.py（28）；test_controlled_browser.py（12）；
                               test_session_runner.py（15）；test_bridge.py（12）；test_transport.py（4）；
                               test_env_integrity.py（2，X5：requests/jsonschema 缺失显式失败）
```

## 交接记录 · 收官轮（2026-10-10，A20 会话接入同步编排 + A21 真实同步 + A22 真实获取 + A24 能力矩阵终稿；按总纲 §8 格式）

- **结论**：**收官轮把"可行"变成"可用"：A20/A21/A22 全部完成，且 A21/A22 为真实数据实测。** A20 受控浏览器会话接入同步编排（会话侧服务 + B 进程桥接 + 共享可见性缓存，已并入 main@a8df35e）；A21 一门真实课程真实同步——两次独立同步归一化指纹逐字节一致（确定性实测）、失败路径（不存在课号 → `RESOURCE_NOT_FOUND`）实测；A22 真实资源获取——3/3 full 资源真实下载成功、同资源重取 sha256 一致、PD-1 64MB 护栏真实复现（1MB 参数化 → denied + message 明示）。完整证据、观察条件与未做项见 **`docs/A21-A22-真实同步与获取记录.md`**。**A23（zykt 真实化）按任务书 P3 裁剪未执行**——zykt 全平台仍为骨架 + 合成测试，勿据此认为可用。
- **变更文件**（A20 主体已并入 main@a8df35e；下列含该批与本轮收口）：`adapters/xzzd.py`（A22 真实下载链路 + 能力升级 supported）、`adapters/transport.py`（`DownloadTransport` 协议 + `RequestsTransport.download` 流式/原子改名/中途护栏）、`browser/session_runner.py`（A20 服务 + A21/A22 直驱；含 fetch `--download-dir` 修复）、`bridge.py`（A20，B 进程侧桥接）、`tests/test_session_runner.py`（15 项）、`tests/test_bridge.py`（12 项）；**本轮新增** `tests/test_transport.py`（4 项）、`docs/A21-A22-真实同步与获取记录.md`；**本轮更新** `samples/capabilities.json`（A24 终稿：xzzd `attachment_download` unknown→supported、版本 0.2.0-xzzd）、`docs/A1`（v0.4 收官注记）、本 README。
- **输入输出契约**：fetch 输出以 `fetch-outcome.schema.json` 为准（handle=`local_file`：`ref`/`sha256`/`size`/`expires_at`，暂存件 24h TTL）；**契约观察点**：denial.reason 枚举无"策略超限"档，PD-1 超限以 `permission_denied` + message 明示（供 B 裁决是否入 CHANGELOG）。sync-result 恒 `mode=resync`（平台无增量接口）。
- **运行方法**：
  ```bash
  # 会话侧服务（B 链路接入，关键路径；浏览器窗口留给用户完成登录）
  PYTHONIOENCODING=utf-8 python -m connectors.browser.session_runner serve --b-api http://127.0.0.1:8000
  # 一次性直驱（留证/排障；全部只读低频）
  PYTHONIOENCODING=utf-8 python -m connectors.browser.session_runner list-courses --wait 300
  PYTHONIOENCODING=utf-8 python -m connectors.browser.session_runner sync --course-id <平台课号> --wait 300 --rate-limit 0.5
  PYTHONIOENCODING=utf-8 python -m connectors.browser.session_runner fetch --course-id <平台课号> --resource-id <平台资源ID> --wait 300
  # 证据（脱敏 JSON）默认输出到 %LOCALAPPDATA%\zjulearn\controlled-browser\a20-results\（仓库外）
  ```
- **测试结果**：connectors 全部 **203 项全绿**（30+21+21+60+28+12+15+12+4；其中传输层 4 项为本地 stdlib HTTP 真实数据流）；`validate_samples.py` 与 `contracts/examples/validate_examples.py` 双端校验通过。**真实数据实测**（2026-10-10 22:24–22:36，受控会话、只读低频 ≤0.5rps、单课程）：同步 stats 6/6/0/0 ×2、获取 3/3 ok + 重取一致 + 护栏复现——明细见 `docs/A21-A22-真实同步与获取记录.md`。
- **已知限制**：① zykt 未真实化（A23 未执行，P3）；② 真实受限（preview_only）资源的 fetch denial 分支未遇真实样本（合成覆盖；A5b 已实测发签 403 形态）；③ 真实空课未遇（两候选空课均有作业条目）；④ version_update / `deleted` 置位未自然观测；⑤ 会话闲置/绝对阈值、异地失效未测（沿用 A1 §6）；⑥ `.zip` 资源与 B `/imports` 白名单冲突待裁决。
- **待协作事项**：
  - → B：合并本批提交；**B17 输入**＝A21 真实归一化 sync-result（含真实课号，不入仓库提交物；本机路径见收官文档 §5）；**415 冲突裁决**（真实资源为 .zip，B `/imports` `EXTENSION_MIME` 仅 pdf/pptx/docx）；serve 接入需配置 `--download-dir`（默认仓库外）与 `STUDY_SYNC_ADAPTERS`。
  - → C：PD-1 超护栏 denial message 的界面文案；handle=`local_file` 的"已获取"态展示（暂存件 24h TTL）；会话 guidance 沿用 A5 v0.2 §4.4 文案。
  - → D：真实资源为 .zip 课件包（HTML/JS/CSS，A21/A22 实测）——解析支持与否待 B/协调者裁决后同步；本批无字幕通道影响。**D15 依赖已就绪**：A21 真实数据＝3 资源（.zip、`kind=other`、全 `full`）+ 3 作业 + 0 回放（回放为 0 是当次真实观察，非缺数据），经 B17 落库后即可执行（证据路径见收官文档 §5-1）。
  - → 用户/协调者：A23 未执行（P3 显式声明）；受控浏览器会话仍未处置（保留中，可用 `reset-session` 清空）。

## 交接记录 · M2 起步轮（2026-10-10，A17 受控浏览器登录可行性验证 + A18 失效形态 + A19 能力矩阵；按总纲 §8 格式）

- **结论**：**受控浏览器登录：可行。** 六条成功判定全满足（启动可见持久化 profile 浏览器 / 导航至平台真实登录入口 / 用户自行登录 / 可读会话 cookie（仅内存）/ 真实只读请求 200 非 302 / 连续两次独立登录成功），四条失败判定均未出现。报告 `docs/A6-受控浏览器登录可行性验证.md`（§0 结论、§1 判定对照、§3 时序、§4 判定依据、§11 证据文件表）。
- **变更文件**：新增 `browser/__init__.py`、`browser/controlled_login.py`（A17 原型，`verify`/`logout-test`/`reset-session` 三子命令）、`tests/test_controlled_browser.py`（12 项，全离线）；新增 `docs/A6-受控浏览器登录可行性验证.md`；更新 `adapters/xzzd.py`、`adapters/zykt.py`、`adapters/mock.py`（恢复文案对齐 B14 定稿"登录已失效，请在连接器窗口完成登录后重试"，CR002 闭环）；`samples/capabilities.json`（A19：新增 `session` 能力键；xzzd=supported、zykt=unknown；文案与 `checked_at` 更新）、`samples/check-connection.json`（文案）；`docs/A1`（§3.5-6/10 补记、§1/§5/§5.1/§6 收口）；本 README。
- **输入输出契约**：原型输出＝**仅脱敏 JSON 证据**（cookie 键名/计数、状态码、判定、时序；**无 cookie 值、无 token、无完整 URL**，复用 `redact.py` 入口）；`capabilities.json` 新增 `session` 键（契约 `features` 为开放集合——**需 B 契约 CHANGELOG 记录**）。红线不变：凭据不进我方代码、会话仅内存持有不落盘不上传、独立 profile 不碰用户主浏览器、只读低频。
- **运行方法**：
  ```bash
  # 依赖：Python + playwright（1.63.0），本机 Edge（channel=msedge）
  PYTHONIOENCODING=utf-8 python -m connectors.browser.controlled_login verify --tag runN       # 登录可行性验证（留窗口给用户登录）
  PYTHONIOENCODING=utf-8 python -m connectors.browser.controlled_login logout-test --tag outN  # 登出端点失效形态观测
  PYTHONIOENCODING=utf-8 python -m connectors.browser.controlled_login reset-session --tag resetN  # 会话处置：清理凭证并实测失效形态
  # 证据（脱敏 JSON）默认输出到 %LOCALAPPDATA%\zjulearn\controlled-browser\a17-results\（仓库外）
  ```
- **测试结果**：connectors 全部 161 项全绿（含 `test_controlled_browser.py` 12 项：判定真值表/脱敏/子命令开关，全离线）；`validate_samples.py` 与 `contracts/examples/validate_examples.py` 双端校验通过。**真实浏览器实测**（2026-10-10 下午）：run3 全新登录（cookies 2→9→18→30，33.2s 检出，页面内 fetch 与 requests 链双路径 200）；run2 跨重启复用（2.2s 检出）；reset2/reset3 失效形态＝302 链至认证入口（`auth_required`）。
- **已知限制**：① zykt 受控会话未实测（保持 unknown）；② `attachment_download` 保持 unknown（M1 策略不开放下载，待 M2 决策）——**收官轮 A22 已实测升级 supported，见顶表**；③ 登出端点直接导航不使会话失效（两次观测）——失效形态经凭证清理取得；④ 会话闲置/绝对阈值、异地失效仍未测；⑤ 受控浏览器原型尚未接入连接器主流程（M2 集成项）——**收官轮 A20 已接入（session_runner + bridge），见顶表**。
- **待协作事项**：
  - → B：合并本批提交；契约 `CHANGELOG` 记录 `features` 开放集合新增 `session` 键；`contracts/examples` 中 A 侧两处样例文案（connections/error-responses 恢复文案）如需与 B14 定稿完全一致由 B 裁决同步。
  - → C：失效形态素材（302 链 → `auth_required`）与定稿文案已在 A1 §3.5-10 / A6 §6；受控窗口引导文案按 A5 v0.2 §4.4。
  - → D：无接口影响（字幕/文档通道不变）。
  - → 用户/协调者：受控浏览器验证需用户在场（仅登录动作）；本次实测会话已处置（reset-session 清空本地凭证），下次运行需重新登录。

## 交接记录 · M1-b（2026-10-09，A5 xzzd 真实适配器 + A6 zykt 骨架；按总纲 §8 格式）

- **变更文件**：新增 `adapters/transport.py`、`adapters/xzzd.py`、`adapters/zykt.py`；更新 `adapters/__init__.py` 导出；新增 `tests/fakes.py`、`tests/test_xzzd.py`（40 项）、`tests/test_zykt.py`（28 项）；本 README。
- **输入输出契约**：全部输出以 `contracts/schemas/` 为准（测试内逐对象 schema 校验）；错误码表用 `contracts/README`（AUTH_EXPIRED 不重试 / RATE_LIMITED 与 NETWORK_ERROR 有界重试 / UPSTREAM_CHANGED 报警降级）。适配器不持有会话与凭据：`XzzdAdapter/ZyktAdapter(connection_id, transport=...)`，`RequestsTransport(base_url, session)` 的 `session` 由调用方（用户会话持有层）提供。`resolve_external_id` 为 A9 内部课号→平台课号映射接缝。
- **运行方法**：
  ```bash
  python connectors/tests/test_xzzd.py   # A5：40 项（离线合成响应，不触网）
  python connectors/tests/test_zykt.py   # A6：28 项（同上）
  ```
  供 B 接入（真实适配器）：
  ```python
  from connectors.adapters.xzzd import XzzdAdapter
  from connectors.adapters.transport import RequestsTransport
  transport = RequestsTransport("https://course.zju.edu.cn", session=user_session)  # 会话由 B/D 的登录层提供
  adapter = XzzdAdapter("conn_x", transport=transport,
                        resolve_external_id=lambda internal: platform_id_map[internal])
  ```
- **测试结果**：xzzd 40/40、zykt 28/28、mock 30/30、sync 21/21、redact 21/21、样例校验通过；两适配器通过 `PlatformAdapter` 协议 isinstance 检查。**全部为离线合成响应**：A5 六项验收（时间归一、access_level 真值表、status_code 映射、denial 不抛错、分页消费、可注入会话驱动）与 A6 五项验收（epoch 换算、字幕映射、XML 解析、video_list 归一、回放到期消失非故障）均有对应测试。
- **已知限制**：xzzd `fetch_resource` 对 full 资源抛 `UPSTREAM_CHANGED`（下载链路未实测，A1 §5-5，不伪装可用）——**收官轮 A22 已实现真实下载（发签→流式→sha256），见顶表**；xzzd `sync_course` 恒 `mode=resync`（平台无增量接口，避免静默全量）；zykt `list_courses`/作业明确拒绝（A1 §5-4）；真实错误映射（200 HTML→auth_required 推断）待 A5b 校核。
- **待协作事项**：
  - → B：合并本批提交；A9 联调需要内部课号↔平台课号映射（`resolve_external_id` 接缝已留）；xzzd/zykt 接入 worker 时用 `RequestsTransport` + 会话持有层。
  - → C：适配器 `check_connection` 的 `auth_required.action.entry_url` 可直接驱动跳转登录页；错误码→界面文案沿用契约。
  - → D：zykt `get_transcript` 输出即 `transcript-sample` 结构；`fetch_resource` 对视频抛 UPSTREAM_CHANGED（红线 3，首版不批量下载）。
  - → 用户：A5b 真实会话联调与下载链路验证需授权时段（只读、低频）。

## 交接记录 · M1 收尾（2026-10-09，A9/A10/A11；按总纲 §8 格式）

- **变更文件**：新增 `docs/A9-A到B上传通道对齐.md`（A9）、`docs/A10-为D提供字幕与文档输入契约.md`（A10）；`docs/A1` 更新 §5（第 5/8/9/10/11 行补充待执行指针）+ 新增 §5.1（适配器实现与验证层级对照＝"尚未验证项"清单）+ §6 重构（逐项标注 已执行/待执行 与判定口径，A11）；本 README。
- **输入输出契约**：A9 对齐 B1 `/api/v1/imports`（multipart payload+files；幂等键 8–128、TTL 24h、同键换体 409；A 侧键规则 `a-import:{platform}:{course_id}:{external_id}:{content_hash[:16]}`）；A10 对齐契约 `recording.schema.json` 的 `transcript_status` 冻结枚举与 `samples/transcript-sample.json` 结构。
- **运行方法**：无需运行（文档批次）；相关样例可用 `python connectors/tests/validate_samples.py` 复验。
- **测试结果**：无代码变更；样例校验保持通过。
- **已知限制**：A9 通道**端到端未联调**（A 侧下载链路未实测，M1 不做上传客户端）——**收官轮 A22 后 A 侧下载已实测；上传端到端仍待 `.zip` 白名单裁决（见收官文档 §5）**；A10 字幕落库形态待 B 决定；A11 待执行项需用户授权时段（只读、低频）。
- **待协作事项**：
  - → B：按 A9 §4 接受 A 侧幂等键；确认元数据同步任务（`/connections/{id}/sync`）接入 `sync-result` 的 job 形态。
  - → C：会话失效恢复文案与 `ConnectionState.action` 待 A5b 校正（A1 §6.2）。
  - → D：按 A10 §2 状态表实现消费分支；如需字段扩展走 B6 变更模板。
  - → 用户/协调者：A5b 受控验证（A1 §6 清单）需授权时段；M1 阶段验收对照 A1 §5.1 与本文档各交接记录。

## 交接记录 · M1（2026-10-09，A2 + A12 + A3/A4 + A7/A8；按总纲 §8 格式）

- **变更文件**：新增 `contracts/reviews/2026-10-09-A-契约v0.1确认回执.md`（A2，接受含 C0 补录影响）；新增 `docs/A5-跳转式登录设计.md`（A12）；`docs/契约v0.1-A侧确认.md` 头部引用回执；新增 `adapters/base.py`、`adapters/mock.py`（A3/A4）、`sync.py`（A7 两阶段编排）、`redact.py`（A8）；新增 `tests/test_adapter_contract.py`（30 项）、`tests/test_redact.py`（21 项）、`tests/test_sync.py`（21 项）与包初始化文件；本 README。
- **输入输出契约**：五个操作面输出形状以 `contracts/schemas/` 为准（connection-state / capabilities / sync-result / fetch-outcome；list_courses 信封为 A3 §3.3 提案）。mock 全部由 `connectors/samples/` 驱动，虚拟时钟固定、游标确定性生成，同一调用序列字节级可重放。`sync.py` 只驱动适配器（只读），不落库、不做变更判定——持久化/版本递增/幂等由 B 侧实现。A12 兼容新增端点走 `CHANGE-REQUEST.md`（B6 模板建立后正式提报，A5 §5）。
- **运行方法**：
  ```bash
  python connectors/tests/validate_samples.py        # 样例 → 契约全量校验
  python connectors/tests/test_adapter_contract.py   # mock 适配器契约测试（30 项）
  python connectors/tests/test_redact.py             # 记录前脱敏单测（21 项）
  python connectors/tests/test_sync.py               # 同步编排与故障恢复单测（21 项）
  ```
  供 B/C/D 调用：
  ```python
  from connectors.adapters.mock import MockAdapter
  adapter = MockAdapter(platform="xzzd", connection_id="conn_x", scenario="default")
  state = adapter.check_connection()          # ConnectionState
  result = adapter.sync_course("crs_3f8a21")  # SyncResult（凭 result["cursor"]["next"] 继续增量）

  from connectors.sync import SyncOrchestrator, SyncCancelled
  orch = SyncOrchestrator(adapter)            # 默认 0.5 rps、重试 3 次、退避 1s 起
  result = orch.sync_course("crs_3f8a21", since_cursor=saved_cursor)  # 阶段一：元数据
  results = orch.fetch_contents(["res_01a7"])  # 阶段二：内容逐项获取（独立触发）
  ```
- **测试结果**：契约测试 30/30 通过（含：连续同步不重复、同名新版本、空/失败不产生删除、游标失效 resync、受限资源 denial、失败恢复语义、schema 全量校验、重放确定性）；同步编排 21/21 通过（含：退避序列 1s/2s、重试耗尽上限、AUTH_EXPIRED 单次上抛不等待、0.5rps 限速与可配置、前置/条目间取消与已完成断点、凭游标恢复、两阶段分离、单项隔离与连接级中止、游标原样透传）；脱敏 21/21 通过（含 `?token=` 形态、JWT、CAS 票据、logging 兜底）；样例校验通过。mock 无真实网络请求。
- **已知限制**：`list_courses` items 采用样例实体形态（含内部 ID 占位；真实适配器按与 B 联调确认，A9）；mock 的 `scenario=partial_failure` 数据即 `samples/sync-result.json`（游标为确定性链）；真实适配器未实现（M1-b）。`sync.py` 取消为协作式（检查点=每次平台调用前），不支持硬中断；游标只在整门课程成功后由调用方推进。
- **待协作事项**：
  - → B：补合 `agent/a@1963bea`（resources.json 补 chunk_count，否则 main 上样例校验失败）；mock 适配器与 `SyncOrchestrator` 可直接接入 worker（`scenario` 选择语义见 `adapters/mock.py` docstring）；B6 模板 → A 正式提报变更（A5 §5）；B12 端点形态裁决。
  - → C：mock 的 `auth_required`/`network_error`/`blocked` 与 `partial_failure` 场景可作界面态 fixtures；C4 降级文案与"能/不能展示"边界（A5 §4.3、§7）。
  - → D：`transcript-sample.json` 为字幕输入；mock `recordings` upsert 的 `transcript_status` 枚举（pending/ready/unknown）用于解析调度态。
  - → 协调者/用户：M1 门槛第 9 项"自动回跳"按 A5 §0.1 口径调整验收（降级流程）。

## 交接记录 · M0 收尾（2026-10-09，任务 #10；按总纲 §8 格式）

- **变更文件**：`samples/` 9 份对齐契约 v0.1 冻结命名并升级为实体形态；`docs/A1` 升 v0.2（新增登录态只读实测 §3.3–3.4，更新 §1/§5/§6/§8）；新增 `docs/契约v0.1-A侧确认.md`；新增 `tests/validate_samples.py`；本 README。
- **输入输出契约**：契约 v0.1（B 冻结，`contracts/schemas`）；`connectors/samples/` 为 A→B 载荷参照；能力三态/错误/恢复语义见 A3/A4。
- **运行方法**：`python connectors/tests/validate_samples.py`（样例对契约全量校验，依赖 jsonschema）。
- **测试结果**：9 样例文件对 19 份 schema 全量通过；B 侧 `contracts/examples/validate_examples.py` 全量通过（16 文件、51 对象）——双端样例交叉一致。
- **已知限制**：平台接口为私有 API（无公开文档、可无预告变更）；剩余未验证项见 A1 §5；样例均为虚构数据。
- **待协作事项**：
  - → B：确认本目录《契约v0.1-A侧确认.md》；两点实测反馈（时间基准异源、access_level 映射真值表）供 v0.2 参考；samples 可直接作为导入 fixtures。
  - → C：capabilities.json 渲染能力态；check-connection.json/errors.json 实现登录恢复与错误提示；A1 §3.3–3.4 的实测枚举可对齐界面文案。
  - → D：transcript-sample.json 为字幕时间轴输入（zykt 实测含中英双语，A1 §3.4-5）；`pending/unknown` 处理约定见 A4 §8。
  - → 用户：A1 §6 剩余受控验证（会话失效/下载 token/限速）需授权时段；M2 扩展安装方式待确认（A2 §7）。

## 红线提醒（贯穿本模块）

1. 不保存学校账号密码到服务端；会话不出浏览器（A2 §1）。
2. 不请求用户无权限的课程，不绕过任何访问限制。
3. 首版不批量下载视频；受限资源只返回"明确不可获取原因"。
4. 日志与测试不保存真实凭据、签名链接、未脱敏个人信息（A4 §10）。
5. 日志/遥测绝不记录完整 URL 与 Referer——智云实测有 JWT 入查询参数（A1 §3.4-9）；统一"记录前脱敏"入口。
