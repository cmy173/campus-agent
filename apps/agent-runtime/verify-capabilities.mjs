/** Actual upstream tools with a controlled conversation/search API; no paid calls. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DeepSeekHarness } from '@deepseek-ai/dsh-sdk-client';
import { prepareProfile } from './prepare-profile.mjs';

const runtimeDirectory = fileURLToPath(new URL('.', import.meta.url));
const directory = await mkdtemp(fileURLToPath(new URL('../../var/v2-capabilities-', import.meta.url)));
const workspace = resolve(directory, 'workspace');
await (await import('node:fs/promises')).mkdir(workspace);
const requests = [], results = [];
const text = message => (message?.content ?? []).filter(b => b.type === 'text').map(b => b.text).join('\n');
const server = createServer(async (req, res) => {
  const chunks = []; for await (const chunk of req) chunks.push(chunk);
  const body = JSON.parse(Buffer.concat(chunks).toString() || '{}');
  requests.push({ path: req.url, body });
  res.setHeader('Content-Type', 'application/json');
  if (req.url.endsWith('/schemas')) return res.end('[]');
  if (req.url.endsWith('/model-lease')) return res.end('{"ok":true}');
  if (req.url.endsWith('/tool-lease')) return res.end('{"ok":true}');
  if (req.url.endsWith('/messages')) return res.end(JSON.stringify({
    content: [{ type: 'web_search_tool_result', content: [{ type: 'web_search_result',
      url: 'https://api-docs.deepseek.com/', title: 'Official DeepSeek API Docs' }] },
      { type: 'text', text: 'Search fixture response', citations: [{ url: 'https://api-docs.deepseek.com/', cited_text: 'Official API documentation' }] }],
  }));
  const user = [...body.messages].reverse().find(m => m.role === 'user' && /CASE_[A-Z_]+/.test(text(m)));
  const kind = text(user);
  const index = body.messages.lastIndexOf(user);
  const tools = body.messages.slice(index + 1).filter(m => m.role === 'tool');
  const call = (name, arguments_) => [{ id: 'native_' + requests.length, name, arguments: arguments_ }];
  let tool_calls = [], content = '';
  if (kind.includes('CASE_FILES')) {
    if (!tools.length) tool_calls = call('write', { file_path: 'notes.md', content: '# Actual tool fixture\nsource-marker-731\n' });
    else if (tools.length === 1) tool_calls = call('read', { file_path: 'notes.md' });
    else if (tools.length === 2) tool_calls = call('pwsh', { command: '& $env:DSH_PYTHON_EXECUTABLE -u -c "print(6 * 7)" | Out-String', description: 'Calculate a fixture result with Python' });
    else content = tools.map(text).join('\n');
  } else if (kind.includes('CASE_SKILL')) {
    if (!tools.length) tool_calls = call('skill', { name: 'campus-material-organizer' });
    else content = tools.map(text).join('\n');
  } else if (kind.includes('CASE_WEB')) {
    if (!tools.length) tool_calls = call('web_search', { queries: ['Official DeepSeek API documentation'] });
    else if (tools.length === 1) tool_calls = call('web_fetch', { url: 'https://api-docs.deepseek.com/' });
    else content = tools.map(text).join('\n');
  } else if (kind.includes('CASE_CHILD_READ')) {
    if (!tools.length) tool_calls = call('read', { file_path: 'notes.md' });
    else content = tools.map(text).join('\n');
  } else if (kind.includes('CASE_CHILD_CALC')) {
    if (!tools.length) tool_calls = call('pwsh', { command: '& $env:DSH_PYTHON_EXECUTABLE -c "print(7 * 8)"', description: 'Calculate the independent second subtask' });
    else content = tools.map(text).join('\n');
  } else if (kind.includes('CASE_SUBAGENTS')) {
    if (!tools.length) tool_calls = call('subagent', { description: 'Read the task material', prompt: 'CASE_CHILD_READ Read notes.md and report its actual content.', run_in_background: false });
    else if (tools.length === 1) tool_calls = call('subagent', { description: 'Compute the second result', prompt: 'CASE_CHILD_CALC Calculate 7 times 8 with Python and report actual output.', run_in_background: false });
    else content = tools.map(text).join('\n');
  } else content = 'Unhandled fixture input: ' + kind;
  res.end(JSON.stringify({ tool_calls, content }));
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const baseURL = `http://127.0.0.1:${server.address().port}`;
const profile = await prepareProfile(resolve(directory, 'home'), { workspace, searchBaseURL: baseURL, searchModel: 'deepseek-flash' });
const harness = new DeepSeekHarness({ ...profile, provider: 'campus-model', model: 'controlled-capabilities',
  cwd: workspace, processCwd: runtimeDirectory, initializeTimeoutMs: 60000,
  env: { PATH: process.env.PATH, PATHEXT: process.env.PATHEXT, ComSpec: process.env.ComSpec,
    SystemRoot: process.env.SystemRoot, USERPROFILE: process.env.USERPROFILE,
    TEMP: process.env.TEMP, TMP: process.env.TMP, CAMPUS_BRIDGE_URL: baseURL, CAMPUS_BRIDGE_TOKEN: 'fixture-only',
    DEEPSEEK_SEARCH_API_KEY: 'fixture-only' } });
try {
  for (const name of ['FILES', 'SKILL', 'WEB', 'SUBAGENTS']) {
    const started = performance.now();
    const result = await harness.run('CASE_' + name + ' Complete the requested fixture task using actual tools.', { sessionId: 'v2_' + name.toLowerCase() });
    results.push({ name, elapsed_ms: performance.now() - started, response: result.finalResponse, events: result.events, notifications: result.notifications });
    if (name === 'FILES') {
      assert.match(result.finalResponse, /source-marker-731/);
      assert.match(result.finalResponse, /42/);
      assert.equal(await readFile(resolve(workspace, 'notes.md'), 'utf-8'), '# Actual tool fixture\nsource-marker-731\n');
    } else if (name === 'SKILL') assert.match(result.finalResponse, /学校提交分开/);
    else if (name === 'WEB') {
      assert.match(result.finalResponse, /https:\/\/api-docs.deepseek.com/);
      assert.match(result.finalResponse, /Your First API Call/);
    } else {
      assert.match(result.finalResponse, /source-marker-731/);
      assert.match(result.finalResponse, /56/);
      const children = result.notifications.filter(n => n.method === 'subagent.started');
      assert.equal(children.length, 2);
      assert.equal(new Set(children.map(n => n.params.childSessionId)).size, 2);
    }
  }
  await writeFile(resolve(directory, 'evidence.json'), JSON.stringify({ kind: 'controlled_model_actual_upstream_tools',
    bundles: profile.bundles, results, requests }, null, 2));
  console.log(JSON.stringify({ ok: true, evidence: resolve(directory, 'evidence.json'), cases: results.map(r => ({ name: r.name, elapsed_ms: r.elapsed_ms })) }));
} finally {
  await writeFile(resolve(directory, 'diagnostic.json'), JSON.stringify({ results, requests }, null, 2));
  await harness.close();
  await new Promise(resolve => server.close(resolve));
}
