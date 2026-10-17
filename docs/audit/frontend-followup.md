# Frontend follow-up audit

Date: 2026-10-15 (Asia/Shanghai). Scope: apps/web. Existing uncommitted AppLayout/providers/AgentPage/TurnView/theme/global CSS changes were preserved; no reset, new framework or dependency update.

## Findings and implemented changes

- Starting UI was a toolbar plus history Select, not a sidebar. ConversationSidebar adds persisted desktop collapse (optional storage fallback), current item, new conversation, account/settings actions. Existing task/course/calendar/review/files/browser workspaces remain.
- Mobile navigation uses the existing Ant Design Drawer independently of desktop collapse preference. History selection and Escape close it; media-query change closes stale drawers.
- History uses useInfiniteQuery, opaque cursors, search-specific query keys, 200ms debounced remote question/response search, ID dedup and explicit load-more/loading/empty/error states. Lead implemented owner-isolated paginated API and first-question title; no client-side full-transcript scan.
- New conversation is navigation, not an implicit create/send request. Per-conversation drafts restore when selecting history. Account/settings directly select existing URL tabs without losing draft. Per-conversation model consent was retained, not replaced with blanket local-data external-send authorization.
- Workbench uses viewport-sized conversation, independently scrolling transcript and separate composer slot, safe-area padding and reduced motion. Input describes contextual state; Enter does not send while composing IME, a turn is active or upload is running. CSS is not proof of actual soft-keyboard behavior.
- Tool inputs/results and source details remain collapsed by default. Technical record IDs are opt-in. Tool summary reports observed completed/failed/stopped operations, not a fabricated goal completion percentage. Goal round limits remain unfinished.
- AgentErrorNotice gives recovery language, collapsed raw diagnostics/request IDs and literal text instead of HTML. Failed conversation loading no longer shows the new-chat welcome simultaneously. Invalid provider URL does not crash consent screen.
- Send recovery retains exact pending payload/key on network errors, malformed success JSON, 5xx and 408. Only another definitive 4xx rejection clears the key but keeps editable draft. Reload/exact retry reuses the key; successful send refreshes history title/order.
- Real-mode account tab now includes CampusBrowserSessionCard: explicit reuse after official manual browser login/MFA, successful backend identity verification before connected state, no password inputs/cookie values/WebVPN jar handoff. Failed adoption retains previous status. Revocation requires confirmation, keeps cache and explicitly does not log out the independent persistent browser profile. Consent must be renewed after service restart. Lead owns matching private bridge/endpoints; frontend tests are mocks.
- MCP settings disclose that enabling/testing trusted servers may run local commands/access remote services and grant tool authority. This is disclosure, not proof all shell/MCP operations are sandboxed.
- Event dedup changed from quadratic findIndex to Map(sequence); replay/empty pages return previous array identity, new pages are ordered. Dedup O(previous+incoming), new-result sorting O(n log n). Conversation polling still fetches full turn data.
- BrowserWorkspace has Chinese stop-vs-logout guidance: stopping closes browser but retains persistent login; official website logout is required. Query revocation is separate.

## Actual verification

Commands run from D:\AI\campus-agent\apps\web with installed dependencies:

| Command | Actual outcome |
| --- | --- |
| npm test -- --maxWorkers=2 | Startup blocked by Vite bundled-config Windows net use spawn EPERM; no tests executed. |
| npm run build | TypeScript completed; bundled Vite config blocked by same spawn EPERM. |
| npm test -- --configLoader native --maxWorkers=2 | Config loaded; default fork workers blocked by spawn EPERM; not counted as passing. |
| npm test -- --configLoader native --pool threads --maxWorkers=2 | 30 files / 164 tests passed, 139.54s. Includes sidebar/mobile, remote history pagination/search/dedup, drafts/new chat, account/settings, uncertain-send, collapsed tools and campus authorization mocks. This full run PRECEDED merge helper follow-up. |
| Follow-up: npm test -- --configLoader native --pool threads --maxWorkers=2 src/features/agent/mergeTurnEvents.test.ts src/features/agent/TurnView.test.tsx src/features/agent/BrowserWorkspace.test.tsx | 3 files / 6 tests passed, 8.97s; 2 new merge cases plus existing stop/goal/browser regressions. No claim that all 166 tests were rerun together. |
| Final Chinese browser logout-guidance test: npm test -- --configLoader native --pool threads --maxWorkers=2 src/features/agent/BrowserWorkspace.test.tsx | 1 file / 1 test passed, 6.86s. Added assertion for the shared desktop/mobile Chinese guidance. |
| npm run build -- --configLoader native | Exit 0: TypeScript + production bundle. Measured Vite 1.07s in one run; merge and final Chinese/CSS follow-ups also exit 0. |
| npm run lint | Exit 0, 25 warnings, 0 errors; existing React hook/ref/fast-refresh warnings remain, not a warning-free claim. |
| git diff --check -- apps/web | Passed after trimming accidental EOF blank lines; LF/CRLF notices are nonfunctional. |

Successful alternatives use normal native config loading and worker threads, not patched node_modules or disabled tests/TypeScript. Default production scripts remain unchanged. UI-vendor is approximately 1115kB / 354kB gzip and triggers existing >500kB warning; AgentPage about 66kB / 22kB gzip. Do not blindly split initialization-sensitive Ant Design cycles: current config documents prior runtime failure from splitting.

## Limitations and remaining work

1. NO real browser: jsdom checks DOM, interaction and roles, not pixels, actual focus trapping/layout, soft keyboard/safe area/scroll geometry, real school login/MFA, network end-to-end or external submission receipts. Ant Design emits jsdom pseudo-element getComputedStyle notices. No screenshots or complete visual acceptance claimed.
2. Browser-profile clear API is not implemented: shared profile, active turn and action ledger need atomic design/testing. Stop is not logout; use official logout. Campus-query revocation is independent.
3. Dedicated campus.query record / campus.academics schedule cards remain a P1 gap in ResultView. Raw tool details remain expandable, but no complete campus agenda/source-card implementation is claimed. Missing fields must not imply no class, live completeness, GPA or parsed attachment contents.
4. No persistent cross-conversation model scope grant/revoke, transcript virtualization or fully incremental conversation polling. Per-conversation consent intentionally retained. Long-history performance needs real-browser measurements.
5. Sidebar preference/search are not authorization; owner boundaries are server-side. Campus-query and legacy sync-connector status are separate. No real-campus session renewal acceptance claimed.
6. Next browser matrix: expanded/collapsed desktop rail; 320/375px mobile; soft keyboard; long prompts; denied storage; slow/offline history; restored scroll; simultaneous progress; MFA reconnect; successful/cancelled/uncertain external submission receipts.
## Lead final complete rerun

After all merge and Chinese guidance changes, Lead ran `node node_modules/vitest/vitest.mjs run --configLoader native --pool threads --maxWorkers=2` in apps/web: **31 files / 166 tests passed, 127.04s**, log `.test-tmp/followup-web-final.log`. Lead also reran build (exit0, Vite1.19s) and lint (exit0, 25warnings/0errors). This supersedes the earlier 164-test full-run scope limitation, not the no-real-browser limitation.
