# Harness 独立验收：需修改

验收日期：2026-10-15。验收对象为 `harness-next-round-result.md` 对 M0–M3 的交付；依据为预先确定的 `../harness-next-round-acceptance.md`。**基础工程可运行，但整体未达到验收门槛。**

本轮只增加独立验收测试、报告和证据，没有修改业务代码，也没有替实施方修复后再给自己验收。进入时工作区已有大量未提交修改，全部保留；不能将整个 `git diff HEAD` 视为本轮实施方新增。

## 1. 已经独立验证的成果

| 项目 | 独立运行结果 | 证据 |
|---|---|---|
| 完整 API 回归 | 660 passed / 5 skipped，205.39 秒 | `api.xml` |
| 根目录及 connectors 回归 | 594 passed / 87 subtests passed，47.66 秒 | `connectors.xml` |
| runtime Node 测试 | 85 passed / 0 failed | `verification-summary.json`，命令见下文 |
| 既有真实 Harness + 受控模型执行链 | ok，83 次受控模型请求 | `runtime-evidence.json`；原始目录 `var/agent-integration-3k322fch` |
| 原生错误恢复 | 限流重试后读取并消费结果；认证错误仅请求一次 | `verification-summary.json` |
| TypeScript / Agent OpenAPI | 通过；51 paths / 54 schemas | `verification-summary.json` |
| 数据迁移 | 全新隔离 SQLite：upgrade head、单 head、check、downgrade 前一版本、再 upgrade 均通过 | `migration.json` |
| 实施方十二类场景独立复跑 | 首次 11/12；A05 因验证脚本的 Windows 文本解码异常未完成；同源码使用 `-X utf8` 单独复跑 A05 通过 | `deterministic.json`、`deterministic-a05-utf8-retest.json` |
| 验收方增加的边界测试 | **4 项失败，证实下文产品缺陷** | `boundaries.xml`、`review-boundaries.xml` |
| 真实模型独立抽测 | **2/3 完整目标成功**，每类一次，固定原预算 | `live-model.json`、`traces-live/` |

不能将“十二类既有用例经环境修正均能运行”表述为全部行为通过；追加的边界输入揭示了原用例未覆盖的问题。首次运行和复测原件均保留，没有覆盖或修改实施方证据。

导出能力是真实实现：下载的 PDF 是有效 PDF；六页跨页表格完整，第一页、第三页、末页已渲染查看，中文可读、表头重复、页边正常。真实模型生成的两页实验报告也逐页查看。XLSX 经 openpyxl 打开，数值、日期、布尔、空值及 `=` 开头的文本类型正确；真实模型导出的五条抗拉强度与附件一致，没有公式单元格。复习卷 Markdown 确实包含三题、答案、解析和冻结材料指纹，已人工核对其二分查找内容。

## 2. 阻塞发现与返修条件

### F1 · P1：渲染超时没有终止渲染，且上下层超时预算冲突

位置：`apps/api/app/agent_bridge/artifacts.py:285–289`；`apps/agent-runtime/campus-plugin.mjs:127`。

`Thread.join(timeout)` 到期后抛出“已中止且未交付”，但没有停止线程。独立测试用事件控制渲染器：收到 `ARTIFACT_TIMEOUT` 后才放行线程，线程仍成功写出 `late.txt`。产品随后删除 staging，无法保证后台不会继续占用资源、写文件或与清理竞争。现有测试只断言输出列表为空，未证明中止。

另一个已读出的配置问题：域工具围栏是 20 秒，渲染服务上限是 30 秒。慢渲染可能先被 Harness 判定超时，后端继续执行；“服务端自己的 30 秒 timeout”不足以约束模型看到的执行生命周期。

返修：固定渲染器使用可终止且能够回收的受控执行单元；成功、超时、取消均完成收尾后再报告终态。上下层 deadline 一致或显式传递剩余预算。不得以开放任意 Shell 解决。测试必须检查超时后无线程/子进程继续写入、无迟到发布、无重复重试造成资源累积。

复现：`test_acceptance_boundaries.py::test_timeout_stops_renderer_before_reporting_abort`，失败断言为“API 报告 aborted 后仍发生文件 IO”。

### F2 · P1：真实模型等待出卷时重复提交付费业务，耗尽预算后没有交付答复

位置：`apps/api/app/agent_bridge/review_actions.py:236–260`、`apps/api/app/agent_bridge/tools.py` 的 `reviews.generate` / `jobs.read` 分派，以及当前 Agent 编排提示。

独立真实模型会话 `t2-1-ff0459e9` 的原始顺序：

1. 工具第 8 次调用 `reviews.generate(wait_seconds=14)`，受理 `job_556e41c3`。
2. 第 9–11 次调用读取该 job，均为 running。
3. 第 12 次工具调用再次 `reviews.generate`，参数和材料指纹相同、call ID 不同，受理第二个 job `job_eff9e725`。
4. 第 13 次读取第一个 job 已 succeeded；随后模型读卷、取消第二个 job、导出第一份卷。
5. 共 12 次模型请求、17 次工具调用；turn 最终 failed / BUDGET_EXCEEDED，**最终答复为空**。第二个 job 已经执行到 validation 才取消。

这是实际的额外付费业务执行，不能归类为联网权限不足，也不能以“文件区里已有 Markdown”算完整任务成功。同 call ID 重放保护有效，但不足以处理模型以新 call ID 重复提交同一在途操作。当前工具的有界等待虽有改善，尚未稳定解决等待和任务身份管理。

返修：让在途任务的稳定业务身份与轮询、恢复衔接；同一用户请求中相同课程/冻结材料/约束的在途出卷不得因新的模型 call ID 再次计费。可复用原 job 或明确拒绝重复并返回原 job；用户明确要求另出一份时另建业务操作。提供节省模型轮数的有界观测/续执行方式，预留最终交付预算。不要无限提高调用上限，也不要仅增加“不要重复”的提示词后宣称已解决。

证据：`live-model.json` 对应 run 的 `tool_trace`、`domain_db.review_jobs`、`turn`；完整轨迹 `traces-live/t2-1-ff0459e9.json`。这是独立抽测中的一次失败，不据此声称所有出卷均失败。

### F3 · P2：多格式发布中断的状态不真实，建议的重试路径实际上不可用

位置：`apps/api/app/agent_bridge/artifacts.py:342–365`；`apps/api/app/routers/agent.py:504–506,536`。

逐个 `files.save` 后才保存整体回执。独立注入第二格式发布失败，第一份 Markdown 已实际可下载，但错误仅称“未交付，可重试同一调用”，没有返回部分交付回执，也没有回滚本次已发布文件。另一个独立测试只让第一次 save 临时失败，随后恢复正常；重复相同 call ID，网关直接返回缓存错误，save 尝试次数仍为 1。

返修：明确批次发布状态：原子交付，或返回真实的 partial 文件回执并持久记录进度。不能误删此前成功文件。定义兼容网关缓存的安全重试/恢复入口，保留请求指纹、防止换内容重试或重复文件；错误的 retryable 字段与文案一致。不要简单取消全部写操作的重放缓存。

复现：`test_second_format_publish_failure_has_no_hidden_partial_delivery`、`test_publish_transient_failure_same_call_can_recover`，均在 `test_acceptance_boundaries.py`。

### F4 · P2：读卷适配器丢失试卷主键

位置：`apps/api/app/agent_bridge/review_actions.py:279`；原业务契约 `apps/api/app/models.py:805`。

`to_paper_contract` 返回键 `id`，新适配器却取 `payload.get('paper_id')`，因此 `reviews.read.data.paper.paper_id` 恒为 null。现有测试验证题目和 revision，没有验证主键；独立真实模型轨迹和新增真实 worker 测试同时复现。模型若根据读卷结果构造后续导出参数，会遇到断链。

返修：显式映射原契约 id，统一 read/generate/export 身份字段，并测试读卷返回的 paper_id 可直接用于导出，不依赖模型从之前消息猜回 ID。

复现：`test_review_boundaries.py::test_read_preserves_completed_paper_identity`，实际 `None != pap_a17b4609`。

### F5 · P2：真实模型验收评分漏掉最终交付，3/3 复测中包含失败回合

位置：`tools/verify_next_round_live.py:783–874` 的 `judge_t2`，以及 `is_goal` / 汇总逻辑。

T2 评分只检查 job、读卷、文件和快照，没有要求回合成功及最终回答引用真实交付物。实施方最新 `live-model-t2-final3.json` 中 `t2-2-00bc3c8c` 被记为 achieved=True，但 turn 为 failed、BUDGET_EXCEEDED、final_answer 长度为 0。它产生了可用文件，说明业务执行有进展，**不能证明完整 Agent 交付成功**。

因此最新 T2 复测按完整交付口径为 2/3，而非报告中的 3/3。首轮正式九次仍为 6/9；不同代码阶段的 T1/T3 与后来的 T2 复测不得拼成当前版本 9/9。实施方保留失败记录的做法正确，不将此判为隐瞒。

返修：保留旧 verdict，版本化修正 judge。核对最终回答、真实可下载文件、具体 paper/revision/snapshot/job 的对应关系，不能各取“最后一次 generate”和“第一张 paper”拼接。记录额外生成和取消，不把仅脚本状态 completed 等同 turn succeeded。增加失败回合即使有文件也不能判完整成功的反例。

证据：`evidence-reconciliation.json`；原件位于 `../harness-next-round/live-model-t2-final3.json`。

## 3. 真实模型样本：严格分开统计

原模型为保存的 DeepSeek 配置 `prv_c68d2878` / `deepseek-flash`，由官方端点调用；校园材料和数据库是合成的。主 Agent 与复习卷 worker 均使用真实模型。每次保持 12 模型请求 / 40 工具 / 150 秒，没有因失败放宽。

| 证据集 | 完整交付结果 | 可得出的结论 |
|---|---|---|
| 实施方最初 9 次 | T1 3/3、T2 0/3、T3 3/3，共 6/9 | 未达合同要求 ≥8/9 且每类 ≥2/3 |
| 实施方最新 T2 单类复测 | 完整交付 2/3；其脚本报告 3/3 | 有改善；无法替代当前版本完整九次 |
| 本次独立抽测 | T1 1/1、T2 0/1、T3 1/1，共 2/3 | 当前版本仍能复现出卷编排失败 |

| 独立会话 | 模型 / 工具调用 | turn 用时 | 人工介入 | 完整目标 |
|---|---:|---:|---:|---|
| t1-1-3cb5b3e1 | 3 / 3 | 29.625 秒 | 0 | 成功；实际读取附件并交付 PDF、XLSX |
| t2-1-ff0459e9 | 12 / 17 | 56.079 秒 | 0 | 失败；文件存在，重复出卷且无最终答复 |
| t3-1-9f9ffac1 | 5 / 9 | 27.500 秒 | 0 | 成功；确认无可用正文，不生成假卷 |

上述用时来自每个 turn 的实测；启动、清理及 turn 后的 worker 终态核验不当作模型执行时间。主 Agent token 用量保留在 `live-model.json` 与 `verification-summary.json`。worker 用量及实际货币费用仍为 unknown，不能把主 Agent usage 当全部成本。三个独立样本不构成生产成功率估计。

本轮没有真实学校账号、WebVPN 或学在浙大私有材料验证，未将合成流程通过写成真实校园平台接入成功。公开网页与认证恢复的既有回归通过，也不代表全部校园网络环境已验证。

## 4. 源码与证据可信边界

HEAD：`f5ebbbcc4ce7d163e44ed583b96cce06ff49bd9b`，验收的是其上工作区源码，不是假设 HEAD 已包含交付。

实施方 manifest 的 22 个文件中，21 个哈希匹配，`tools/verify_next_round_e2e.py` 不匹配。当前 SHA256 为 `eec4299259d468eb826268b61a8d3337496d249935428c5724fd6b53863240de`。所有列出的产品源码匹配；不因证据脚本后续变化直接否定产品，但交付方应刷新 manifest 并说明版本，不能让旧哈希替当前验证背书。详见 `source-verification.json`。

独立首次十二类运行中 A05 在 `_fault_child` 对 stdout 切片处出现 NoneType；父进程改为 UTF-8 后同源码单类复跑通过。归类为 Windows 文本解码/验证驱动问题，**不冒充产品 A05 故障**。真正的产品缺陷由另外四个边界测试和真实模型轨迹证实。

## 5. 可直接执行的返修与复验

先修 F1–F4 产品行为，同时修 F5 评分口径。沿用现有业务服务、会话 scope、认证、SSRF 和 TLS 边界，不重写校园连接器，不新增通用代码执行。当前分层总体可保留；问题集中在执行生命周期、业务操作身份、工具数据映射和评测完整性，不支持“大规模替换 Harness 才能解决”的结论。

最小独立复现命令（仓库根目录，PowerShell）：

```powershell
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py docs/audit/harness-independent/test_acceptance_boundaries.py docs/audit/harness-independent/test_review_boundaries.py -q -p no:cacheprovider --tb=short
```

当前该命令预期失败四项。修复后应全部通过，且不能删除、skip 或削弱断言。F3 可以采用诚实 partial 交付方案；若采用，必须补充有依据的状态/回执断言并由验收方审查，而非为凑绿测试丢弃边界。

常规回归命令：

```powershell
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py apps/api/tests -q -p no:cacheprovider
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py tests connectors/tests -q -p no:cacheprovider
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/verify_agent_runtime.py
node apps/agent-runtime/verify-transition-recovery.mjs
node apps/web/node_modules/typescript/bin/tsc -b apps/web
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/export_agent_contract.py --check
# runtime 测试从其目录运行：npm run test:audit
```

完成针对性修复后，在**同一最终源码哈希、固定模型配置与原预算**下另建证据集，重跑完整 3×3 真实模型任务；保留本次和之前所有首轮记录。至少 8/9 完整目标且每类 ≥2/3，既有十二类与新增边界均通过，才重新申请整体验收。禁止用反复单类挑选成功记录拼凑九次，也不能让预算耗尽后的后台产物替代最终交付。

返修交付必须列：准确改动文件、故障对应测试、当前源码哈希、原始轨迹及可下载产物、模型与 worker 用量边界、剩余限制。当前不批准宣称“Agent 完整任务能力已验收通过”。
