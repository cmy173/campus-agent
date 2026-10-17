# BUS-04 public activity tool-chain experiment — 2026-10-17

## Scope and safety

All new runs went through the normal Agent conversation/turn API and the real local Harness. They used one configured DeepSeek provider, concurrency 1, seed `20261017`, disposable isolated API state, and read-only public campus data. They did not use the live user's database or campus credentials. The configured provider key was referenced from the existing DPAPI store by ID and was never copied into the temporary state or benchmark artifacts. The pre-existing API on port 8000 was left untouched.

## Observed runs

| Batch | Condition | Model-call budget | Result | Model calls | Tool calls | End-to-end |
|---|---|---:|---|---:|---:|---:|
| `20261017-080551-c3905bbf` | Existing live API, 3 repeats | 4 per task | 3/3 censored and unverifiable | 4 each | mean 6.67 | see prior report |
| `20261017-082142-e1386954` | Isolated old notice-read description, 1 pilot | 6 | budget-cancelled | 6 | 11 | 37.38 s |
| `20261017-082423-8f69d02a` | Isolated source description, 1 pilot | 6 | budget-cancelled | 6 | 7 | 26.56 s |
| `20261017-082624-c2bf3355` | Isolated source description, warm public cache | 8 | succeeded; partial after rescoring | 4 | 5 | 25.77 s |
| `20261017-083119-971455f1` | Source-aligned API; browser-source evidence present | 8 | succeeded; partial | 7 | 10 | 53.87 s |
| `20261017-083729-c742dd13` | Query-description treatment pilot | 8 | budget-cancelled | 8 | 10 | 55.74 s |
| `20261017-084030-f33aee98` | Query + sync argument guidance; source-aligned API | 12 | succeeded; partial after privacy-safe rescoring | 7 | 10 | 43.61 s |

The six-call control and treatment pilots did not invoke `campus.notice.read`, so the notice-read description was not engaged. Their call-count and latency differences are descriptive only; they are not evidence of causal improvement. The later batches changed cache, budget, and tool descriptions, so they are also not a controlled A/B comparison.

The runner's explicit comparison `20261017-083119-971455f1` → `20261017-084030-f33aee98` reports unchanged partial result, unchanged 7 model/10 tool calls, and an observed latency difference of −19.1%. It also flags different runner hashes, different observed schema fingerprints (runtime schema drift within each batch), and unpaired account/cache/network state. Therefore the latency difference is not causal evidence and must not be presented as a measured speedup.

## Evidence from the completed 12-call-budget batch

- The turn completed in 7 model calls and 10 tool calls; the run did not hit its configured budget.
- The Agent selected the campus notice cache, campus sync, public web search/fetch, and the authorized browser navigation path.
- Four anonymous `web_fetch` calls were blocked by the public-address safety policy. The authorized browser subsequently opened the public International Campus events page; this is a successful fallback, not evidence that the whole network was unavailable.
- The browser page snapshot contained all nine cited event URLs and the dates used in the response. The deterministic verifier matched 10/10 citation URLs and all eight date claims; 9/10 citation labels aligned with observed source text. The tenth label was generic and needs human review. Report score: **partial**, not full success.
- One separate `campus.query` call was classified as account-derived data and caused the final answer text to be redacted. The report preserves only the safe aggregate source oracle (9 aligned labels of 10, dates verified), not the answer body or private result. This also exposes an unnecessary personal-data access risk for a public activity request.
- The initial eight-call attempt before this batch ended at the call budget. The successful batch used the same budget but completed; this variation is consistent with model non-determinism and is not a measured improvement.

## Evidence-led code changes

### 1. Public browser evidence in the deterministic oracle

The evaluator previously read public sources only from structured `meta.sources` fields and missed source links embedded in an authorized browser page snapshot. It consequently marked a successful official-page retrieval as unverified. The evaluator now correlates browser navigation call/result IDs, resolves relative links against the observed page URL, and extracts page dates. It accepts a generic citation label such as “原链接” only when the exact linked URL was present in the returned page. It does not infer sources from URLs absent from the trace.

When a final response must be redacted, the report persists only source counts and date-verification status. An aligned but redacted response is capped at partial success. Partial evidence remains partial and reports the counts. The verified run above was rescored from its saved aggregate oracle as 9/10 aligned labels and “部分成功”.

### 2. Campus query/sync parameter guidance

Earlier traces showed public notice reads returning `INVALID_INPUT` when the model invented a `limit` query argument; a later trace showed `campus.sync` rejecting the invented `wait_seconds` field. The shared schema only exposes `capability`, `query`, and optional `platform`. The two campus tool descriptions now explicitly limit arguments to the declared contract: notice queries should omit unsupported filters, and campus sync does not take `wait_seconds`.

The first live query-schema treatment used `{}` rather than the previously observed `limit: 50`, but the task was censored at eight model calls and the Agent then issued a sync with unsupported `wait_seconds`; do not count it as a successful optimization. The later 12-call sample did not use `wait_seconds`, but had other errors and a distinct personal-data call. A three-repeat paired baseline/treatment is still required before claiming a reliable behavioral change.

### 3. Error attribution precision

The diagnosis previously attached every code in a mixed failure list to the external-network root cause when any one code was a network error. It now keeps `INVALID_INPUT` evidence under tool/parameter selection (`C`) and only attaches network/authentication codes to external dependency (`H`). Mixed failures can therefore retain separate confirmed causes.

### 4. Parameter-scoped private-data constraints

The real trace exposed an account-derived `campus.query` result in a public activity task, but tool-name-only redundancy rules could not distinguish it from the necessary `campus.query(capability=notices)` call. BUS-04 now forbids only the non-public campus capabilities at the argument level. The evaluator compares them in memory and persists only the tool name if a violation occurs; the argument value and result remain redacted. This policy has unit coverage but has not yet been behaviorally measured with another real-model run.

## Verification

- Benchmark runner tests: `65 passed` after source snapshot, aggregate redaction, generic citation-label, and error attribution updates.
- API tool/Harness/privacy suites: included in the combined run below.
- Combined targeted verification: `133 passed` across `tools/campus_bench/test_runner.py`, `apps/api/tests/test_agent_tools.py`, `apps/api/tests/test_campus_bench_harness.py`, and `apps/api/tests/test_campus_readonly_boundary.py`.
- The report was rebuilt from the saved batch after updating the evaluator. No model request was repeated during rescore.
- `build_tool_map.py` was rerun after the tool contract edits; the map contains 34 domain tools, 9 native groups, and all 43 benchmark scenarios.

## What is and is not established

**Confirmed:** the real Agent and authorized browser can retrieve the official International Campus activity listing; the cited page links and activity dates in this sample align with the captured snapshot. The anonymous fetch safety policy blocked four URLs. One additional account-domain query was made and the answer text was redacted. Direct `INVALID_INPUT` responses occurred for arguments outside the runtime tool schema in earlier traces.

**Not established:** that the current schemas reduce invalid arguments over repeated trials; that all citations are correct (one label needs review); that this one campus's event page represents all campus events; or that the Agent will avoid unnecessary personal-data reads in future tasks. The `notice.read` schema A/B remains untested behaviorally because neither pilot selected it.

## Next priority

1. Add a public-only BUS-04 fixture and a tool-selection constraint that treats account-domain reads as forbidden for this public task, then run 3 paired source/control repeats with the same cold-cache state and a 12-call budget.
2. Keep browser fallback available after `WEB_BLOCKED_URL`, but measure whether multiple same-domain anonymous fetches can be avoided without reducing verified citations.
3. Add assertions that schema-invalid optional fields are rejected or guided before execution, and persist sanitized validation error details sufficient to distinguish field errors.
4. Keep public activities limited to the observed source page and state campus scope. Do not represent the event listing as a university-wide feed.
5. Re-run BUS-04 three times against the argument-scoped constraint with the same cold-start cache and fixed schema snapshot; record whether the unnecessary private read disappears.
