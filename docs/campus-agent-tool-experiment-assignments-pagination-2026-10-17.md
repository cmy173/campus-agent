# BUS-12: assignment pagination through the Harness

## Hypothesis

For a request that requires the full local assignment list, the model should consume the actual `next_cursor` returned by page one and forward it unchanged to page two. The Harness should execute both calls and return both results; stable paging should yield all rows once and stop when the cursor is absent.

This isolates cursor/result plumbing and a scripted model policy. It does not measure real-model tool selection or prove campus-side completeness.

## Setup and safety

- Scenario `BUS-12`: one synthetic course with three synthetic assignment rows; page size is two.
- Real API conversation/turn/poll entry points, real local Harness and `assignments.list` executor, loopback-only scripted provider, temporary SQLite state.
- No personal workspace, credential, browser state, campus service, or internet access is used.
- Maximum 3 model calls and 2 tool calls per run; serial repetitions.

## Results

The initial smoke batch `BUS12-ASSIGNMENT-PAGING-SMOKE-20261017` passed 1/1. The default repeat batch [`BUS12-ASSIGNMENT-PAGING-N3-20261017`](../var/campus-bench/BUS12-ASSIGNMENT-PAGING-N3-20261017/index.html) passed 3/3. Every run shows two `campus_assignments_list` calls and two actual `assignments.list` results; page sizes were 2 then 1. The oracle compared the second call cursor in memory to the first result's `next_cursor`, checked three unique IDs and no final cursor, and confirmed the second page result was in the final provider request. Reports store only counts and booleans, not assignment content or cursor values.

End-to-end elapsed time, including each disposable API setup, was 13.50–13.77 seconds (mean 13.60; median 13.52). Each run used 3 model calls and 2 tool calls. Tool execution took 25.43 ms and 21.51 ms in the smoke run; run-level executor timings varied slightly in the n=3 batch. No campus request or retry occurred.

## Interpretation

The n=3 check proves repeatability of the API/Harness/tool cursor path under this scripted policy and fixture. It does not prove a real model chooses to paginate or that cached rows are school-side complete. No product code change is justified because the tested path behaved as specified.

## Reproduction

```powershell
python tools/campus_bench/runner.py mock-e2e --batch-id BUS12-ASSIGNMENT-PAGING-N3-20261017 --scenario BUS-12 --repeat 3 --timeout 120
```
