# Tool coverage and direct baseline — 2026-10-17

## Scope

This is a read-only audit of tool coverage and direct executor tests. No campus account data, campus services, model calls, or write-capable operations were exercised in this batch. Repository HEAD at test time: `650a9348e859ee24544c85fce1a3f397ae3e2345`; the working tree was dirty, so this is not a clean-commit replay.

## Results

- Direct API executor baseline: **114 passed** across `test_agent_tools.py`, `test_agent_plan_actions.py`, `test_agent_feedback.py`, `test_agent_connection_sync_tool.py`, and `test_reviews_agent.py` (88.36 s).
- Export test collection was attempted with those modules, but `test_artifacts_export.py` could not be collected because the current Python environment lacks `openpyxl`. Its tests did not run; this is an environment dependency blocker, not a code failure.
- The isolated tool-map generator tests pass: **5 passed**. `python tools/campus_bench/build_tool_map.py` regenerated the map from the current `tools.py:SPECS` and saved traces.
- Runtime schema route probe: unauthenticated `GET /api/v1/agent/internal/schemas` returned 405; unauthenticated `POST` returned 403. The route is runtime-authorized, so the loaded API registry could not be read through this probe. No authorization bypass was attempted.

## Agent scenario coverage gaps

The generated tool map has 34 API domain tools and 43 catalog scenarios. Eleven registered tools currently have **no exact tool scenario crosswalk**:

`campus.imported.read`, `campus.notice.import`, `plans.read`, `plans.apply`, `plans.revoke`, `jobs.cancel`, `tasks.feedback`, `artifacts.export`, `reviews.generate`, `reviews.read`, `reviews.export`.

Some are covered by direct unit tests, but that does not establish Agent schema selection, Harness dispatch, tool-result handoff, or user-task completion. Write-capable tools must be tested only with isolated synthetic state or dedicated sandbox fixtures. These are coverage gaps, not proof that their implementations fail.

## Schema comparison interpretation

The map compares historical model-visible schema snapshots with the current source registry. It found 4,816 field-difference observations across the history it scanned. Most snapshots lack an Agent runtime fingerprint; even a fingerprint would not identify the registry loaded by a separate API process. Therefore this count is historical comparison evidence only, not a live runtime mismatch or confirmed root cause. The route probe above independently confirms that the live endpoint is protected, but does not reveal its schema.

## Next verification priorities

1. Make the export test dependency available through the project-supported test environment, then run `test_artifacts_export.py`.
2. Add isolated synthetic Agent/Harness scenarios for the eleven uncovered tools, prioritizing read tools and local-only writes first; never use real submit, delete, payment, or enrollment paths.
3. Add a safe, authorized way for benchmark runs to capture the live API schema registry fingerprint alongside the provider-visible schema snapshot, so future schema differences can be attributed to the correct process/version.

## Current-version usability smoke (2026-10-17 09:51 local)

Batch `20261017-095122-07a19700` ran `BUS-11` once through the live local Agent API (`http://127.0.0.1:8000`) with the configured real model provider and a separate test conversation. It used the existing local course cache only; no remote campus access, login, sync, or writes were permitted.

- Result: **1/1 confirmed success** by the deterministic course-name/term oracle before content redaction; the persisted report contains counts and redacted output only.
- Execution: `courses.list` exactly once; 2 model calls; zero tool errors, retries, confirmation events, or redundant tool candidates.
- Timing: 17.01 s end-to-end; first assistant event 13.45 s; observed model stream wall time 4.09 s and tool time 0.057 s. The first journal event arrived about 11.92 s after runner submission. The remaining startup/queue interval is not currently split into stages, so its cause is unknown and should be measured before assigning a root cause.
- Harness dispatch contract layer: passed. The direct synthetic tool suite was skipped for this live smoke because the direct baseline above was recorded separately.

This is a narrow usability check, not a claim that the whole Agent is reliable. It does prove that one current, safe cache-read user task completed through the normal API/Harness/model/tool path.
