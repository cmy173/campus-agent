/** One owned turn. All reasoning/tool-loop/session persistence is the official SDK. */
import { createInterface } from 'node:readline';
import { DeepSeekHarness } from '@deepseek-ai/dsh-sdk-client';
import { prepareProfile } from './prepare-profile.mjs';

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
const input = await new Promise((resolve, reject) => {
  lines.once('line', line => { try { resolve(JSON.parse(line)); } catch { reject(new Error('Invalid runner input')); } });
  lines.once('close', () => reject(new Error('Runner input closed')));
});
const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
const profile = await prepareProfile(input.home);
const harness = new DeepSeekHarness({
  ...profile, provider: 'campus-model', model: 'configured-campus-provider',
  processCwd: process.cwd(), initializeTimeoutMs: 30000, maxTokens: 4096,
  shutdownTimeoutMs: 1000, disposeEofGraceMs: 2000, disposeGraceMs: 1000,
  env: { PATH: process.env.PATH, SystemRoot: process.env.SystemRoot, USERPROFILE: process.env.USERPROFILE,
    TEMP: process.env.TEMP, TMP: process.env.TMP,
    CAMPUS_BRIDGE_URL: process.env.CAMPUS_BRIDGE_URL, CAMPUS_BRIDGE_TOKEN: process.env.CAMPUS_BRIDGE_TOKEN },
});
let cancelled = false;
const cancel = () => { cancelled = true; void harness.close(); };
lines.on('line', line => { try { if (JSON.parse(line).cancel) cancel(); } catch { /* Ignore malformed control frame. */ } });
lines.on('close', cancel);
process.on('SIGTERM', cancel);
try {
  emit({ kind: 'runtime', data: { version: '0.2.0-rc.2', plugins: profile.entries } });
  const result = await harness.run(input.text, {
    sessionId: input.conversation_id,
    onNotification: notification => {
      // Event envelopes are kept out of stdout; the gateway journals authorized
      // domain results itself. This reports progress without copying provider internals.
      emit({ kind: 'runtime_progress', data: { method: notification.method } });
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
