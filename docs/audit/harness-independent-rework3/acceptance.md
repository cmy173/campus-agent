# Harness 收尾验收：通过

日期：2026-10-15。对象：`docs/audit/harness-next-round-rework3-result.md` 中针对 N1 的局部修复。

**结论：本次 Harness 开发任务验收通过；此前已确认的功能阻塞全部关闭，可以进入 PR 整理与提交阶段。** 本结论限定于已约定的 Harness、导出及复习卷能力，不替代整个 V2 累积改动的评审或 GitHub CI。

## 本轮独立证据

| 检查 | 验收方实际执行结果 | 证据 |
|---|---|---|
| 上轮 N1 及之前六项独立边界 | **7 passed**，14.11 秒 | `prior-boundaries.xml` |
| 本轮新增下载验证控制 | **2 passed**，2.87 秒 | `download-controls.xml`、`test_download_verification.py` |
| 文件、网关、反馈、范围、出卷、工具及停止回归 | **164 passed / 2 既有 skipped**，147.14 秒 | `targeted-api.xml` |
| A03 调用重放 / 内容冲突 | 通过 | `controlled-chains.json` |
| A05 渲染错误、缺依赖、超时、发布失败、既有文件保留 | 通过 | 同上 |
| E01 实际 API + Harness：读附件→导出→发布→下载 | 通过；真实 PDF/XLSX 被下载并解析 | 同上；`artifacts/` |
| E02 实际 API + Harness + 独立 worker：预检→受理→等待→读卷→导出 | 通过；三题与同一冻结材料链对应 | 同上 |
| Agent 契约 | current：**51 paths / 54 schemas** | 本次 `export_agent_contract.py --check` 输出 |
| 源码冻结核对 | **28/28 哈希匹配**，测试前后匹配，受控执行期间无变化 | `source-verification.json`、`source-verification-end.json` |
| `git diff --check` | 未报告空白错误 | 本次命令输出 |

两个 skip 来自既有 Windows 符号链接创建权限限制，没有新增跳过。验收方未修改业务代码、实施方测试、历史证据，没有创建 commit、推送或创建 PR。新增探针与结果均保存在本目录。

## N1 为什么可以关闭

原始探针原样复跑通过：“内容已经缺失 + 索引 DELETE 失败”时，不再将该 ID 放入 delivered/retained 供模型下载。

源码已将文件可用性统一到 `ConversationFiles.servable()`：它检查会话归属、索引行、受控路径、非链接/非重解析点及真实内容存在；`discard_state()`、补偿恢复和 HTTP 错误回执使用该判定。缺失内容被保留为待清理状态，不再谎称可下载或安全地完整回滚。

新增独立控制验证了：

- 同一文件在所有者会话可用，在其他会话不可用，内容没有因此受损；删除本探针自己的 blob 后不再判定为 retained。
- 索引无法读取时，状态为 `None` / `unknown`，不会被正常的下载可用性检查判为成功。

这与普通部分交付区分明确：回滚失败但真实文件仍能下载时可以返回 retained；未核实或真实缺失的文件不能作为可用成果。此前的有效文件保留、批次回滚、同调用重试、业务去重、read→export 以及停止相关回归均保持通过。

## 验证边界与历史结果

按上一轮已经确定的复验范围，本轮只修文件状态与异常回执，因此没有盲目重跑全库或付费 3×3。

- 实施方对本轮源码报告完整 API **698 passed / 5 skipped**、十二类场景 **12/12 passed**；这些是实施方证据，**不记作验收方本轮独立全量执行**。
- 验收方本轮独立执行的是上表中的 164 项相关回归、7+2 项边界和四类受控场景；两条完整业务链均在当前源码运行。
- 前一源码阶段的真实模型 9/9 与独立抽测 3/3、前端 193/193、Node 85/85 等仍作为历史回归依据，**不是本轮新模型或前端结果**。本轮未改模型策略、正常出卷链、前端或 Node 文件，未发现需要扩大复验的失败。
- 没有真实校园账户、WebVPN 或学在浙大私有数据的新实测；不得把合成闭环表述为所有校园平台均已真实验证。
- 实际货币费用及 worker 总模型用量仍 unknown，小样本模型结果不等于生产成功率。

## PR 判断

**Harness 范围的功能验收已不再阻塞提交 PR。** 可以整理提交并发起评审，无需继续新增架构改造或为同一局部修复反复运行付费评测。

但“验收通过”不等于当前整个工作区已经可直接合并：

1. 当前 HEAD 仍为 `f5ebbbc`，交付包含大量未提交源码。需要按明确范围形成 commit，使 PR 真正包含这些成果。
2. 排除本地记忆、临时测试树、个人数据、真实环境截图及未经脱敏的运行证据；保留必要合成测试、契约、迁移和验收摘要。不要整体 `git add .`。
3. 明确目标是 V2 集成 PR 还是基于 V2 的 Harness 增量。整个历史分支还包含连接器、网络及 UI 改动，本验收不为它们自动背书。
4. 对整理后的确切提交运行 CI、进行正常代码评审，再决定合并。本轮没有核验新的远端 PR/CI 状态，没有自动发布或合并。

因此：**可以进入 PR 提交；是否 ready-to-merge，由最终提交范围、CI 与评审决定。** 不再以本轮 N1 或之前已关闭的 Harness 缺陷要求继续返修。

## 复现入口

```powershell
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py docs/audit/harness-independent-rework2/test_retained_content.py docs/audit/harness-independent-rework/test_replay_wait.py docs/audit/harness-independent/test_acceptance_boundaries.py docs/audit/harness-independent/test_review_boundaries.py docs/audit/harness-independent-rework3/test_download_verification.py -q -p no:cacheprovider
```

上表分两次执行该组测试，总计 9 passed；不再存在前几轮已经记录的失败断言。原始失败与各阶段报告继续保留，构成诊断、修复和复验的可追溯记录。
