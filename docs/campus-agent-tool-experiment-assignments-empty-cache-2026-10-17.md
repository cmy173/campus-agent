# ERR-11: empty assignment cache semantics

## Question and hypothesis

When the authorized local course scope has no assignment rows, `assignments.list` should expose the empty cache as incomplete/stale evidence. The model must receive that result, avoid claiming that the school has no assignments, and avoid refreshing the campus connection unless the user requests it.

This is a contract and Harness verification, not a product-quality or real-model experiment. No production Agent/Harness code was changed in this iteration.

## Execution

- Scenario: `ERR-11`, isolated synthetic SQLite with one authorized synthetic course and zero assignment rows.
- Path: normal Agent conversation/turn/poll API → local Harness → registered `assignments.list` executor → loopback scripted OpenAI-compatible provider.
- Repeats: 3, serial; per-turn timeout 120 seconds; provider/model calls bounded to 6 (observed 2 per run); tool calls bounded by the fixture to 1.
- Campus account, saved credentials, browser profile, remote campus services, and public internet were not used.
- Batch: [`ERR11-EMPTY-ASSIGNMENT-VERIFIED-20261017`](../var/campus-bench/ERR11-EMPTY-ASSIGNMENT-VERIFIED-20261017/index.html).

## Results

All 3/3 repetitions passed the deterministic fixture oracle. Each run used exactly `assignments.list(order_by=due_soon, limit=10)`, returned zero items and no cursor, status `partial`, `freshness.stale=true`, `coverage.complete=false`, and one coverage gap. The structured result was present in the next provider request before the final response. Each final answer distinguished the local cache from the school-side state and did not trigger sync.

| Metric | Result |
|---|---:|
| End-to-end elapsed (including disposable API setup) | 13.28–13.94 s; mean 13.56 s; median 13.46 s |
| Model calls | 2 per run |
| Tool calls | 1 per run |
| Tool executor duration | 18.69–20.87 ms |
| Provider received tool result before final | 3/3 |
| Confirmed success | 3/3 mock E2E |

The first fixture draft failed before any useful conclusion because its oracle expected `ok` and looked for tool arguments at the wrong event envelope level. Inspection of the actual Harness trace showed the intended executor behavior was `partial` with one coverage gap; the argument event carries values under the nested `tool/call` event. The fixture/evaluator was corrected and the final batch above is the valid repeat. Earlier failed batches remain on disk and are not counted as success.

## Interpretation and next experiment

The current executor already communicates empty/possibly stale cache limits correctly; this test found no production defect to fix. It proves the local Harness passes this tool result to a scripted model path and the scripted path produces a boundary-aware answer. It does **not** prove a real model will choose this tool, use the cache boundary correctly, or that campus-side data is absent/current.

The next useful assignment-family experiment is a paginated Harness continuation: seed more rows than one page, verify the exact continuation cursor is returned to the model and consumed once, and confirm no duplicate/missing rows. Direct unit coverage already checks stable due-date order and cursor continuation; a real-model paginated path remains unverified. The relevant direct tests pass in `apps/api/tests/test_agent_tools.py -k assignment` (8 passed).

## Verification

- `python -m pytest -q tools/campus_bench/test_runner.py tools/campus_bench/test_build_tool_map.py`: 61 passed.
- `apps/api/.venv/Scripts/python.exe -m pytest -q tests/test_agent_tools.py -k assignment`: 8 passed.
- Mock results remain separate from real-model campus success rates.
