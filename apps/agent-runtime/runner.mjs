/** One owned turn. All reasoning/tool-loop/session persistence is the official SDK. */
import { createInterface } from 'node:readline';
import { DeepSeekHarness } from '@deepseek-ai/dsh-sdk-client';
import { prepareProfile, mcpCredentialEnvironment } from './prepare-profile.mjs';

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
const input = await new Promise((resolve, reject) => {
  lines.once('line', line => { try { resolve(JSON.parse(line)); } catch { reject(new Error('Invalid runner input')); } });
  lines.once('close', () => reject(new Error('Runner input closed')));
});
const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
const profile = await prepareProfile(input.home, { workspace: input.workspace, maxTokens: input.max_tokens,
  searchBaseURL: input.search_base_url, searchModel: input.search_model, skillDirs: input.skill_dirs,
  mcpServers: input.mcp_servers });
const harness = new DeepSeekHarness({
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
    CAMPUS_PYTHON_EXECUTABLE: process.env.CAMPUS_PYTHON_EXECUTABLE,
    CAMPUS_MCP_CREDENTIALS: mcpCredentialEnvironment(input.mcp_servers),
    DEEPSEEK_API_KEY: process.env.DEEPSEEK_API_KEY, DEEPSEEK_SEARCH_API_KEY: process.env.DEEPSEEK_SEARCH_API_KEY },
});
let cancelled = false;
const cancel = () => { cancelled = true; void harness.close(); };
lines.on('line', line => { try { if (JSON.parse(line).cancel) cancel(); } catch { /* Ignore malformed control frame. */ } });
lines.on('close', cancel);
process.on('SIGTERM', cancel);
try {
  emit({ kind: 'runtime', data: { version: '0.2.0-rc.2', bundles: profile.bundles, plugins: profile.entries, provider: input.provider ?? 'campus-model' } });
  const result = await harness.run(input.text, {
    sessionId: input.conversation_id,
    onNotification: notification => {
      if (notification.method === 'session.event') {
        const event = notification.params.event;
        // The official session log is authoritative. Project task progress and
        // tool/assistant output, without copying request bodies or credentials.
        if (event?.type?.startsWith('tool/') || ['assistant/message', 'subagent/catalog', 'subagent/descriptor'].includes(event?.type)) {
          emit({ kind: 'harness_event', data: { session_id: notification.params.sessionId, event } });
        }
      } else if (notification.method.startsWith('subagent.')) {
        emit({ kind: notification.method, data: notification.params });
      } else emit({ kind: 'runtime_progress', data: { method: notification.method } });
    },
  });
  if (!cancelled) emit({ kind: 'result', data: {
    conversation_id: result.sessionId, response: result.finalResponse, event_count: result.events.length,
  } });
} catch {
  if (!cancelled) emit({ kind: 'error', data: { code: 'HARNESS_FAILED', message: 'Harness 本轮未完成，请检查模型或工具错误记录', retryable: true } });
  process.exitCode = cancelled ? 0 : 1;
} finally {
  await harness.close();
  lines.removeAllListeners('close');
  lines.close();
}
