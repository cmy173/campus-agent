/** Actual installed Harness loop + controlled bridge/model; no external calls. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DeepSeekHarness } from '@deepseek-ai/dsh-sdk-client';
import { prepareProfile } from './prepare-profile.mjs';
import { runWithGoalLifecycle } from './goal-lifecycle.mjs';

const runtime = fileURLToPath(new URL('.', import.meta.url));
const directory = await mkdtemp(resolve(runtime, '../../.test-tmp/transition-recovery-'));
const workspace = resolve(directory, 'workspace'); await mkdir(workspace);
await writeFile(resolve(workspace, 'evidence.txt'), 'synthetic-recovery-marker-748');
let mode = 'transient';
const requests = [], events = [], results = [];
const server = createServer(async (req, res) => {
  const chunks = []; for await (const chunk of req) chunks.push(chunk);
  const body = JSON.parse(Buffer.concat(chunks).toString() || '{}');
  res.setHeader('Content-Type', 'application/json');
  if (req.url.endsWith('/schemas')) return res.end('[]');
  if (!req.url.endsWith('/internal/model')) return res.end('{"ok":true}');
  const count = requests.filter(x => x.mode === mode).length;
  requests.push({ mode, count, message_roles: body.messages.map(m => m.role),
    tools: body.tools.map(t => t.name) });
  if (mode === 'auth' || count === 0) {
    res.statusCode = 502;
    return res.end(JSON.stringify({ error: { code: mode === 'auth' ? 'PROVIDER_AUTH_FAILED' : 'PROVIDER_RATE_LIMITED',
      retryable: mode !== 'auth', message: 'synthetic-private-provider-diagnostic' } }));
  }
  const read = body.messages.findLast(m => m.role === 'tool');
  if (!read) return res.end(JSON.stringify({ tool_calls: [{ id: 'read-proof', name: 'read',
    arguments: { file_path: 'evidence.txt' } }] }));
  assert(JSON.stringify(read).includes('synthetic-recovery-marker-748'));
  res.end(JSON.stringify({ content: '已读取 synthetic-recovery-marker-748，完成恢复。', tool_calls: [] }));
});
await new Promise(r => server.listen(0, '127.0.0.1', r));
const url = `http://127.0.0.1:${server.address().port}`;
const profile = await prepareProfile(resolve(directory, 'home'), { workspace });
const harness = new DeepSeekHarness({ ...profile, provider: 'campus-model', model: 'controlled-fixture',
  cwd: workspace, processCwd: runtime, initializeTimeoutMs: 60000, shutdownTimeoutMs: 1000,
  env: { PATH: process.env.PATH, PATHEXT: process.env.PATHEXT, SystemRoot: process.env.SystemRoot,
    ComSpec: process.env.ComSpec, USERPROFILE: process.env.USERPROFILE, TEMP: directory, TMP: directory,
    CAMPUS_BRIDGE_URL: url, CAMPUS_BRIDGE_TOKEN: 'synthetic-private-token' } });
try {
  const result = await runWithGoalLifecycle(harness, 'Read evidence.txt and quote its marker.', {
    sessionId: 'transient', readGoal: async () => ({ goal: null, agent_status: 'idle' }),
    onNotification: e => events.push(e),
  });
  assert.match(result.finalResponse, /synthetic-recovery-marker-748/);
  assert.equal(requests.filter(x => x.mode === 'transient').length, 3);
  results.push({ case: 'rate-limit-then-read-then-answer', ok: true, model_requests: 3,
    tools_execute_once: true, result_consumed: true });
  mode = 'auth';
  await assert.rejects(runWithGoalLifecycle(harness, 'Read evidence.txt.', {
    sessionId: 'auth', readGoal: async () => ({ goal: null, agent_status: 'idle' }), onNotification: e => events.push(e),
  }));
  assert.equal(requests.filter(x => x.mode === 'auth').length, 1, 'auth must not be automatically retried');
  assert(!JSON.stringify(events).includes('synthetic-private-provider-diagnostic'));
  assert.equal(events.filter(e => e.params?.event?.type === 'tool/call'
    && e.params.event.data.name === 'read').length, 1);
  assert.equal(events.filter(e => e.params?.event?.type === 'llm/retry').length, 1);
  results.push({ case: 'auth-failure-no-retry', ok: true, model_requests: 1 });
  const report = { mode: 'real-installed-harness/controlled-model-and-bridge', results, requests,
    event_count: events.length, llm_retries: events.filter(e => e.params?.event?.type === 'llm/retry').length };
  await writeFile(resolve(runtime, '../../docs/audit/transition-harness-recovery.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally {
  await harness.close(); await new Promise(r => server.close(r));
}
