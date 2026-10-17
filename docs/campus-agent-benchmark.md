# Campus Agent Benchmark

This benchmark uses the application's normal local Agent API (`/api/v1/agent/conversations` → `turns` → `turns/{id}`), keeps a separate conversation for every run, polls the API event journal, and writes one redacted JSON trace per scenario run. `tools/campus_bench/scenarios.json` has 44 scenarios, retaining the original 33 and adding AUTH-01, BUS-09, BUS-10, BUS-11, JOB-01, ERR-09, ERR-10, ERR-11, BUS-12, and TOOL-REV-01. At present 33 scenarios are runnable through the real-model path; BUS-03, BUS-09, ERR-02, ERR-09, ERR-10, ERR-11, BUS-12, JOB-01, and TOOL-REV-01 have isolated scripted-model fixtures; 4 scenarios remain blocked on safe/reproducible preconditions. Mock results are never mixed into real-model task success rates. Each entry records its intent, setup, goal, observable conditions, source/tool constraints, interventions, execution mode, capability state, and known gaps.

## Support boundary

The catalog describes implemented and missing capabilities separately. Each scenario includes an initial state and preconditions. `run_status=blocked_precondition` means the required fault fixture or safe target is absent; default batches omit these scenarios, while an explicit request fails before contacting the Agent API. Currently expired-session injection, a controlled WebVPN target/fault switch, page DOM-change fixtures, and same-task human-login resume are blocked. Real-time empty classrooms/seats and reminders are marked missing. No scenario is counted as a verified live result just because the Agent says it succeeded. Dynamic facts and UX judgments need human review.

Campus state has three distinct parts: saved Passport/WebVPN credentials (masked `configured` and `auto_relogin` flags), persisted consent to reuse the assistant browser (`browser_session_authorized`), and a currently live campus connector/session (`connected`). A stored password is never printed or added to traces; it also does not prove a current authenticated session. Do not infer live access from connector registry status alone.

Every real run now records `tool_availability_checks` against each model-call schema snapshot. `available_first_call`, `loaded_later`, `unavailable`, and `not_observed` distinguish first-call exposure, dynamic loading, proven absence from complete snapshots, and insufficient evidence. A visible but unselected tool is a selection issue; a truncated or legacy snapshot cannot prove absence. Batch summary and the HTML report surface missing required tools. Evidence review and the current 88-run retrospective are in [tool availability audit](campus-agent-tool-availability-check-2026-10-17.md).

The four layers are kept distinct in results:

1. Tool foundation: `layers/direct-tools.json` has 20 in-memory synthetic tool/transport cases. `layers/campus-auth-contract.summary.json` runs 76 isolated API tests for persistent browser grants, expired-session recovery, credential separation, and WebVPN adapter behavior. These tests use synthetic credentials and patched dependencies; no real browser or campus network is contacted, and they do not prove natural-language Agent recovery.
2. `layers/harness-dispatch.summary.json`: an offline Harness bridge contract test that mocks provider responses while exercising the real FastAPI model/tool endpoints, argument parsing, domain executor, journal events and result feed-forward.
3. `layers/mock-agent-e2e.json`: an isolated scripted-model E2E using the ordinary public Agent conversation/turn APIs, real Node Harness subprocess and registered local executors. The default `course-list-smoke` fixture tests `courses.list`; `--scenario BUS-03` seeds a synthetic syllabus and drives `materials.search` → exact resource/version/chunk `materials.read`; `--scenario JOB-01` seeds a running job plus session-ownership receipt, then transitions it to succeeded while real `jobs.read` performs its bounded wait. Fixtures verify tool selection, argument dependency, execution, call-ID result feed-forward and final response. They are explicitly **mock model / real local Harness and tools / synthetic data / no campus services contacted**; dispatch-path tests, not reasoning-quality or campus-connectivity scores. Temp state and subprocesses are removed after each run.
4. `layers.agent_task`: deterministic, trace-aware heuristics plus explicit unverifiable/partial outcomes. No self-judging model is used.
5. `layers.user_experience`: confirmation/clarification observations with human review required.

Before a real `run` batch creates scenario rows or contacts the model, it performs local `/api/v1/health`, source-fingerprint, and selected-provider preflights. The provider check confirms that the requested configuration exists, is enabled, and has a key configured; the key itself is never read or persisted in the benchmark. The health response includes a startup-time fingerprint over the API Agent, campus connector, and learning executor Python sources. Preflight compares it with the current checkout and records `matched`, `mismatch`, or `not_reported`; a healthy HTTP response alone does not prove the process loaded this checkout. A normal run now **blocks** on `mismatch`, `not_reported`, or invalid fingerprints before provider lookup, batch creation, or model calls. For an intentional diagnostic run against an older/unfingerprinted service, pass `--allow-unmatched-api-source`; the manifest marks that run as overridden and the result must not be presented as current-checkout behavior. Comparisons require a loaded fingerprint that matches the expected checkout. Successful preflight evidence is recorded in `manifest.json` (or the resume history). `probe` remains available for read-only environment diagnosis and does not require a matching source fingerprint.

Run the JOB-01 isolated scenario with `python tools/campus_bench/runner.py mock-e2e --batch-id <unique-id> --scenario JOB-01`. It needs the API virtual environment but no real model provider, campus credentials, or network access to campus systems. The command defaults to three repetitions, each with a fresh temporary API/database. `--repeat` accepts 1..10. The ordinary `run --scenario JOB-01` path rejects it because JOB-01 is not a real-model scenario.

Run the BUS-03 isolated materials chain with the same command using `--scenario BUS-03`. BUS-03 still supports its normal `real` mode; this fixture only validates the synthetic Harness/tool path.

After adding API source fingerprints, the initial `BUS03-SOURCE-FINGERPRINT-SMOKE-20261017` run verified the Harness path but did not assert the fingerprint. A corrected run `BUS03-API-FINGERPRINT-ASSERT-N1-20261017` passed 1/1: the disposable API's `/health` fingerprint exactly matched the current checkout before the Agent turn was submitted; three scripted model/provider exchanges executed `materials.search` → `materials.read`, result feedback continued, and the temporary process/state were cleaned up. Elapsed time was 13.54 seconds including isolated API setup. This remains a scripted-model/synthetic-data Harness check, not a real-model/campus result; the separate long-lived API still reports its fingerprint as unavailable.

Run the MULTI-02 isolated learning-plan preview with `--scenario MULTI-02`. The scripted model exercises the real local `materials.search → materials.read → assignments.list → availability.read → plans.preview` executors through one public Agent conversation. It verifies each prior result is returned before the next dependent call, the plan is a pending candidate, and `plans.apply` is never called. Its SQLite inputs are synthetic; this is Harness/executor evidence, not proof that a real model will plan correctly or campus data is available. The fixture uses at most six model calls and six tool calls, a 120-second per-turn limit, and no campus network access.

Run ERR-02 with `--scenario ERR-02` to test an empty materials cache. It requires one exact `materials.search` call, confirms the empty result and the tool's warning reach the final model turn, and checks that the answer distinguishes “no match in local cache” from “no course material exists”. The fixture rejects a campus sync call and never contacts campus services. This is a scripted-model harness check, not real-model behavior evidence.

Run ERR-09 with `--scenario ERR-09` to test tool parameter error recovery. The fixture sends an invalid material version first, verifies the real Harness returns `INVALID_INPUT` to the model, then checks a corrected read succeeds. It is mock-only, uses synthetic content, and does not contact campus services.

Run ERR-09 with `--scenario ERR-09` to test parameter-error recovery. On the first `materials.read`, the fixture injects `expected_version=0`; the real Harness must return `INVALID_INPUT` to the model. The scripted model then retries once with the seeded version 2, reads the source text, and finishes. This scenario is mock-only because the invalid parameter is deliberately injected; all records are synthetic and no campus service is contacted.

The HTML scenario detail panel shows the user turns, goal, success conditions, required tools, allowed alternatives, intervention requirement, capability state, final evaluation, trace, and diagnosis together so a reviewer can compare the rubric with the observed execution.

Per-run `tool_quality` now reports observed call names, tool errors, call/result correlation gaps, and accepted tool aliases. `efficiency` carries measured latency, call counts, observed tokens, and an explicit `threshold_status=not_calibrated`; `autonomy` reports observed confirmation events and leaves manual login/unnecessary clarification counts `null` when the event API cannot establish them. These are evidence fields, not subjective scores, and a missing signal is never reported as zero.

`model_call` and `tool_started`/`tool_result` events include durations, tool lists/selection, tool arguments/results, and available usage metadata. Native-provider model events retain a 64,000-character bounded snapshot and SHA-256 of the sanitized schemas visible on that call, so an absent tool can be distinguished from a selection error. The runner deduplicates mirrored SDK and backend tool-call events. Duplicate assistant prose/reasoning is omitted. Arguments to account-derived campus tools are replaced with field names and a digest; their results and dependent final answers are replaced with status/count/hash summaries before export. Public retrieval results remain available for review. Secret keys, bearer/API/session tokens, and URL query values are also redacted. Scenario prompts are fixed benchmark catalog text. Review every trace before sharing it because other retrieved public content can still contain personal information.

The runner keeps the privacy boundary at the result level: private tool arguments and results are always redacted from persisted traces. A final answer is also redacted if a private result may have reached the model, or if the result is missing/ambiguous. A private call that is explicitly an error with no data payload does not by itself erase an unrelated public answer; the error result remains redacted. `campus.status` stays classified as account-derived because its envelope may contain a local cache snapshot even though credentials and browser-grant details are removed by the API.

The BAS-04 target-page rubric can award `成功` only when the requested URL is called, a correlated non-error fetch result is observed, and the answer has minimum lexical overlap with that observed page text. This proves basic extraction alignment, not semantic relevance or completeness. Other open-ended rubric types remain partial or unverifiable until a task-specific oracle can verify their actual outcomes. The `--seed` value is recorded and controls runner-side run labels only; it does not seed the provider unless the provider API exposes a supported sampling-seed setting. Rebuilding a report reapplies the latest redaction to saved traces and rewrites the local run JSON files.

BAS-02 has a separate deterministic partial-success oracle: it checks that the structured `campus.status` trace contains the registered `zdbk` connector, the answer names the observed portal, and a disconnected campus status is disclosed. It does not verify every extra portal, citation, or claim, so it cannot produce full success. URL query redaction preserves valid JSON in structured results. The summary separates recorded confirmation events from total human interventions, which remain `null` because external logins and untracked UI actions are not observable. Batch comparison reports both mean and P95 latency; opposing directions are labeled as mixed instead of calling the whole scenario faster or slower.

Timing fields distinguish session setup before the first turn submission, submission-to-completion, and total end-to-end time. `first_assistant_message_ms` is derived from the event journal when available; first-token latency remains `null` because the current API does not expose a token timestamp.

For native provider calls, `model_call` records the actual visible schema snapshot, selected tool names, and `llm/stream` wall time (including lease/SDK stream overhead; this is request wall time, not model compute alone). Native web-tool duration remains a per-step span estimate and is not presented as precise per-call latency. BAS-03 citation evaluation checks that cited URLs and labels match observed page/search results and the official domain allowlist. It intentionally leaves factual summaries partial for human review; unmatched citations become unverifiable rather than being counted as a task failure or confirmed success.

The sync tool has an optional `kinds` scope: `assignments` and `resources`; omission preserves full sync behavior. BUS-02 should use `kinds=["assignments"]` when a fresh assignment read is needed, while BUS-03 may use `resources`. Scenario repeats share the real local cache and can therefore be warm after the first run; use a separate isolated/mock fixture for a cold-cache comparison. The real BUS-02 batch and direct sync evidence, including their limitations, is recorded in `docs/campus-agent-tool-refactor-baseline-2026-10-16.md`.

## Requirements and safe operation

- Python 3.11+; API dependencies are already in `apps/api/.venv`.
- A local running project API and an enabled provider ID from the local AI provider settings. The runner never reads or prints provider secrets.
- For campus scenarios, log into the school's test account through the app's controlled browser. Never put a password, cookie, session token, or verification code in CLI arguments or benchmark files.
- Start a campus connector explicitly with `python tools/dev.py --platforms xzzd` (or add `zykt`). Use `--no-platforms` only for API/web-only checks; a bare `--platforms` is rejected to avoid silently skipping the controlled browser.
- Batch defaults: repeat 3, concurrency 1 (maximum 2), per-turn timeout 600 seconds, at most 24 model rounds per run, 120 observed model rounds per batch, and one retry per failed run. These defaults leave room for long multi-step tasks while bounding a batch; the runner cancels active work when either call ceiling or the timeout is reached. Use lower `--max-total-model-calls`, `--max-model-calls-per-run`, or `--max-retries 0` for tighter limits. Dollar cost cannot be safely capped without provider price metadata; `--max-cost-usd` is an operator acknowledgement only, not a billing limit. Start with a selected scenario and monitor provider usage. Token usage is recorded when the SDK supplies it; cost is `null` until a trusted pricing table is configured.
- Only read-only lookup scenarios are in this catalog. Do not add submit/pay/drop/register/send/delete actions without a mock or dedicated sandbox account and explicit safety assertions.

## Commands (PowerShell)

```powershell
python tools/campus_bench/build_tool_map.py
python tools/campus_bench/runner.py list
python tools/campus_bench/runner.py layers --batch-id BATCH
python tools/campus_bench/runner.py mock-e2e --batch-id MOCK-E2E-SMOKE --timeout 120
python tools/campus_bench/runner.py probe --api http://127.0.0.1:8000 --public-notices --batch-id LIVE-PROBE
python tools/campus_bench/runner.py probe --api http://127.0.0.1:8000 --private-courses --academic-year 2026 --semester '1|秋' --batch-id PRIVATE-COURSE-PROBE
python tools/campus_bench/runner.py run --provider-id prv_your_local_id --scenario BAS-01 --repeat 1 --limit 1
python tools/campus_bench/runner.py run --provider-id prv_your_local_id --scenario BAS-03 --repeat 3 --timeout 180
python tools/campus_bench/runner.py run --provider-id prv_your_local_id --max-cost-usd 2.00 --repeat 3 --max-total-model-calls 60 --concurrency 1
python tools/campus_bench/runner.py run --provider-id prv_your_local_id --batch-id BATCH --resume --retry-failed --max-retries 1
python tools/campus_bench/runner.py pause --batch-id BATCH
python tools/campus_bench/runner.py resume --batch-id BATCH
python tools/campus_bench/runner.py compare --before BASELINE --after CANDIDATE
```

The `campus_reviews` deferred-exposure experiment is benchmark-only and defaults off. The `mock-e2e` runner starts its isolated API with the required gate automatically. Reproduce both workloads with `python tools/campus_bench/runner.py mock-e2e --scenario TOOL-REV-01 --batch-id REVIEW-TREATMENT --repeat 3 --defer-tool-group campus_reviews` and `python tools/campus_bench/runner.py mock-e2e --scenario MULTI-02 --batch-id HOLDOUT-TREATMENT --repeat 3 --defer-tool-group campus_reviews`; run matching controls without the flag. A real API used with `runner run --defer-tool-group campus_reviews` must be started with `CAMPUS_BENCH_ALLOW_TOOL_GROUPS=1`. The server filters only model-facing schemas until a successful `load_tools` receipt; execution permissions remain unchanged. Current 3/3 paired evidence uses a scripted model and synthetic SQLite, not a real model or campus service. See the tool refactor baseline for timing and call-count results. Production defaults remain all-visible.

`layers` can run without an API, model provider, or campus account. It creates a new batch directory and manifest when needed, then runs the synthetic direct-tool suite, campus auth/session/WebVPN contract suite, and offline Harness bridge test; `--only direct` runs both tool-foundation suites, while `--only harness` runs the bridge contract alone. `mock-e2e` starts a disposable API, scripted loopback provider, real Harness subprocess, and synthetic course database, then stores its separate evidence layer and report; it does not use the logged-in browser, real campus account, or external campus network. `probe` checks the live local API, registered connection states, and campus authorization; `--public-notices` makes at most one public, credential-free read and records its result. `--private-courses` explicitly makes one read-only `zdbk` course sync and may use the application's bounded single automatic login attempt; the artifact records only count/freshness/completeness/error metadata, never course contents. The sync may update the local campus cache. This option does not call a model. The report separates transport from capability status: an HTTP 200 does not imply campus authorization. A real run executes the local offline layers automatically (unless `--skip-direct-tools` skips only the synthetic direct-tool layer; the Harness layer still runs). A run can be limited with `--scenario`, repeated `--scenario ID`, or `--limit`. Scenarios with `messages` send those turns in order in the same independent conversation, under one end-to-end deadline. Output is under `var/campus-bench/<batch-id>/`: `manifest.json`, `layers/`, `runs/*.json`, `summary.json`, and a self-contained local `index.html` report. `report` reapplies the current rubric only when the saved prompt sequence is unchanged, preserving `evaluation_as_run` and recording current catalog provenance. `compare` also writes `comparison.json` and `comparison.html` into the after-batch directory; when both batches contain scripted `mock-e2e` traces, it emits a separate mock Harness comparison with repeat counts, elapsed ranges, schema visibility, model rounds and tool calls, without mixing those metrics into real-agent success rates. `Ctrl+C` saves completed runs; use `--resume` with the same batch ID to continue. Existing run IDs are skipped unless `--retry-failed` selects unsuccessful/unverifiable rows for rerun.

For a source-aligned real-model run without restarting the existing API, `tools/campus_bench/isolated_api_server.py` seeds a disposable SQLite database and temporary Agent home, binds only to loopback, and references the existing encrypted provider key by ID without reading or copying it. Point `--secrets-dir` at the same protected secret directory configured for the local API; choose a new empty `--state-dir` for every server start. Example (PowerShell; use the provider values shown in local settings, never put a key on the command line):

```powershell
$env:PYTHONPATH = 'apps/api;packages/learning/src'
python tools/campus_bench/isolated_api_server.py `
  --state-dir "$env:TEMP\campus-bench-state-01" `
  --secrets-dir 'var/secrets' `
  --provider-id LOCAL_PROVIDER_ID `
  --base-url 'https://api.example' `
  --model 'CONFIGURED_MODEL' `
  --port 8017
```

In a second terminal, run a selected scenario with a single worker and explicit budgets, then stop only the isolated server with Ctrl+C:

```powershell
python tools/campus_bench/runner.py run `
  --api http://127.0.0.1:8017 `
  --provider-id LOCAL_PROVIDER_ID `
  --scenario BUS-04 --repeat 1 --concurrency 1 --timeout 120 `
  --max-model-calls-per-run 12 --max-total-model-calls 12 --seed 20261017 `
  --skip-direct-tools
```

Do not pass live campus credentials to this disposable server. It has no user's campus database/session; use public-only scenarios unless a dedicated sandbox and isolated test account are explicitly configured. The benchmark may make model API calls, so the model-call ceiling is enforced by the runner, not by a provider dollar cap.

For a tool-description ablation, start the control server with `--condition baseline-campus-args` (restores the Git HEAD descriptions for only `campus.query` and `campus.sync`) and the treatment server with `--condition source`. Give each a different empty state directory; run identical `run` arguments, then compare with repeated `--target-tool campus.query --target-tool campus.sync`. The comparator verifies target tool fingerprints, flags any non-target tool fingerprint changes, and distinguishes staged whole-snapshot changes from per-tool schema changes. Its comparison remains descriptive when live source, cache or model sampling cannot be fully paired.

## Interpretation and iteration

Success, partial, failed, and unverifiable are distinct. Tool evidence is extracted from the event journal; status 200 and a final answer alone do not prove success. The simple lexical checks in the current catalog are screening signals, not truth judges. Validate time-sensitive claims against a trusted independent source and manually inspect open-ended samples before making product changes. Record the batch ID, git commit, catalog hash, model/provider configuration (non-secret), and account/session cold or warm state when comparing changes. Keep an untouched scenario subset for final checks.

`BUS-02` now uses a deterministic, privacy-preserving date oracle: the runner compares final-answer dates to the raw `assignments.list` results in memory and persists only counts/match status. It accepts both UTC and Asia/Shanghai calendar labels for timestamped source facts, because the tool returns UTC while users normally read campus deadlines in China local time. It never writes assignment titles, course IDs, instructions, or exact dates to benchmark artifacts. A match against a partial local snapshot is only `部分成功`; ungrounded dates are `失败`; missing/fully redacted source evidence is `不可验证`. This does not verify assignment names or prove school-side completeness. The 2026-10-16 schema experiment and its before/after traces are in `docs/campus-agent-tool-refactor-baseline-2026-10-16.md`, with the per-run report under `var/campus-bench/20261016-184627-3adc4782/`.

Root-cause reports mark trace evidence as confirmed and suspected explanations as hypotheses. Compare paired scenario results with `compare`; it reports per-scenario success-rubric deltas, latency means/ranges, call counts, and an efficiency direction. Treat these as descriptive unless repeat count, model, session temperature, and source data are matched. Retain a change only if affected tasks improve without key regressions. For each code change, save before/after batch IDs and run affected cases plus the holdout set. Do not use these heuristic scores as a product KPI until the rubric is calibrated against human annotations.

`BUS-09` 增加逐课程下一项作业的受控缓存场景：不允许联网/登录，要求使用 `assignments.next_by_course`、核对日期来源、说明本地缓存边界。该工具结果不会保存到 Benchmark 轨迹，评分只保留去标识化计数。当前真实重复批次中工具选择和缓存边界通过，但最终日期回答均有来源不支持的内容，故全部判失败；详见最新工具重构基线文档与 `var/campus-bench/20261016-201803-93dd967c/index.html`。这表示查询工具改造没有解决模型事实利用问题。

#### BUS-09 受控缓存回答收敛复测

2026-10-16 的 `20261016-202719-251ddaf6` 在不联网/不登录的条件下真实运行 Agent 三次。新 Harness 窄路径把 `assignments.next_by_course` 的可信数据渲染为可核验日期表；三次均为 17 行/17 门不同课程、零重复、零来源外日期、未观察到同步，并披露本机缓存边界。结果计为 3 部分成功，非 3 成功：本地缓存不能证明覆盖学校端全量数据。

这一轮解决的是 BUS-09 的来源外日期输出，不是全工具体系改善。平均调用由基线 2.33 工具/2.67 模型变成 3.00/3.33，平均耗时 33.79 秒变成 35.64 秒；未见效率收益。场景数据及解释见 [工具重构实测记录](campus-agent-tool-refactor-baseline-2026-10-16.md) 和本地批次 `var/campus-bench/20261016-202719-251ddaf6/`。测试通过日期内容的内存 oracle，落盘只保留聚合证据。

BUS-09 的分页渲染器现把工具实际返回的游标与后续成功续页调用关联：续页完成不再误报不完整；存在未消费游标或缺少关联证据时仍会提示未完成。此分支通过双页合成 Harness 测试，最新真实缓存批次未产生 `next_cursor`，因此真实环境的多页路径仍标为尚未验证。实验和非因果对照详见基线记录的“BUS-09 第四轮”。

#### BUS-09：逐课程缓存状态覆盖

专用 `assignments.next_by_course` 现按授权本机课程逐门返回下一项已知截止作业、仅未知截止或无缓存记录状态，并按课程分页；“无本机记录”不等于“学校端无作业”。真实 n=3 批次 `20261016-210107-fa3589ef` 三次都覆盖33门本机课程（17门有已知截止项、16门无缓存作业记录），日期来源核验0个错误，均为部分成功，因为学校端完整性仍不可证。相对旧批次，模型不再调用通用课程/作业列表拼接，但额外状态读取仍存在，工具调用和平均时延未改善。前后对照是描述性的，runner/catalog与缓存状态未配对；不据此声称因果或全局提升。报告和限制见 [逐课程实测记录](campus-agent-tool-refactor-baseline-2026-10-16.md)。

#### BUS-09 真实模型冗余候选调用信号（2026-10-17）

真实批次 [`20261016-233704-07ffb8b9`](../var/campus-bench/20261016-233704-07ffb8b9/index.html) 为 n=1，DeepSeek provider、串行、最多4次模型调用、120秒单任务超时。Agent 通过 `assignments.next_by_course`、`assignments.list`、`courses.list` 三次调用；三个调用均有 Harness call/result 关联，工具错误为0、重试为0、校园同步为0。耗时44.84秒，首次 assistant message 14.13秒；观测到输入20,863、输出5,163 tokens，缓存读取69,376 tokens；成本未知。场景专用结果已含所需课程信息，且另外两工具描述建议使用专用工具，因此评分器将列表调用记为“场景定义的冗余候选”，诊断 I 为 suspected。作业日期与来源对齐，但只读本机缓存无法证明课程/作业全量，任务仍为部分成功。

此次只改评分与诊断证据表达：BUS-09 要求精确工具 `assignments.next_by_course`，并把 `assignments.list`、`courses.list` 记录为冗余候选；不会把候选工具当成禁止工具。真实 n=3 历史批次的工具调用行为不同，所以尚不能确认这是稳定模型选择缺陷；没有据此改产品 schema/Harness。重建报告后的 `test_runner.py`：49 passed；`git diff --check` 通过。该次实验优化了可诊断性，未证明 Agent 真实完成率改善。


#### ERR-10：认证过期时停止重试（隔离 Harness E2E）

`ERR-10-AUTH-STOP-MOCK-N3-20261017` 使用独立临时 SQLite 将 xzzd 连接状态置为 `auth_required`，通过正常 Agent API、真实 Harness 和 `connections.sync` 执行器运行；脚本 provider 控制模型往返，未连接校园服务。3/3 均单次调用 `connections.sync` 得到 `AUTH_REQUIRED`，错误回传下一模型轮，之后输出登录后继续的指引；每次2次模型调用、1次工具调用，无重试或 `jobs.read` 轮询。端到端环境耗时15.57–15.80秒（含独立 API 启动）。该 fixture 证明执行器错误处理与 Harness 回传，不证明真实模型决策或人工登录后恢复；ERR-04 的真实会话过期路径仍未验证。报告位于 `var/campus-bench/ERR-10-AUTH-STOP-MOCK-N3-20261017/index.html`。

### BUS-10 assignment/course schema A/B (2026-10-17)

The confirmed schema-only treatment removed the extra `courses.list` call in 3/3 BUS-10 runs; all remain partial successes because local cache completeness is unknown. See the [experiment record](campus-agent-tool-experiment-bus10-2026-10-17.md) and batch comparison at `var/campus-bench/20261017-015958-054702e8/comparison.html`. This result applies only to the tested assignment task.

### BUS-11 directory holdout

The protected holdout explicitly asks for the local course directory, so it checks that the schema change which reduced BUS-10's extra `courses.list` call does not hide the tool when the user genuinely needs it. Real Agent batch `20261017-022743-6e9d91eb` passed 3/3: one `courses.list(limit=5)` call per run, zero assignment/sync tools, two model calls, and deterministic in-memory comparison of all 5 titles and terms plus the cache-boundary disclosure. The trace persists only match counts and status; final answers and course data remain redacted. This verifies a local cache query, not the completeness of school-side course data. See the [BUS-11 experiment record](campus-agent-tool-experiment-bus11-2026-10-17.md) and [run report](../var/campus-bench/20261017-022743-6e9d91eb/index.html).

#### ERR-11: empty assignment cache in the Harness

An isolated scripted-model E2E batch passed 3/3 through the normal Agent API and real local Harness/executor. In each run, `assignments.list(order_by=due_soon, limit=10)` returned zero rows, no cursor, `partial`, stale freshness, and an incomplete-coverage marker; the exact tool result was returned in the next provider request, and no sync tool ran. This confirms the current contract behavior for an empty local cache only; it is not a real-model or campus-platform result, and no production Agent change was justified by this fixture. See the [ERR-11 experiment record](campus-agent-tool-experiment-assignments-empty-cache-2026-10-17.md) and [run report](../var/campus-bench/ERR11-EMPTY-ASSIGNMENT-VERIFIED-20261017/index.html).

#### BUS-12: assignment cursor continuation in the Harness

The isolated three-row fixture passed both the smoke and default n=3 batch through the normal API/Harness path. All three runs forwarded the exact page-one cursor to page two; pages returned 2 then 1 rows, with three unique records total and no final cursor. Each run used 3 model calls and 2 tool calls; end-to-end mean was 13.60 seconds including disposable API setup. This verifies the scripted execution path only; real-model pagination and school-side completeness remain unverified. See the [BUS-12 experiment record](campus-agent-tool-experiment-assignments-pagination-2026-10-17.md) and [run report](../var/campus-bench/BUS12-ASSIGNMENT-PAGING-N3-20261017/index.html).


工具暴露对照（`--defer-tool-group campus_reviews`）只支持走校园模型网关的 Provider，因为该实验需要网关按会话筛选并回传工具 schema。runner 会在创建实验批次前验证 Provider 类型；官方 DeepSeek 直连时明确拒绝，不产生场景运行记录。若本机没有校园网关 Provider，则该对照标记为未执行，不用直连 Provider 绕过限制。
