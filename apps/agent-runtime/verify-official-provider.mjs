/** Native DeepSeek Messages/SSE protocol with a local endpoint; no paid requests. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DeepSeekHarness } from '@deepseek-ai/dsh-sdk-client';
import { prepareProfile } from './prepare-profile.mjs';

const directory = await mkdtemp(fileURLToPath(new URL('../../var/v2-official-protocol-', import.meta.url)));
const requests = [], failures = [], results = [];
const key = 'fixture-native-credential-926';
let mode = 'success';
const server = createServer(async (req, res) => {
  const chunks = []; for await (const chunk of req) chunks.push(chunk);
  const body = JSON.parse(Buffer.concat(chunks).toString() || '{}');
  if (req.url.endsWith('/schemas')) return res.end('[]');
  if (req.url.endsWith('/model-error')) { failures.push(body); return res.end('{"ok":true}'); }
  if (req.url.endsWith('/model-lease') || req.url.endsWith('/tool-lease')) {
    requests.push({ path: req.url, body }); return res.end('{"ok":true}');
  }
  if (!req.url.endsWith('/v1/messages')) {
    res.statusCode = 500; return res.end('{"error":{"message":"Python model relay must not be used"}}');
  }
  requests.push({ path: req.url, body, authenticated: req.headers['x-api-key'] === key });
  if (mode === 'auth_error') {
    res.statusCode = 401; res.setHeader('Content-Type', 'application/json');
    // Raw provider error intentionally contains fixture credential; journal must redact it.
    return res.end(JSON.stringify({ error: { type: 'authentication_error', message: 'Refused ' + key } }));
  }
  const toolResults = body.messages.flatMap(m => Array.isArray(m.content) ? m.content.filter(b => b.type === 'tool_result') : []);
  const index = toolResults.length;
  const tool = index === 0 ? { name: 'write', args: { file_path: 'native-proof.md', content: 'Actual native provider tool marker 926\n' } }
    : index === 1 ? { name: 'read', args: { file_path: 'native-proof.md' } } : null;
  res.setHeader('Content-Type', 'text/event-stream');
  const emit = (type, data) => res.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`);
  emit('message_start', { message: { id: 'msg_' + requests.length, type: 'message', role: 'assistant', model: body.model,
    content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 100, output_tokens: 0 } } });
  if (tool) {
    emit('content_block_start', { index: 0, content_block: { type: 'tool_use', id: 'native_call_' + requests.length, name: tool.name, input: {} } });
    emit('content_block_delta', { index: 0, delta: { type: 'input_json_delta', partial_json: JSON.stringify(tool.args) } });
  } else {
    emit('content_block_start', { index: 0, content_block: { type: 'text', text: '' } });
    emit('content_block_delta', { index: 0, delta: { type: 'text_delta', text: toolResults.map(r => JSON.stringify(r.content)).join('\n') } });
  }
  emit('content_block_stop', { index: 0 });
  emit('message_delta', { delta: { stop_reason: tool ? 'tool_use' : 'end_turn', stop_sequence: null }, usage: { output_tokens: 20 } });
  emit('message_stop', {}); res.end();
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const url = `http://127.0.0.1:${server.address().port}`;
const profile = await prepareProfile(resolve(directory, 'home'), { workspace: directory, deepseekBaseURL: url, reasoningEffort: 'off' });
const harness = new DeepSeekHarness({ ...profile, provider: 'deepseek-official', model: 'deepseek-v4-flash', maxTokens: 16384,
  cwd: directory, processCwd: fileURLToPath(new URL('.', import.meta.url)), initializeTimeoutMs: 60000,
  env: { PATH: process.env.PATH, PATHEXT: process.env.PATHEXT, SystemRoot: process.env.SystemRoot,
    ComSpec: process.env.ComSpec, USERPROFILE: process.env.USERPROFILE, TEMP: directory, TMP: directory,
    DEEPSEEK_API_KEY: key, CAMPUS_BRIDGE_URL: url, CAMPUS_BRIDGE_TOKEN: 'fixture-only' } });
try {
  const result = await harness.run('Read and write the requested proof file with actual tools.', { sessionId: 'native_protocol' });
  results.push(result);
  assert.match(result.finalResponse, /Actual native provider tool marker 926/);
  assert.equal(await readFile(resolve(directory, 'native-proof.md'), 'utf-8'), 'Actual native provider tool marker 926\n');
  const modelRequests = requests.filter(r => r.path.endsWith('/v1/messages'));
  assert.equal(modelRequests.length, 3);
  assert(modelRequests.every(r => r.authenticated && r.body.stream && r.body.max_tokens === 16384));
  assert.equal(requests.filter(r => r.path.endsWith('/model-lease')).length, 3);
  assert.equal(requests.filter(r => r.path.endsWith('/tool-lease')).length, 2);
  assert(!requests.some(r => r.path.endsWith('/internal/model')));
  mode = 'auth_error';
  let rejected = false;
  try {
    const failed = await harness.run('Read one file.', { sessionId: 'native_auth_error' });
    rejected = !failed.finalResponse;
  } catch { rejected = true; }
  assert(rejected, 'Authentication failure was treated as a successful answer');
  assert(failures.some(f => f.code === 'PROVIDER_AUTH_FAILED' && f.retryable === false));
  assert(!JSON.stringify(failures).includes(key), 'Raw provider credential leaked into journal');
  for (const item of await readdir(resolve(directory, 'home/sessions'), { recursive: true })) {
    if (item.endsWith('.jsonl')) assert(!(await readFile(resolve(directory, 'home/sessions', item), 'utf-8')).includes(key), 'Raw credential leaked into upstream session log');
  }
  await writeFile(resolve(directory, 'evidence.json'), JSON.stringify({ kind: 'actual_upstream_native_messages_sse_controlled_endpoint',
    requests, failures, successful_events: result.events, checks: { actual_file_tool_loop: true, no_python_model_relay: true,
      native_authentication: true, model_and_tool_leases: true, error_journaling_redacted: true } }, null, 2));
  console.log(JSON.stringify({ ok: true, evidence: resolve(directory, 'evidence.json') }));
} finally {
  await writeFile(resolve(directory, 'diagnostic.json'), JSON.stringify({ requests, failures, results }, null, 2));
  await harness.close(); await new Promise(done => server.close(done));
}
