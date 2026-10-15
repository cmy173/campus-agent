# Agent execution and artifact delivery acceptance

Updated 2026-10-15. This is the publication summary for the V2 integration PR. Raw model traces,
personal account data, screenshots, browser profiles and local audit workspaces are not part of the
submission. Synthetic fixtures and runnable verification tools are included.

## Result and scope

The Harness/document-export/review-paper task passed independent acceptance. The work keeps the
existing FastAPI, SQLite, connector, learning-engine and React boundaries. It fixes the complete
model → tool → business service → worker → result → final-answer path without replacing the Harness.

- Domain tools enforce server-created conversation scopes. Imported course grants, jobs, cached data,
  files and papers cannot be read merely by supplying another conversation's IDs.
- Model failures preserve actionable errors. Temporary provider failures have bounded retry; invalid
  arguments return sanitized repair hints. Tool leases and browser operations drain during stopping.
- `artifacts.export` accepts bounded structured data and runs one fixed renderer in a terminable,
  reaped child process. The model cannot choose code, executable, output directory or arbitrary URLs.
  It publishes real PDF/XLSX/Markdown files, verifies disk content and records replay identities.
- Interrupted batch publishing distinguishes usable retained files, pending cleanup and uncertainty.
  An index row alone is not proof of delivery. Download availability validates ownership, safe paths,
  links/reparse points and real content. Clean rollback is safely retryable under the same call ID.
- `reviews.preflight/generate/read/export` reuse the existing review engine and persistent worker.
  Frozen material fingerprints prevent stale previews from silently changing scope. Identical
  in-flight business requests reuse the original job; an explicit new generation remains possible.
  `jobs.read(wait_seconds=14)` provides bounded observation without enlarging model budgets.
- Call-cache identity excludes observation arguments only for declared tools; lookup and storage use
  the same normalization. Real business-argument changes still produce an idempotency conflict.
- Campus sessions, the optional OS-encrypted credential vault, legal WebVPN transport and authentication
  recovery stay separated from public-network requests. Browser-owned cookies need explicit adoption.
  Browser disconnect, clearing cached data and clearing credentials retain distinct meanings.
- The chat UI supports cursor-based history, account/authentication recovery, safe file previews,
  downloadable artifacts, bounded child-task control and desktop/mobile layouts.

## Independent verification history

| Stage | Executed result | Scope |
|---|---|---|
| Initial architecture audit | API 616 passed / 5 skipped; root/connectors 594 passed + 87 subtests; runtime Node 85 passed | Synthetic transport and controlled model, with public-network observations recorded separately |
| First export/review delivery | API 660 passed / 5 skipped; additional independent boundary probes failed | Found unbounded render threads, publication/retry errors, dropped paper ID and incomplete real-model scoring |
| Second revision | API 696 passed / 5 skipped; frontend 193 passed; 12 deterministic classes passed | Real API, installed Harness and independent worker; subsequent probe found a stale-index delivery claim |
| Final scoped acceptance | 7 previous independent probes passed; 2 additional controls passed; affected API subset 164 passed / 2 skipped; A03/A05/E01/E02 passed | Last stale-index fault closed; source hashes stayed fixed during verification |

The final independent review was deliberately scoped to file/error-state changes; it did not repeat
the whole paid model experiment. The implementation separately reported 698 API tests passing with
five host-limited skips before the two independent controls were retained in the normal API suite.
The final PR snapshot's own results are recorded below after testing.

Real-provider experiments on the preceding reviewed source stage used the saved `deepseek-flash`
configuration: three tasks × three independent sessions, all nine achieved their complete goals.
An independent one-per-task sample also achieved all three goals. These sets are separate; they are
not combined into a 12/12 trial or a production reliability estimate. The tasks used synthetic campus
materials and real provider/Harness/worker execution. Limits stayed at 12 model requests, 40 tool
calls and 150 seconds per run. Worker token totals and actual monetary cost remain unknown.

## Reproduction

Run from the repository root after `python tools/dev.py --setup`:

```powershell
apps/api/.venv/Scripts/python.exe -X utf8 tools/audit_regression.py apps/api/tests -q -p no:cacheprovider
apps/api/.venv/Scripts/python.exe -X utf8 tools/audit_regression.py tests connectors/tests -q -p no:cacheprovider
apps/api/.venv/Scripts/python.exe -X utf8 tools/audit_regression.py tools/test_launcher.py -q -p no:cacheprovider
apps/api/.venv/Scripts/python.exe -X utf8 tools/export_agent_contract.py --check
apps/api/.venv/Scripts/python.exe -X utf8 tools/verify_agent_runtime.py
node apps/agent-runtime/verify-transition-recovery.mjs
node apps/web/node_modules/typescript/bin/tsc -b apps/web
# In apps/agent-runtime: npm run test:audit
# In apps/web: npm run build; npm test -- --maxWorkers=2
```

`tools/verify_next_round_e2e.py` drives twelve deterministic acceptance classes through the real API
and Harness, with a local controlled provider. It never needs school credentials or paid model calls.
`tools/verify_next_round_live.py --live` is an optional paid diagnostic; it requires a saved provider
configuration, uses isolated synthetic domains and records budgets, usage and complete delivery.
Do not run it in CI, substitute fake success for unavailable credentials, or publish raw traces.

## Limits

- This PR is a V2 integration change; the Harness review is not a blanket acceptance of every campus
  endpoint. No new private-campus-account/WebVPN/学在浙大 acceptance is claimed by the final Harness review.
- Captcha, MFA and missing authorization remain explicit user interactions. The model cannot provide
  account ownership, trusted site lists or arbitrary URLs to bypass those boundaries.
- Direct networking still follows OS routes and does not promise to bypass TUN. Browser WebVPN login
  is not automatically a backend authenticated session. Public reachability is not login status.
- Failed cleanup can leave local files or metadata pending repair. It is disclosed with a structured
  error, not advertised as a completed artifact or blindly retried.
- Existing Windows symlink-creation skips must be reported. They are host limitations, not new passes.
- CI and review apply to the submitted Git commit. Local acceptance does not automatically authorize
  merging, deployment, external submissions or publishing private runtime material.

## Submission snapshot checks

The submission was exported from the Git index into a separate source directory, with no raw audit
logs, personal database, browser profile or untracked implementation files. Existing installed Python
and Node dependencies were reused; this is a source-isolation check, not a fresh dependency install.

| Final snapshot check | Result |
|---|---|
| API | **700 passed / 5 existing host-limited skips**, 262.57 seconds |
| Root/connectors plus launcher | **606 passed + 87 subtests passed**, 51.40 seconds |
| Frontend | **193 passed / 34 files**, 137.81 seconds |
| Frontend production build / TypeScript | Passed |
| Runtime boundary tests | **85 passed** |
| Installed Harness / actual API integration | **ok, 83 controlled model calls** |
| Native provider recovery | Rate-limit retry followed by one consumed read; authentication failure not retried |
| Agent contract export | Current: **51 paths / 54 schemas** |
| Staged whitespace / publication scan | Passed; local stored credential values and token patterns absent from selected files |

The first snapshot API run had 699 passes, five skips and one environment-integrity failure: the
reused editable `learning` installation still pointed to the original checkout. The snapshot's
`packages/learning/src` was then explicitly put on that test process's Python path; the entire API
suite above was rerun and passed without changing assertions. The first result was retained locally.

CI installs API dependencies from `apps/api`, where the editable learning-package path in
`requirements.txt` resolves correctly. A no-install dependency-resolution check passed after this
working-directory correction. The connector test directory and runtime recovery tests are included
in CI. Actual GitHub CI results still apply to the pushed commit and are separate from these local
snapshot results. Only documentation and verification/CI setup changed after the source snapshot was tested.

The first remote CI run exposed a clean-runner prerequisite in runtime verification: its temporary
parent directory did not yet exist. The recovery and subagent verification scripts now create their
own parent/output directories, and are rechecked in an exported checkout without runtime data.
The older child-cancellation scenario also used a now-denied PowerShell sleep. It now exercises two
real child agents reading distinct fixture files and waiting on controlled provider responses; it
still verifies unauthenticated cancellation is denied, only the chosen child aborts, and its sibling
and parent complete. It does not enable shell access to make a verification pass.
