/** Owned turns. All reasoning/tool-loop/session persistence is the official SDK. */
import { createInterface } from 'node:readline';
import { DeepSeekHarness } from '@deepseek-ai/dsh-sdk-client';
import { prepareProfile, mcpCredentialEnvironment } from './prepare-profile.mjs';
import { runWithGoalLifecycle } from './goal-lifecycle.mjs';

// One process serves every turn of one conversation. The SDK owns a single runtime
// subprocess per DeepSeekHarness, so building it once and reusing it is the documented
// usage; restarting it per turn would re-compose the whole profile (and every MCP child)
// before the model is ever asked anything. The API replaces this process whenever a
// profile-affecting setting changes, so the profile below stays valid for its lifetime.
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
const pending = [];
let wake = null, closed = false, cancelled = false, turn = 0;
let harness = null, profile = null;

const raw = value => process.stdout.write(JSON.stringify(value) + '\n');
const emit = value => raw({ ...value, turn });

const nextFrame = () => pending.length ? Promise.resolve(pending.shift())
  : closed ? Promise.resolve(null)
    : new Promise(resolve => { wake = resolve; });

lines.on('line', line => {
  let frame;
  try { frame = JSON.parse(line); } catch { return; }
  if (frame && frame.cancel) {
    // The API is ending this turn (or this process); abort the in-flight turn.
    cancelled = true;
    if (harness) void harness.close();
    return;
  }
  if (wake) { const resolve = wake; wake = null; resolve(frame); } else pending.push(frame);
});
lines.on('close', () => {
  closed = true;
  cancelled = true;
  if (wake) { const resolve = wake; wake = null; resolve(null); }
});
process.on('SIGTERM', () => { cancelled = true; if (harness) void harness.close(); });

try {
  for (;;) {
    const input = await nextFrame();
    if (!input) break;
    turn += 1;
    cancelled = false;
    if (harness === null) {
      profile = await prepareProfile(input.home, { workspace: input.workspace, maxTokens: input.max_tokens,
        searchBaseURL: input.search_base_url, searchModel: input.search_model, skillDirs: input.skill_dirs,
        mcpServers: input.mcp_servers });
      harness = new DeepSeekHarness({
        ...profile, provider: input.provider ?? 'campus-model', model: input.model ?? 'configured-campus-provider',
        cwd: input.workspace ?? process.cwd(),
        processCwd: process.cwd(), initializeTimeoutMs: 60000, maxTokens: input.max_tokens ?? 16384,
        shutdownTimeoutMs: 1000, disposeEofGraceMs: 2000, disposeGraceMs: 1000,
        env: { PATH: process.env.PATH, PATHEXT: process.env.PATHEXT, ComSpec: process.env.ComSpec,
          SystemRoot: process.env.SystemRoot, USERPROFILE: process.env.USERPROFILE,
          APPDATA: process.env.APPDATA, LOCALAPPDATA: process.env.LOCALAPPDATA,
          HTTP_PROXY: process.env.HTTP_PROXY, HTTPS_PROXY: process.env.HTTPS_PROXY,
          ALL_PROXY: process.env.ALL_PROXY, NO_PROXY: process.env.NO_PROXY,
          http_proxy: process.env.http_proxy, https_proxy: process.env.https_proxy,
          all_proxy: process.env.all_proxy, no_proxy: process.env.no_proxy,
          TEMP: process.env.TEMP, TMP: process.env.TMP,
          CAMPUS_BRIDGE_URL: process.env.CAMPUS_BRIDGE_URL, CAMPUS_BRIDGE_TOKEN: process.env.CAMPUS_BRIDGE_TOKEN,
          CAMPUS_CONVERSATION_ID: process.env.CAMPUS_CONVERSATION_ID,
          CAMPUS_GOAL_RESUME: process.env.CAMPUS_GOAL_RESUME,
          CAMPUS_PYTHON_EXECUTABLE: process.env.CAMPUS_PYTHON_EXECUTABLE,
          CAMPUS_MCP_CREDENTIALS: mcpCredentialEnvironment(input.mcp_servers),
          DEEPSEEK_API_KEY: process.env.DEEPSEEK_API_KEY, DEEPSEEK_SEARCH_API_KEY: process.env.DEEPSEEK_SEARCH_API_KEY },
      });
    }
    try {
      emit({ kind: 'runtime', data: { version: '0.2.0-rc.2', bundles: profile.bundles, plugins: profile.entries, provider: input.provider ?? 'campus-model' } });
      const result = await runWithGoalLifecycle(harness, input.text, {
        sessionId: input.conversation_id,
        isCancelled: () => cancelled,
        readGoal: async () => {
          const response = await fetch(process.env.CAMPUS_BRIDGE_URL + '/api/v1/agent/internal/goal-state', {
            method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + process.env.CAMPUS_BRIDGE_TOKEN },
            body: JSON.stringify({ conversation_id: input.conversation_id }), signal: AbortSignal.timeout(10000),
          });
          if (!response.ok) throw new Error('Goal state unavailable');
          return response.json();
        },
        onGoalState: data => emit({ kind: 'goal.state', data }),
        onNotification: notification => {
          if (notification.method === 'session.event') {
            const event = notification.params.event;
            // The official session log is authoritative. Project task progress and
            // tool/assistant output, without copying request bodies or credentials.
            if (event?.type?.startsWith('tool/') || ['assistant/message', 'subagent/catalog', 'subagent/descriptor', 'goal/change'].includes(event?.type)) {
              emit({ kind: 'harness_event', data: { session_id: notification.params.sessionId, event } });
            }
          } else if (notification.method.startsWith('subagent.')) {
            emit({ kind: notification.method, data: notification.params });
          } else emit({ kind: 'runtime_progress', data: { method: notification.method } });
        },
      });
      if (!cancelled) emit({ kind: 'result', data: {
        conversation_id: result.sessionId, response: result.finalResponse, event_count: result.eventCount,
      } });
    } catch {
      if (!cancelled) emit({ kind: 'error', data: { code: 'HARNESS_FAILED', message: 'Harness 本轮未完成，请检查模型或工具错误记录', retryable: true } });
      // A failed turn may have left the harness in an unknown state; end the process so
      // the API starts a clean one instead of reusing this one.
      break;
    }
  }
} finally {
  if (harness !== null) await harness.close();
  lines.close();
}
