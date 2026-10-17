# BUS-11 post-pagination holdout and runtime schema drift — 2026-10-17

## Experiment

Batch `20261017-074921-7b4d9e27` ran BUS-11 three times through the real Agent API using the configured provider, three independent sessions, concurrency 1, a 120-second per-task timeout, at most two model calls per task, six total model calls, and no retries. The task only reads the local course cache; no campus network, login, or synchronization was used.

The objective oracle matched the private course names and terms in memory, then retained only aggregate evidence. All three runs returned aligned course facts and disclosed the local-cache boundary. The agent made one `courses.list(limit=5)` call in run 1. Runs 2 and 3 also called `connections.status`, which cannot establish course-directory contents or cache completeness and is therefore redundant for this task.

| Metric | Result |
|---|---:|
| Repetitions | 3 |
| Course facts aligned | 3/3 |
| `courses.list` selected | 3/3 |
| Redundant `connections.status` | 2/3 |
| Tool calls | 1, 2, 2 (mean 1.67) |
| Model calls | 2 each |
| Elapsed time | mean 17.67 s; P50 17.88 s; P95 18.18 s |
| Recorded confirmation events | 0 |

The report now scores the two redundant-call runs as partial success while retaining the deterministic content oracle. This distinguishes task correctness from tool-use quality. The BUS-11 rubric explicitly marks `connections.status` as a redundant candidate for a local course-directory request.

An offline comparison was generated between this batch and `20261017-022743-6e9d91eb`. It confirms the prompt matched, but the rubric did not; the two model-call schema hashes also differed (`76b30b5d…` vs `4e5e21b0…`). Both batches had one consistent schema fingerprint internally, but not the same fingerprint. The runner now emits this evidence and warns that tool-call and latency differences are confounded by schema drift. The historical `3/3 success` to `1/3 success + 2/3 partial` change reflects the stricter redundancy rubric, not a task-completion regression.

## Runtime schema evidence

The model-visible schema snapshot from the latest live API batch differs from the current source registry. `campus_courses_list` had a 21-character description in the live snapshot and a 190-character description in source; `campus_connections_status` also differed. Both parameter schemas matched. The older BUS-11 batch `20261017-022743-6e9d91eb` matched current source descriptions for both tools.

This confirms schema drift between the live process and repository source at test time. It does not prove that the drift caused the two extra status calls. A stale API process or another runtime schema source are plausible explanations; the process was not restarted because its startup has external connector side effects. The selected tools and schema snapshots are retained in the per-run traces for further diagnosis.

The current-source path was independently exercised in `BUS11-CURRENT-SOURCE-HARNESS-N3-20261017`: 3/3 isolated scripted Harness runs selected only `campus_courses_list`, executed one local `courses.list`, returned its result to the next provider request, and completed. Each used two model calls; elapsed time was 13.24–13.50 seconds including disposable API setup. This validates local source registration, dispatch, and feed-forward only; it does not test real-model selection, live campus data, or the older process.

## Comparison limits

The earlier batch `20261017-022743-6e9d91eb` used one tool call per run (mean 1.00) and averaged 18.07 seconds. The current batch averaged 1.67 calls and 17.67 seconds. This is descriptive only, not a causal before/after estimate: runner/catalog hashes, call/retry budgets, cache/runtime state, and live schema snapshot differ. Do not claim latency improved or regressed from these two batches.

## Changes and verification

- Updated the BUS-11 rubric to flag `connections.status` as redundant without incorrectly treating it as a forbidden capability.
- Fixed report-time oracle restoration so it preserves the independently recorded redundant-tool evidence after private answer text is redacted.
- Added a regression assertion for aligned oracle output with a redundant tool call.
- Added a reproducible tool-map audit comparing every captured real model-call tool schema with the current source registry. The scanned history contains 2,909 differing tool observations; this aggregate spans historical code versions and is not a single-current-process measurement. Per-tool difference fields are exported in `docs/campus-agent-tool-map.json`.
- Added model-visible schema fingerprint summaries to batch comparisons, including mixed-schema and cross-batch drift warnings. [Offline comparison report](../var/campus-bench/20261017-074921-7b4d9e27/comparison.html)
- Ran the current-source isolated Harness E2E n=3; all three selected and executed the local course-list tool and passed the result forward. [Sanitized fixture report](../var/campus-bench/BUS11-CURRENT-SOURCE-HARNESS-N3-20261017/index.html)
- Benchmark unit tests: `64 passed`.
- Batch report: [20261017-074921-7b4d9e27](../var/campus-bench/20261017-074921-7b4d9e27/index.html)

This round changes evaluation and diagnosis only; it does not change the Agent tool schema or claim a product-level improvement. Next, align the live API process schema with the registered source in a controlled environment, then repeat BUS-11 with otherwise identical settings and compare redundant status-call frequency. The key test remains whether explicit directory intent is handled with one relevant local read, without suppressing legitimate connection-status requests in other scenarios.
