# BUS-04 public notice retrieval diagnosis — 2026-10-17

## Real Agent baseline

Batch `20261017-080551-c3905bbf` ran BUS-04 three times through the real Agent API with the configured provider, concurrency 1, seed `20261017`, four model calls per task, twelve total, and no retries. It was read-only. The task requested recent campus activities and required a source and date for each listed item.

All three runs ended at the configured model-call budget and were **unverifiable**. The final response was redacted because the Agent also invoked a personal campus status tool; no private response text is preserved here. These runs are not counted as completed tasks.

| Observation | Result | Evidence strength |
|---|---:|---|
| `campus.query(capability=notices)` | 3/3; results were stale and partial, with 6–7 cached records | Direct structured tool trace |
| `campus.status` | 2/3; unrelated to listing public event facts | Direct call trace; benchmark marks as redundant |
| `campus.sync(capability=notices)` | One task attempted it; first call failed on extra `wait_seconds`, next returned stale/partial data | Direct tool trace; read-only public-notice refresh |
| `campus.notice.read` | 6 calls; every call returned `UNSUPPORTED` | Direct tool result code |
| `campus.notice.read` burst | Four distinct records were requested in one model response in run 2 | Provider selected-tool list and four correlated calls |
| `web_search` / `web_fetch` | Search ran in 2/3; a fetched campus-host URL was blocked by DNS resolving to a non-public IP | Direct Harness events and `WEB_BLOCKED_URL` recovery result |
| Tool calls / task | Mean 6.67; model calls 4/4 | Batch metrics |

The `UNSUPPORTED` result is consistent with the implementation: `campus.notice.read` resolves only cached `college_notices`, while BUS-04's successful list call used public `notices`. The old tool description said the ID could come from any `campus.query/sync`, obscuring this distinction. Because the four reads were issued together for four distinct IDs, exact-call deduplication would not have prevented this burst.

## Root cause and limits

- **Confirmed contract mismatch (high confidence):** the tool description named the broad query/sync path; the executor only reads college notices. Six public-notice IDs produced `UNSUPPORTED`.
- **Confirmed scope inefficiency (high confidence):** `campus.status` ran twice although the task required public event facts. Its model-visible description did say not to use it for public activities, so schema wording alone is not proven to explain these calls.
- **Confirmed data limitation (high confidence):** the local notice cache was stale/partial; one requested public host was rejected by the anonymous fetch SSRF guard. This does not prove that the page is unavailable to a browser or that campus internet is down.
- **Not established:** whether the Agent could have completed the task within a larger model-call budget, whether a new live service would reduce these calls, or whether the local source has any newer activity beyond the observed cache/search results.

## One-factor change

Changed only the `campus.notice.read` contract guidance and its error recovery message. The tool now states that it only accepts `college_notices` records; public `notices` summaries do not support body reads, and their title/date/source should be used directly. `UNSUPPORTED` and missing-record responses include that same actionable scope guidance without exposing backend diagnostics. BUS-04's rubric now records `campus.status` and `campus.notice.read` as redundant candidates for a title/date/source listing task.

## Verification

- Before change, API-focused baseline: `51 passed` across `test_agent_tools.py` and the related campus/tool gateway tests.
- After change, direct tool, read-only boundary, and Harness suites: `67 passed`, including the error round-trip assertion that the sanitized `UNSUPPORTED` guidance reaches the next model request.
- Benchmark baseline was rescored with the updated rubric, but remains 3/3 unverifiable because final output was redacted and all runs hit the call budget.
- The active API process predates the working-tree edits. No live treatment batch is claimed; restarting it triggers app startup/recovery and connector registration. Re-run the real BUS-04 batch against a source-aligned process to measure whether the six invalid calls disappear and whether public event facts can be verified.

## Next validation

Use the same provider, seed, concurrency, and environment, with a bounded six-call budget to avoid censoring the expected public search/fetch chain. Compare `campus.notice.read` calls, personal status calls, exact source/date coverage, task completion, and latency. Until then, this is a confirmed tool-contract correction with Harness proof, not a demonstrated real-model behavior improvement.
