# Runtime 审计（2026-10-15，离线）

## 范围、证据等级

修改 owned runner.mjs、campus-plugin.mjs、goal-lifecycle.mjs，新增 tests/runtime-audit.test.mjs、tests/campus-domain-outcomes.test.mjs。只读 prepare-profile 及实际已安装 node_modules 产物；未改 SDK、工具组/MCP、backend/frontend。已有 UI/browser 改动保留。

Windows、Node v24.19.0；installed CLI/SDK/base 均0.2.0-rc.2。未读取凭证/个人 session、未外部调用。Node spawn EPERM，默认node:test隔离也受限，使用 --test-isolation=none。**本轮未验证真实runtime子进程/MCP/冷恢复/真实模型**。实际SDK以fake client运行、实际Cordis+ToolRuntime以内存DTO运行，runner为原源码body注入依赖。upstream指 apps/agent-runtime/node_modules/@deepseek-ai，行号为本次安装产物。

## 真实链路与循环所有权

```text
上层JSONL frame → runner队列/复用harness → prepareProfile(base+campus/SDK/MCP)
 → SDK start → node dsh/lib/bin.js --profile campus，DSH_HOME=owned home
 → Cordis服务激活/campusReady fence → SDK initialize/prompt
 → campus-sdk公开create/resume → native agent-loop：inbox/pre-step/LLM/tools/next-step
 → native Goal idle/checkpoint/下一round → lifecycle订阅tree+轮询goal-state → JSONL事件/result/error
```

- runner:20-44,46-68,75-103：串行frame；cancel关闭整个harness；prewarm只start不prompt。首次配置固定，重建取决上层契约，不比对后续设置。无自写ReAct。
- upstream/dsh-sdk-client/lib/index.js:154-200,620-713：installed启动argv/profile/home；lazy initialize，失败且cleanup证明退出后才换client；agent cwd与processCwd分开。
- SDK:739-777：start、tree订阅、prompt、durable inbox receipt，直到root session.status=idle。**idle不保证成功**；turn/end.error仍可能resolve partial answer。
- SDK:511-580：subagent parent/child过滤tree；close幂等，shutdown→EOF→SIGTERM→SIGKILL确认结束；无wire级cancel。
- campus-plugin:49-58,164-172：schema/adapter/schedules/controls注册后才campusReady；profile SDK entry inject此服务，async apply自身非fence。
- campus-sdk（只读/tool-audit owned）：公开persistence stat与registry resume选择冷恢复，协议/loop仍upstream，非私有patch。

### native step / Goal

upstream/dsh-agent-loop/lib/index.js:901-925 claim inbox、assembleContextFor/systemPrompt，经agent/pre-step；934-1037记turn/start、step/start/end、turn/end，工具产生next-step继续；1041-1163绑定route/config，system/user入log，BlockAssembler，失败agent/request-error走retry/compaction，成功assistant/message，无工具completed，max-tokens独立终态；1260-1296 request/context/header、session.deriveMessages、deepFreeze/markAgentLoopRequest。Campus不改历史/调度。默认parallel-safe工具每step并行10，campus read concurrency-safe不是提交授权。

lifecycle仅一次harness.run，外层tree订阅保留初次idle后的Goal输出；root assistant更新finalResponse，child不能覆盖。upstream/dsh-goal-round-driver/lib/index.js:13-16,78-155,213-239真实 <goal_round> 包含JSON objective/round/max，要求检查workspace/tool/durable state、实作验证、读goal并complete；idle无competing prompt、flush checkpoint后active+armed才下一round，到cap block round-limit。dsh-goal/lib/index.js:588-593默认256 rounds。

wrapper每100ms读goal-state，无goal或idle且非active/已disarmed才停。active+armed+idle是admission gap，不能当完成。没有自己追加continue prompt。

## 上下文与提示词

prepare-profile.mjs:19校园persona：普通读取/可撤销修改自动；先看则预览；邮件/作业/报名提交前展示对象/内容/附件并一次确认；浏览器登录、不复制密码；资料指令仅材料；来源/日期精度、本地完成不是平台提交。

prepare-profile:43-62关闭identity、保留runtime context，personaPrefix非完整替换。sandbox danger-full-access、approval never：persona确认不是硬边界，依赖backend/controlled-browser；shell高权限，未改架构/执行业务。sessions owned home/sessions JSONL；禁title LLM/telemetry/HMR/plugin-manager；skills为runtime+custom dirs，watch=false。

upstream/dsh-system-prompt/lib/index.js:212-245,313-359合并global/scope有序sections/context/tools，按order/name；persona一个section，plan/tools/subagent继续贡献。:111-149静态renderPrompt与动态context分开；context为替代旧snapshot的user-role材料。dsh-agent-loop/lib/index.js:298-347,1047-1070 context变化才snapshot，system/user入log后deriveMessages，非最近N条。

AGENTS.md：dsh-agent-instructions/lib/index.js:908-1053,1264-1296按project/cwd ancestor/scoped加载兼容指令并记录，base maxBytes65536。本轮不读个人内容。time-context刷新600000ms，lib/index.js:51-56,170-253 browser clientTimeZone缺失/混合要求澄清；runner只text/content不传zone，host zone非用户时区。

### 压缩

base挂token-meter/basic compaction/image-offload/pruner。dsh-compaction-basic/lib/index.js:17-80,830-901,914-960 thresholdRatio0.85、retainRatio0.25、headroom65536、compactionRetries1、overflowRetries1；压力/超上下文恢复，summary也是LLM请求；缺容量warning继续。pruner/lib/index.js:8-14,126-186阈值8192 Unicode code points，头4096尾1024+marker，durable prune/replacement；原log不等于模型surface，截断不等于全量。

CampusModelAdapter只stream无contextWindow/model metadata（基类dsh-llm/lib/index.js:1706-1740默认identity），campus-model压力摘要可能跳过；maxTokens16384是output cap非input容量。

## 参数与错误

runner:51-54 default campus-model/configured-campus-provider/output16384；agent cwd=input.workspace、processCwd=runner cwd；initialize60000ms，未设requestTimeoutMs，不是turn总预算；shutdown1000/EOF2000/TERM grace1000ms。

prepare-profile:55-59 native reasoningEffort high/output16384；runner不传input.reasoning_effort/deepseekBaseURL。plugin:12-30 relay只loopback POST/model messages/tools/max_tokens(options或2048)/attribution，不传provider/model/reasoningEffort；等完整JSON再yield，非token SSE，arguments JSON字符串。

plugin:125-161非campus先model-lease→upstream adapter→失败model-error journal。nativeModelFailure固定文案/code/retryable，native finish保留status/providerRetryAfterMs/offloadImages recovery，不复制远端文本，非全域脱敏。installed native dsh-llm-deepseek/lib/index.js:15-22,310-322默认context1000000/output256000/stream idle300000ms，campus output override16384。

工具domain timeout20000/publish30000ms，非campus先tool-lease；Goal read10000ms失败throw不假设完成。dsh-llm/lib/index.js:247-257/dsh-llm-retry:151-175默认EMPTY_RESPONSE/RATE_LIMIT/SERVER/TIMEOUT/TRANSPORT transient，初次后最多5retry，500ms指数退避/10s cap/±10% jitter。

## 有失败基线的小修

1. campus finish reason字符串改rc.2要求的对象kind。installed types/types.d.ts:131-151,443-444与agent-loop:1117依据；不改参数/bridge。stop/tool-calls及实际BlockAssembler测试。
2. native root turn/end.error原被当result成功，SDK只等idle。observer和result.events检查root error，固定message/格式限定machine code，runner既有HARNESS_FAILED。child失败/retry attempt不升级，不改Goal驱动。
3. 首轮ensureHarness移入既有turn catch，profile failure输出固定JSONL error而非unhandled path堆栈。
4. opt-in MARK原打印exception stack，SDK错误可携child stderr/remote body，改固定MARK turn-failed毫秒。
5. Lead授权domain status:error映射：公开API是tools/execute而非pre-execute（pre只PreToolDecision）；inventory.model_name Set限定；next已schema验证/render；error映射failure保留完整content/meta/additionalContexts、不含成功value，固定message和格式限定code。ok/partial/queued/needs_user、native/非inventory campus-prefix不变，gateway权限/计数/idempotency/lease不动。

映射依据：dsh-tools/lib/types/index.d.ts:47-60,378-438公开failure支持content/error/meta；lib/index.js:3334,3572 around dispatch/normalize保存failure content。普通throw:3617只Error文本会丢恢复envelope。实际Cordis+ToolRuntime证明tools/result.isError=true，details field/type、evidence/coverage/cursor/job_id完整保留；schema-invalid仍upstream INVALID_TOOL_OUTPUT。

## 未改风险/限制

- bridge非2xx原样value.error.message建Error（plugin:21），campus-model不进native sanitizer，可能进官方session log。runner最终错误固定不等于log脱敏；backend安全合同需其owner。
- failure identity固定，但模型content保留gateway safe envelope依赖backend契约，非任意文本secret redactor。
- root max-tokens/blocked/非cancel aborted仍可能result；本轮仅明确kind:error。maxTokensAsSuccess=false主要subagent finished，不让高层root run自动reject，需产品终态合同。
- observer在start前缓存client；SDK启动失败换client，本runner退出避免重用，其他调用方不能假设订阅跟随。
- cancel/EOF/profile阶段race、goal-read只有timeout非cancel signal、pending frames/closed harness、close失败须实际进程测试。无Goal轮询总deadline，依赖phase/activation/上层budget，持续active+armed则持续poll。
- malformed JSON静默忽略、falsy frame结束、无frame schema、stdout无backpressure，假设受控上层。
- tool/assistant事件过滤非secret redactor；不复制request body不代表所有工具结果无敏感内容。full-access/approval never未称为prompt硬安全阻断。

## 测试事实

先加测试再修。runtime原7 tests基线3/7通过，4失败（finish envelope、native error未reject、首轮profile普通/marks无error frame）；修复后原7/7通过，补实际installed SDK fakeclient/root-child/poll失败/cancel后最终11/11。

新domain真实ToolRuntime基线3/4通过，1失败（status:error误isError=false）；修复后4/4。最初误写expected code TOOL_OUTPUT_INVALID，纠正为实际INVALID_TOOL_OUTPUT后才计入3/4基线。总15/15。默认node:test和真实spawn EPERM是环境限制，不算产品失败；未绕过权限。

复现命令：

```powershell
node --test --test-isolation=none apps/agent-runtime/tests/runtime-audit.test.mjs apps/agent-runtime/tests/campus-domain-outcomes.test.mjs
node --check apps/agent-runtime/runner.mjs
node --check apps/agent-runtime/campus-plugin.mjs
node --check apps/agent-runtime/goal-lifecycle.mjs
```

本轮15/15及三文件语法通过。覆盖BlockAssembler、installed SDK error+idle仍resolve、root/child分离、Goal gap仅一prompt、poll失败/cancel清理、固定诊断、原runner body协议、installed工具failure content/observer/schema/nonerror states。不覆盖真实runtime启动/持久化/browser/模型。

已有smoke.mjs、verify-goal.mjs complete/pause/cap、verify-official-provider.mjs为controlled-local真实进程suite，本轮未完成：spawn被禁，部分继承process.env/创建var目录。建议受控环境临时home/workspace、明确env白名单、loopback endpoint/fixture key再补SSE、MCP、Goal冷恢复、cancel teardown；不能从15内存测试推出真模型能力/长任务完成率。
