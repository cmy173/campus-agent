# BUS-11 course-directory holdout and privacy-safe oracle — 2026-10-17

## Purpose

BUS-11 is the holdout for the BUS-10 assignment/course schema experiment. It asks for up to five courses from the user's local cache, with term, while prohibiting assignment lookups, login, refresh, and sync. The test checks that `courses.list` remains available and is selected when course-directory browsing is the actual user goal.

The local database preflight found 33 course rows (count only). The normal Agent API and current provider were used on an isolated API port pointed at the same local database; the duplicate background worker was disabled. No campus service or network request was made.

## Evaluation defects found and fixed

The first two n=3 attempts selected and executed `courses.list(limit=5)` in every run, but could not produce trustworthy persisted task scores:

- Provider-facing `tool/result` omitted call ID while exposing `sourceEventSeqs` that linked it to `tool/call`. Tool-selection evidence only correlated by call ID and falsely reported missing results.
- Report rebuilding replaced all private-output scores with `不可验证`, discarding any safe in-memory task oracle.
- The first catalog draft also used a broad `tools_any` alias that did not match the runtime name. It was removed in favor of exact tool and argument constraints.

The runner now correlates call/result by source event sequence. BUS-11's dedicated oracle compares returned course titles and terms with the assistant's answer in memory, then persists only returned/matched counts, a status, and cache-boundary disclosure. Course names, terms, IDs, and answer text remain redacted. Report rebuilding restores only this allowlisted aggregate.

## Final repeated run

Batch `20261017-022743-6e9d91eb` used the real Agent API, current code, configured provider `prv_c68d2878`, independent conversations, seed 20261017, serial concurrency, 100-second task timeout, 8 model calls per run, 24 total, and zero retries. The user task was read-only and restricted to local cache.

| Run | Outcome | Tool calls | Model calls | Course rows matched | Cache boundary | Elapsed |
|---|---|---:|---:|---:|---|---:|
| 1 | Success | 1 | 2 | 5/5 titles, 5/5 terms | Disclosed | 18.01 s |
| 2 | Success | 1 | 2 | 5/5 titles, 5/5 terms | Disclosed | 17.37 s |
| 3 | Success | 1 | 2 | 5/5 titles, 5/5 terms | Disclosed | 18.82 s |

All three runs selected/executed only `courses.list(limit=5)`; no assignment or sync tool was called. Mean elapsed time was 18.07 s, mean tool calls 1.00, and mean model calls 2.00. This validates the exact five returned local rows, not the completeness of the local snapshot against school-side course data.

## Regression and reports

- `tools/campus_bench/test_runner.py` covers source-sequence result correlation, deterministic title/term comparison, mismatch detection, redacted report restoration, and non-persistence of fixture names and call IDs.
- Direct synthetic tool layer: 20/20 passed; auth/session/WebVPN contract: 76 passed; Harness model/tool round trip: 7 passed.
- `test_build_tool_map.py` + `test_runner.py`: 60 passed, including privacy checks that persisted traces contain neither course rows nor answer text.
- The latest tool map scans 178 real API runs and 43 schema-snapshot batches, covering 34 domain tools, 9 native groups, and 41 scenarios.
- [Batch report](../var/campus-bench/20261017-022743-6e9d91eb/index.html)

The course-directory holdout passed after the BUS-10 schema change, but the BUS-10 efficiency/correctness improvement remains scoped to its assignment task; these runs do not establish a global improvement across tools.
