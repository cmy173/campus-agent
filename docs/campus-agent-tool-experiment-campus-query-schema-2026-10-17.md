# Campus query/sync tool-description experiment — 2026-10-17

## Experimental question

Does narrowing `campus.query` and `campus.sync` descriptions reduce out-of-scope campus data reads for a public activity request, without hurting completion or efficiency?

## Conditions

Both conditions used BUS-04's identical natural-language prompt, rubric, provider ID/model, runner version, source checkout, seed (`20261017`), serial execution, 120-second per-task timeout, 12-model-call per-task cap, 36-call batch cap, zero retries, and the normal local Agent API/Harness. Each API instance used a new empty SQLite database and isolated Agent home; within each 3-run batch the first repetition began cold and later repetitions reused the public cache. Requests to campus services were read-only and limited to public notices; official event pages were read in the authorized browser. The ordinary API on port 8000 was not used or restarted.

- **Control** batch `20261017-084822-adb5957b`: `baseline-campus-args`, restoring the `campus.query` and `campus.sync` descriptions from Git HEAD.
- **Treatment** batch `20261017-085326-b3e75622`: current descriptions. These explicitly say not to invent query filters for public notices and that campus sync has no `wait_seconds` argument.

Per-tool schema fingerprints prove that `campus.query` and `campus.sync` were each stable within their condition and changed between conditions. No non-target tool schema fingerprint changed when both intended targets are supplied to the comparator (`--target-tool campus.query --target-tool campus.sync`). The full model-visible schema hash varied within each batch because browser tools are exposed in a later tool-loading stage; per-tool fingerprints show this staged discovery did not alter other tool schemas. This is one combined campus-query/sync description treatment, not an isolated test of each description independently. Temporal source data and model sampling remain non-deterministic.

## Results

| Measure | Control (n=3) | Treatment (n=3) | Observed change |
|---|---:|---:|---:|
| Confirmed success | 0/3 | 0/3 | none |
| Partial (after trace-aware reclassification) | 3/3 | 3/3 | no grade change |
| Failed | 0/3 | 0/3 | no confirmed completion in either condition |
| Explicit `needs_user` handoff | 1/3 | 2/3 | treatment had one more manual-action request |
| Out-of-scope private campus capabilities (`query/sync`) | 3/3 runs | 0/3 runs | fewer in this sample |
| Redundant `campus.status` | 1/3 | 1/3 | unchanged |
| Mean model calls | 9.67 | 7.33 | −2.34 |
| Mean tool calls | 13.67 | 9.33 | −4.34 |
| Mean end-to-end time | 87.92 s | 81.63 s | −7.1%; descriptive only |
| P95 end-to-end time | 113.45 s | 120.60 s | +6.3% (worse tail) |
| Observed `WEB_BLOCKED_URL` results | 10 | 7 | still present in both conditions |

The private-capability count was computed before redaction from call arguments and persisted as tool-name-only evidence. Control also called `campus.status` once; treatment also called it once. Thus the observed reduction in non-public `query/sync` calls is 3/3 to 0/3, while any broader “unnecessary account-domain access” measure is 3/3 to 1/3, not zero.

The treatment's second repetition was previously reported as a 120-second timeout. Trace inspection found the `browser_click` result explicitly contained `status=needs_user`; the browser safety policy refused to execute a page control it could not establish was read-only. The model then failed to return a final response, so the old runner waited until its deadline. This is confirmed evidence of a human-action handoff plus a runner observability/waiting defect, not evidence that the click executor hung. A first treatment repetition also requested human action; a third did not. In the control, one repetition requested human action. Other repetitions encountered `WEB_BLOCKED_URL`, separately recorded external-source failures.

The benchmark runner now recognizes this structured handoff, stops waiting, records `needs_user` and an explicit intervention count, and redacts the action identifier and page preview from stored traces. The diagnosis uses a new **K: safety boundary requires human confirmation (not a system failure)** category so these cases are not mislabeled as successful-but-inefficient work or as tool failures. Historical batch reports were rebuilt from the saved events with the same reclassification; the original wall-clock durations remain unchanged and must not be interpreted as the new early-stop latency. Unit coverage checks handoff detection, privacy redaction, scoring, diagnosis and prompt cancellation.

This is an instrumentation correction validated against the captured real Agent events, not a claimed campus Agent completion improvement. The code change is in `tools/campus_bench/runner.py` (SHA-256 `3439874f6ac5c7f5487d36bdc8e6dd87b6583e1c121b3022e711339be4803f93`); the repository HEAD for both original experiment batches remains `650a9348e859ee24544c85fce1a3f397ae3e2345`. The changed runner was exercised by 97 focused tests across the benchmark, Harness, campus read-only boundary and log-redaction suites. A fresh live model repetition was not run for this instrumentation-only fix, so the early-stop wall-clock benefit is not yet measured end to end.

## Interpretation and decision

The description change is **promising for scope control**: the model stopped selecting non-public `campus.query/sync` capabilities in these three treatment runs. It also coincided with fewer model/tool calls, but explicit human-action requests increased from 1/3 to 2/3. Both conditions had zero confirmed completions. The mean time was 7.1% lower while P95 was 6.3% higher. With only three non-deterministic tasks per condition and unpaired live-source/cache state, these are descriptive differences, not causal improvements. No task-completion improvement is established.

Keep the narrowly scoped descriptions for now: they express the actual runtime argument contract and reduce a concrete unnecessary-data risk in the observed sample. Do not roll them out as a proven completion-rate optimization. Next run a counterbalanced 3-pair experiment (alternate old/new conditions, each with a new empty state) and separately measure public-source completion, non-public capability selection, `campus.status`, browser fallback duration, timeout rate, and cache-first/cache-warm outcomes. The current treatment's browser timeout should be investigated as a distinct Harness/browser execution issue before changing query/sync behavior further.

## Reproduce

The control launcher accepts `--condition baseline-campus-args`; the treatment uses `--condition source`. Start each condition with a different empty `--state-dir`, then run:

```powershell
python tools/campus_bench/runner.py run --api http://127.0.0.1:8017 `
  --provider-id LOCAL_PROVIDER_ID --scenario BUS-04 --repeat 3 --concurrency 1 `
  --timeout 120 --max-model-calls-per-run 12 --max-total-model-calls 36 `
  --seed 20261017 --skip-direct-tools --max-retries 0

python tools/campus_bench/runner.py compare `
  --before 20261017-084822-adb5957b --after 20261017-085326-b3e75622 `
  --target-tool campus.query --target-tool campus.sync
```

The comparison command verifies target and non-target per-tool schema fingerprints, and writes `comparison.json` and `comparison.html` in the treatment batch. It does not treat model output or HTTP status as task success.
