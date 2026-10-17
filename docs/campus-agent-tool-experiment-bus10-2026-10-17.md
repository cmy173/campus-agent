# BUS-10 assignment/course tool experiment — 2026-10-17

## Question and isolated change

The agent was asked for the ten nearest cached assignments with course names, without login or synchronization. The evaluator requires `assignments.list(order_by=due_soon, limit=10)`, forbids `courses.list`, verifies date facts against tool evidence, and only grants partial credit because local cache coverage cannot prove school-side completeness.

The falsifiable hypothesis was that the model was using `courses.list` only to map assignment course IDs to course names. The only product change was the `courses.list` description: it now says to use that tool for course-directory browsing/filtering or an independently requested local course-list check, and explicitly says not to call it just to map `assignments.list` results because those already contain `course_title` and `course_term`. No permissions, executor, cache, Harness, or tool availability changed.

## Evidence and batch validity

| Batch | Runtime schema | Runs | Result | Tool calls/run | Model calls/run | Mean elapsed |
|---|---|---:|---|---:|---:|---:|
| `20261017-014938-ca62df7c` | Previous description | 3 | 3 failed exact-tool oracle | 2.00 | 3.00 | 29.47 s |
| `20261017-015310-812fbbee` | Previous description; invalid as treatment | 3 | 3 failed exact-tool oracle | 2.00 | 3.00 | 26.51 s |
| `20261017-015804-09c4b7d4` | No model run; empty DB | 3 blocked | 0 model calls | 0 | 0 | N/A |
| `20261017-015958-054702e8` | New description confirmed in every run's provider schema snapshot | 3 | 3 partial successes | 1.00 | 2.00 | 21.05 s |

The first treatment attempt was rejected as invalid evidence because the long-running API process did not hot reload; its model-visible schema snapshots still contained the old `courses.list` description. The next attempt used an isolated API on port 8010 but initially had no `provider_configs` table and made no model calls. It was stopped and restarted with the workbench's existing SQLite database and files directory, with the in-process worker disabled. These setup attempts are retained and are not counted as product failures or successes.

For the valid batches, provider ID, scenario, seed (20261017), repeat count (3), concurrency (1), timeout (100 s), per-run model-call cap (8), total cap (24), catalog hash, runner hash, and Git head match. The valid treatment's schema snapshot contains the new description. The before batch and treatment do not have a fully captured/matched account cache or connector/network state; elapsed time and token differences are therefore descriptive, not causal.

## Outcome and diagnosis

In the valid treatment, all three model runs requested and executed exactly one tool, `assignments.list`; none selected or executed `courses.list`. The prior valid baseline requested both tools in all three runs. This is direct evidence that the schema-only change reduced the extra course lookup for this task. The benchmark outcome moved from 0/3 failed to 3/3 partial success. Each run remained partial because the current local snapshot cannot establish the complete school-side assignment set; this limitation is not fixed by reducing tool calls.

Observed mean tool calls changed 2.00 → 1.00 and model calls 3.00 → 2.00. Mean elapsed time changed 29.47 s → 21.05 s; P95 changed 33.23 s → 23.27 s. Because cache and live connector state were not strictly paired, no speedup claim is made. Mean observed input/output tokens changed 9,156/2,391 → 6,874/1,815; cost is unknown because provider pricing is not configured in the benchmark.

The batch comparison is saved at [comparison.html](../var/campus-bench/20261017-015958-054702e8/comparison.html) and [comparison.json](../var/campus-bench/20261017-015958-054702e8/comparison.json). It correctly warns that non-exposure configuration and cache/network state are not fully matched.

## Regression evidence and next work

- Synthetic direct-tool layer: 20/20 passed.
- Campus authentication/session/WebVPN contract layer: 76 passed; mocked contracts only.
- Harness model/tool round-trip contract: 7 passed; fixed mock provider.
- Targeted course/assignment API regressions: 16 passed.
- New assignment/course schema assertion: 2 passed.
- Benchmark runner + tool-map regression suite after provider preflight: 57 passed.
- A live invalid-provider CLI probe exited before creating a batch; batch directory count stayed 147 and no model call was made. The safe provider endpoint exposed only ID, enabled state, and `has_api_key`; no key value was read.
- Tool catalog regenerated from 169 API runs and 40 schema-snapshot batches: 34 domain tools, 9 native groups, 40 scenarios.

Retain the description change for now: it improved exact-tool choice in 3/3 current task repeats without changing the partial-success boundary. This does not establish improvement across the assignments family or the full tool system. The runner now preflights that the requested provider exists, is enabled, and has a key configured, so a mismatched empty database fails before batch creation or model calls. Next, validate the schema distinction on a held-out assignment query that genuinely requests a course directory, then add empty/expired-cache and date-pagination scenarios. A broader environment fingerprint should still capture only non-secret database identity, effective schema hash, and connector/cache state for strict batch matching.

Real campus account access was not exercised in these batches. The live probe showed `browser_session_authorized=false`, disconnected campus workspace, and a stale/degraded connector status. No login, sync, or external campus read was triggered.

The provider preflight only proves that the selected local configuration is present/enabled and has a key configured. It does not test whether that key is accepted by the remote provider; that would require a separate, budgeted connection test.
