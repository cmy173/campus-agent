/** Actual local MCP service, discovery, calls, failures and reconnect; controlled model only. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DeepSeekHarness } from '@deepseek-ai/dsh-sdk-client';
import { prepareProfile, mcpCredentialEnvironment } from './prepare-profile.mjs';

const root = fileURLToPath(new URL('../../', import.meta.url));
const directory = await mkdtemp(resolve(root, 'var/v2-mcp-'));
const audit = resolve(directory, 'server.jsonl');
const requests = [], results = [];
const messageText = message => (message?.content ?? []).filter(b => b.type === 'text').map(b => b.text).join('\n');
const server = createServer(async (req, res) => {
  const chunks = []; for await (const chunk of req) chunks.push(chunk);
  const body = JSON.parse(Buffer.concat(chunks).toString() || '{}');
  if (req.url.endsWith('/schemas')) return res.end('[]');
  requests.push(body);
  const user = [...body.messages].reverse().find(m => m.role === 'user' && messageText(m).includes('MCP_CASE_'));
  const name = messageText(user);
  const responses = body.messages.slice(body.messages.lastIndexOf(user) + 1).filter(m => m.role === 'tool');
  res.setHeader('Content-Type', 'application/json');
  if (!responses.length) {
    if (!body.tools.some(t => t.name === 'mcp__calculator__multiply')) {
      res.statusCode = 500;
      return res.end(JSON.stringify({ error: { message: 'Actual MCP discovery did not register the tool' } }));
    }
    const args = { a: 36, b: 7, ...(name.includes('ERROR') ? { report_error: true } : {}),
      ...(name.includes('DROP') ? { disconnect_once: true } : {}) };
    res.end(JSON.stringify({ content: '', tool_calls: [{ id: 'mcp_' + requests.length, name: 'mcp__calculator__multiply', arguments: args }] }));
  } else res.end(JSON.stringify({ content: responses.map(messageText).join('\n'), tool_calls: [] }));
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const mcpServers = [{
  serverName: 'calculator', transport: 'stdio', command: resolve(root, 'apps/api/.venv/Scripts/python.exe'),
  args: ['-u', resolve(root, 'tools/fixtures/mcp_calculator.py'), audit, '--require-credential'], cwd: directory,
  env: { MCP_FIXTURE_CREDENTIAL: 'fixture-private-581' },
  failOnStartupError: true, toolCallTimeoutMs: 3000,
  reconnect: { enabled: true, initialDelayMs: 100, maxDelayMs: 500, maxAttempts: 5 },
}];
const profile = await prepareProfile(resolve(directory, 'home'), { workspace: directory, mcpServers });
assert(!(await readFile(resolve(directory, 'home/profiles/campus/cordis.patch.yml'), 'utf-8')).includes('fixture-private-581'));
const harness = new DeepSeekHarness({ ...profile, provider: 'campus-model', model: 'controlled-mcp',
  cwd: directory, processCwd: fileURLToPath(new URL('.', import.meta.url)), initializeTimeoutMs: 60000,
  env: { PATH: process.env.PATH, PATHEXT: process.env.PATHEXT, SystemRoot: process.env.SystemRoot,
    ComSpec: process.env.ComSpec, USERPROFILE: process.env.USERPROFILE, TEMP: process.env.TEMP, TMP: process.env.TMP,
    CAMPUS_MCP_CREDENTIALS: mcpCredentialEnvironment(mcpServers),
    CAMPUS_BRIDGE_URL: `http://127.0.0.1:${server.address().port}`, CAMPUS_BRIDGE_TOKEN: 'fixture-only' } });
try {
  for (const name of ['OK', 'ERROR', 'DROP', 'RECOVER']) {
    // Let the real reconnect supervisor observe the process exit and negotiate.
    if (name === 'RECOVER') await new Promise(done => setTimeout(done, 1500));
    const result = await harness.run('MCP_CASE_' + name + ' Use the calculator service.', { sessionId: 'mcp_' + name.toLowerCase() });
    results.push({ name, response: result.finalResponse, events: result.events });
    const toolResult = result.events.find(e => e.type === 'tool/result');
    assert(toolResult, 'No actual MCP result');
    if (['OK', 'RECOVER'].includes(name)) {
      assert.match(result.finalResponse, /MCP computed: 252/);
      assert.equal(toolResult.data.message.isError, false);
    } else assert.equal(toolResult.data.message.isError, true, 'MCP failure must not appear as success');
  }
  const calls = (await readFile(audit, 'utf-8')).trim().split('\n').map(JSON.parse);
  assert(!JSON.stringify({ results, requests, calls }).includes('fixture-private-581'), 'Credential escaped into model/evidence');
  assert(calls.some(c => c.method === 'tools/list'));
  assert.equal(calls.filter(c => c.method === 'tools/call').length, 4);
  assert(new Set(calls.map(c => c.pid)).size >= 2, 'Disconnected service was not restarted');
  await writeFile(resolve(directory, 'evidence.json'), JSON.stringify({ kind: 'actual_local_mcp_controlled_model', results, requests, calls }, null, 2));
  console.log(JSON.stringify({ ok: true, evidence: resolve(directory, 'evidence.json'), cases: results.map(r => r.name) }));
} finally {
  await writeFile(resolve(directory, 'diagnostic.json'), JSON.stringify({ results, requests }, null, 2));
  await harness.close();
  await new Promise(done => server.close(done));
}
