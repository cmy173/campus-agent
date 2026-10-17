# Harness 第二轮返修独立验收

日期：2026-10-15。对象：`docs/audit/harness-next-round-rework2-result.md`。结论：**需修改，仅剩一个已复现的 P2 文件状态边界；主要执行链与上轮直接故障已通过。** 可以继续草稿 PR 评审，暂不宣称无阻塞可合并。

本轮没有修改业务代码，没有改写既有验收用例或实施方证据，没有提交、推送或创建 PR。新增的探针、结果与报告均在本目录。工作区既有未提交修改完整保留。

## 独立执行结果

| 验证 | 本次结果 | 证据 |
|---|---|---|
| 上两轮六项独立边界 | **6 passed** | `prior-boundaries.xml` |
| 完整 API | **696 passed / 5 既有 skipped**，276.21 秒 | `api.xml` |
| 前端完整套件 | **193 passed / 34 files**，138.57 秒；无需挑单项复测 | `web.json` |
| runtime Node | **85 passed** | 本次 `npm run test:audit` 输出；`metrics.json` |
| 既有真实 Harness + 受控模型链路 | **ok，83 次受控模型请求** | `runtime-evidence.json`；原始目录 `var/agent-integration-mzk07ome` |
| 十二类确定性测试 | **12/12 passed**，包括 API、Harness、独立 worker、下载和解析 | `deterministic.json` |
| 类型与 Agent OpenAPI | 通过；51 paths / 54 schemas | 本次 tsc / contract check 输出 |
| judge v4 自测 | **12/12 passed**，无模型调用 | `judge-selftest.json` |
| 真实模型独立抽测 | **3/3 完整目标成功** | `live-model.json`、`traces-live/`、`artifacts-live/` |
| 新增文件内容缺失探针 | **1 failed**；见下文 N1 | `retained-content.xml` |

实施方 manifest 的 **26 个文件测试前后全部匹配**，见 `source-verification.json` 与 `source-verification-end.json`。实施方最终九次均处于同一阶段 `rework2_live:a1fa2c2057e6`，均为 succeeded、最终回答非空、判定成功；本次没有把其他源码阶段的记录拼入九次成功。

实施方 9/9 与独立抽测 3/3 分别统计。独立抽测继续使用保存配置 `deepseek-flash`、原上限 12 次模型请求 / 40 次工具 / 150 秒，没有临时扩大预算。

| 独立会话 | 任务 | 模型 / 工具 | turn 时间 | 结果 |
|---|---|---:|---:|---|
| t1-1-1af863d8 | 读取附件并交付 PDF/XLSX | 3 / 4 | 37.250 秒 | 完整成功；另交付 Markdown |
| t2-1-a8ddd93a | 课程材料→预检→生成→等待→读卷→导出 | 9 / 10 | 51.906 秒 | 完整成功；单一真实 job 与材料链对应 |
| t3-1-46df9c4d | 缺资料识别与诚实受阻 | 6 / 13 | 32.156 秒 | 完整成功；没有假出卷 |

以上每次人工介入均为 0。主 Agent token 保留在 `metrics.json` 与原始结果；worker 总用量和真实货币费用仍 unknown。9 次和 3 次均是小样本，不能据此宣传生产成功率。

导出物实际核查：六页 PDF 的首末页、真实模型报告两页已渲染查看，中文、跨页表格、来源可读；XLSX 能打开且没有意外公式，样本值与附件一致；复习卷题目/答案/快照与同一材料链一致。可视证据见 `visual/`。未执行真实校园账户、WebVPN 或学在浙大私有数据验证，合成数据成功不代表这些平台均可用。

根目录/连接器全量与迁移未因本轮局部修改重复运行：本轮变更集中在 Agent 网关、文件与其测试，无迁移变更；上轮 594 项及迁移结果继续保留，但不记作本次新执行。

## 上轮返修要求的状态

- **R1 已关闭**：网关缓存读取、写入均使用 `tools.cache_arguments()`。`wait_seconds` 仅对声明的工具排除；原参数留在执行和审计事件中。首次即带等待的独立重放测试已通过。
- **W1 已关闭本次阻塞**：测试先等待真实远端搜索请求，再等待返回项和列表替换。没有 sleep、skip 或调大超时；本次完整 193 项通过。不声称测试今后绝无波动。
- **R2 原始故障已修，完整性仍差一处**：普通 blob 删除失败会恢复索引、真实返回可下载文件；无法恢复的残留被区分。上轮“索引已删除还广告 404 文件”的直接测试通过。但“索引在”仍被错误当成“内容可读”，见 N1。

实施方将部分发布测试改为验证“干净回滚可重试；回滚失败则逐个核对真实下载”，符合之前允许诚实 partial 交付的验收条件，接受该语义；它不允许返回下载 404 的文件。

## N1 · P2：索引存在仍不足以证明 retained 可下载

代码位置：

- `apps/api/app/agent_bridge/files.py:179–181`：索引删除失败，直接返回 retained。
- `apps/api/app/agent_bridge/files.py:244–270`：`discard_state()` 调用 `indexed()`，后者仅执行 `SELECT 1`，不验证内容。
- `apps/api/app/agent_bridge/artifacts.py:391–415`：回滚后的二次核对仍调用同一 SQL 判定，未补上下载路径检查。

**独立测试的明确条件**：第一格式真实渲染并发布后，模拟该文件的内容在补偿前变得不可用；第二格式发布失败，同时注入索引 DELETE 的 OperationalError。测试没有改生产代码或伪造下载响应，删除的是隔离会话中本探针自己的合成文件。

实测结果：

1. 发布失败返回 `ARTIFACT_PUBLISH_FAILED`。
2. 同一 ID 被放入 `delivered`、`retained_file_ids` 与 `rollback.retained`，提示先使用该文件。
3. 正常会话下载入口对这个 ID 返回 **404**。

失败记录：`file_4afc53c17a2eda6c2e5b21f6324681ee`；探针 `test_retained_content.py::test_index_delete_failure_does_not_advertise_missing_content`；完整断言见 `retained-content.xml`。

这是**合成的双重异常组合**，不意味着正常导出或日常出卷不可用，也没有观察到跨账户泄露或任意文件读取。但它仍违反原 R2 的要求：“返回 delivered/retained 前核对其仍能通过正常会话文件入口获取”。属于同一问题的剩余缺口，不是新增业务范围。

**最小返修条件**：

1. retained 的依据应是正常下载入口共用的文件验证，而不只是索引行存在；至少验证本地内容存在、合法路径和链接限制。索引故障、内容缺失或无法核实应明确标为不可用/unknown，不得广告可下载文件。
2. “有索引但无内容”不能反过来伪称已经干净回滚。保留需修复索引或清理的真实状态，拒绝盲目重试。
3. 修复 N1 后，上轮六项独立测试与新增 R2 异常测试须保持通过；可下载 retained 仍须正常下载，不可下载的 ID 不进入 delivered。

复现命令：

```powershell
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py docs/audit/harness-independent-rework2/test_retained_content.py -q -p no:cacheprovider --tb=short
```

当前预期一项失败。修复不需要重构 Harness、不需要增加权限、不需要取消必要隔离。

## 下一步验收与 PR

目前可保留草稿 PR；还不能把本轮写成最终无阻塞通过。工作区依然混有此前的 V2、校园网络、UI、测试证据等未提交内容；上轮要求的 PR 范围整理、脱敏与实际 CI 不因主路径通过自动完成。本次没有创建或更新 PR，也没有再次推断 GitHub CI 状态。

若下一轮只修 N1 的文件状态判定，复验集中在新增探针、文件发布/回滚/下载、上轮重放和必要短链，**不要求再次盲跑完整付费 3×3 或重做整个架构审计**。若工具 schema、模型策略或出卷链路另有改变，再扩展对应评测。

主流程已有足够证据，剩余工作是关闭这个明确、局部且可复现的错误后完成 PR 整理。
