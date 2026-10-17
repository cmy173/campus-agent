# Harness 返修独立验收与 PR 判断

2026-10-15；验收对象：`docs/audit/harness-next-round-rework-result.md` 所述源码。结论：**主要修复成立，仍需两处产品修正；可以准备草稿 PR，暂不批准合并。**

这是新一轮验收。上一轮报告、首次失败、实施方中间失败与最终复测均保留。本轮验收只新增测试与证据，没有改动业务代码、提交 commit、推送或创建 PR。

## 已验证结果

| 项目 | 独立结果 | 证据 |
|---|---|---|
| 上轮四项边界 | 4 passed | `prior-boundaries.xml` |
| 完整 API | 679 passed / 5 既有 skipped，251.40 秒 | `api.xml` |
| 根目录及 connectors | 594 passed / 87 subtests passed，49.00 秒 | `connectors.xml` |
| runtime Node | 85 passed | `metrics.json`、本次命令输出 |
| 既有完整 Harness 执行链 | ok；83 次受控模型请求 | `runtime-evidence.json`；原始目录 `var/agent-integration-21j11fnk` |
| 原生错误恢复 | 限流有限重试后消费读取结果；认证错误不重试 | 本次 `verify-transition-recovery.mjs` 输出 |
| 12 类确定性场景 | **12/12 passed**，包括真实 API、Harness、worker、下载与解析 | `deterministic.json` |
| judge v4 自测 | 12/12 passed；零付费模型调用 | `judge-selftest.json` |
| 真实模型独立抽测 | **3/3 完整交付成功**，每类一个新会话 | `live-model.json`、`traces-live/` |
| 前端构建、TypeScript | 通过 | 本次 `npm run build` / `tsc -b` 输出 |
| Agent OpenAPI | 通过，51 paths / 54 schemas | 本次 `export_agent_contract.py --check` 输出 |
| 前端完整测试 | **192 passed / 1 failed**，34 文件，135 秒 | 下文 W1；失败项单独复跑通过 |
| 验收新增边界 | **2 failed，均为已复现产品缺陷** | `new-boundaries.xml`、`test_replay_wait.py` |

实施方返修 manifest 的 **24 个文件全部匹配**，测试前后均一致。所有最终九次会话标记为同一阶段 `rework_final:f03e496dae97`，turn 均 succeeded、最终回答非空；三次出卷均只有一个真实 review job，没有把中间失败记录拼入成功集。记录见 `source-verification.json`、`source-verification-end.json` 及实施方 `rework-final-live-model.json`。

独立三个会话也使用原保存模型配置 `deepseek-flash`，预算保持 12 模型请求 / 40 工具 / 150 秒：

| 会话 | 完整目标 | 模型 / 工具 | turn 用时 | 人工介入 |
|---|---|---:|---:|---:|
| t1-1-2217ae89 | 读附件并交付真实 PDF/XLSX | 3 / 3 | 32.266 秒 | 0 |
| t2-1-0389b5bc | 发现课程、读取材料、预检、生成、等待、读卷并交付 Markdown | 9 / 9 | 46.625 秒 | 0 |
| t3-1-fbf5acde | 识别资料正文缺失，不编造、不出假卷 | 5 / 9 | 26.062 秒 | 0 |

实施方最终集 9/9、本次独立集 3/3，**均满足各自的目标核验**，分别统计，不合称一次 12/12 正式试验，也不称为生产成功率。主 Agent 用量保存在原始 JSON 与 `metrics.json`；worker 总 token 和真实货币费用仍 unknown。

产物已实际下载检查：跨页 PDF 的首末页、真实模型 PDF 两页均渲染查看，中文可读、表格与来源正常；XLSX 能打开、数值与来源一致、没有意外公式；复习卷三题及出处与二分查找冻结材料对应。可视证据见 `visual/`。真实学校账号、WebVPN、学在浙大私有数据本轮没有实测，不能由合成流程推出已接入成功。

## 上轮问题的状态

- F1：从不可终止线程改为固定渲染子进程；真实进程终止/回收与无迟到写入测试通过；渲染 deadline 调整到域工具围栏内。主要故障已修。
- F2：相同业务请求可复用在途/完成 job，轮询支持有界等待；最终九次与独立抽测均完成交付。重复业务受理与轮数耗尽的原始故障路径已有修复与证据。
- F3：普通第二格式失败的整批回滚、同 ID 临时失败恢复已通过；**回滚自身发生异常的状态仍不真实，见 R2**。
- F4：read 的 paper_id 已正确映射，独立真实 worker 的 read→export 边界通过。
- F5：judge v4 验证 turn、最终答复及同一业务链，旧失败不再因存在文件而误判成功。证据阶段清晰。

实施方改写了上轮 F1 测试的注入方式。经阅读和复跑，这是执行单元由线程变成子进程所需的适配：真实启动并通过 OS 检查回收，未删除场景或削弱核心断言，予以接受。

## R1 · P2：显式等待参数使完全相同调用不能重放

代码：`apps/api/app/routers/agent.py:545–550,582`；`apps/api/app/agent_bridge/store.py:369–375,401–415`。

网关查缓存时从参数删掉 `wait_seconds` 得到 `replay_key`，但保存缓存时仍传入原始 `request`。**首次请求就带 wait_seconds 时**，写入与读取的 hash 不同。第二次发送完全相同的调用 ID 和原参数，实际返回 409 `IDEMPOTENCY_CONFLICT`；不是请求内容真的改变。

现有重放测试首次没有 wait_seconds、只在第二次添加，刚好避开该路径。工具描述又推荐首次显式等待，因此不能视作无关输入。`jobs.read` 等带此参数的工具也经过同一网关。

独立复现：`test_first_call_with_explicit_wait_replays_under_same_id`，首次 200，第二次 409。详见 `new-boundaries.xml`。

修复条件：读写使用同一套规范化后的业务指纹，且只对真正属于观测参数的工具应用该规则；原始参数可独立用于日志，不能混作缓存身份。补测首次显式带等待、相同等待、不同等待、省略等待及真实业务参数变化；后三者中真正改变业务内容仍须冲突。不能靠删掉首次等待或改变调用 ID 掩盖问题。

## R2 · P2：已删除索引的文件被报告为“仍可交付”

代码：`apps/api/app/agent_bridge/files.py:147–167`；`apps/api/app/agent_bridge/artifacts.py:348–361,426–445`。

`discard()` 先提交文件索引 DELETE，再删除物理文件；物理删除若因 PermissionError 等失败，函数抛错。调用方将“函数抛错”当作“文件仍可交付”，把已失去索引的 ID 放入 `delivered` / `retained_file_ids`，提示用户先使用这些文件。

独立测试让第一格式真实落库、第二格式发布失败，并仅在第一文件物理删除时注入 PermissionError。实际日志明确为“文件索引已移除但内容删除失败”；API 却返回 retained file，下载该 ID 得到 **404**。残留 blob 不等于本会话可下载成果。

独立复现：`test_failed_blob_cleanup_does_not_advertise_deleted_file_receipt`，详见 `new-boundaries.xml`。

修复条件：区分“下载索引仍有效”和“仅残留待清理物理文件”；返回 delivered/retained 前核对其仍能通过正常会话文件入口获取。索引已删除时不向模型返回可用文件回执，并记录有界清理状态；不要把旧文件误删或把清理异常伪装成成功。增加索引删除前失败、索引已删而 blob 删除失败、多个文件部分回滚三类断言。

这不是伪造了正常 PDF/XLSX；问题在异常交付状态错误，不能按平均成功率抵消。

## W1 · PR 的前端测试稳定性门槛

全量执行：`npm test -- --maxWorkers=2`，192 passed / 1 failed。失败项为 `AgentPage.test.tsx:88` 的历史分页和远端搜索测试，断言位置第 107 行：预期请求 `/agent/conversations?search=数学`，当时 mock 尚未收到该请求。只运行同一项则 1 passed / 26 因名称过滤未执行。

代码支持时序竞争的解释：`useConversationController.ts:35` 对远端搜索有 200ms debounce；测试第 106 行等待本地列表移除旧项，第 107 行立刻断言远端请求。前者不保证后者已经发生。**不能把单独复跑通过改写成完整套件通过，也不能据此断言产品搜索功能必坏。**

应让测试等待其真正要验证的远端请求及返回结果，再重跑相关套件。不得简单加固定长 sleep、skip，或只提高超时掩盖依赖关系。该 UI 改动属于当前累积工作区，不将其根因强行归到 Harness 返修。

## 是否足够提交 PR

**成果足够进入评审，适合草稿 PR；当前不足以标为可直接合并。** 不必等整个校园 Agent 所有设想都实现才提 PR，但应关闭本次已复现缺陷，并形成清晰提交范围。

1. 先修 R1/R2，使新增两个边界测试通过；修正 W1 的等待条件并获得相关完整前端回归通过。已有 9/9、3/3 是有效正面证据，但不会覆盖这些可复现错误。
2. 明确 PR 基线与范围。当前分支 `codex/personal-assistant-v2` 的本地 HEAD 仍为 `f5ebbbc`；本轮源码多数未提交。相对本地缓存 `origin/main` 已累积 48 commits、703 个文件、约 13 万新增行；另有 83 个 tracked 文件修改以及大量 untracked 文件。**这不是一个已经整理好的“小型 Harness 修复 PR”。**
3. 若要提交完整 V2 集成 PR，应如实说明整个 V2 范围，并分别评审连接器、执行层和 UI；本轮只批准/拒绝 Harness 范围，不能为整个历史分支背书。若只提 Harness 增量，应基于明确的 V2 基线整理依赖完整的提交，避免夹带独立网络/WebVPN、账户设置或 UI 改版。
4. 按白名单收录业务源码、契约、迁移、必要测试、摘要和脱敏的合成复现材料。不要 `git add .`：工作区有 `campus-api-tests-*` 临时文件、`.workbuddy-ai` 本地记忆、`outputs/round3-real` 等真实环境截图和大量运行记录。它们不能因“验收资料”名义自动公开。保留本地原件，不在验收阶段删除。
5. 当前尚未形成这些新修改的 commit，因此只从现有远端分支开 PR 不会包含它们。整理提交后再跑相应 CI，再从 draft 转 ready。当前 GitHub CLI 未认证，未能核对远端是否已有 PR 或其 CI；不声称“GitHub CI 已绿”，也没有创建或更新 PR。

`git diff --check` 未报告空白错误；构建和主要后端测试已绿。更多证据见 `pr-scope.json`、`worktree-paths.json`、`metrics.json`。

## 复现命令

```powershell
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py docs/audit/harness-independent-rework/test_replay_wait.py -q -p no:cacheprovider --tb=short
# 当前预期两项失败；修复后应两项通过。

# 保留已修故障的回归
.\apps\api\.venv\Scripts\python.exe -X utf8 tools/audit_regression.py docs/audit/harness-independent/test_acceptance_boundaries.py docs/audit/harness-independent/test_review_boundaries.py -q -p no:cacheprovider

# 前端原始运行与定向复测（在 apps/web 下）
npm test -- --maxWorkers=2
npm test -- src/features/agent/AgentPage.test.tsx -t "loads all cursor pages" --maxWorkers=1
```

R1/R2 属于局部工程修正，无需再扩大架构。修复后复跑新增边界、受影响工具/文件/重放/停止测试、必要的真实链路及前端相关回归；保留本次失败证据，不覆盖为成功。若修改改变工具 schema、模型策略或出卷链，则另建同版本完整真实模型证据集，不拼接旧轮次。
