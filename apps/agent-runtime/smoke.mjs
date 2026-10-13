/** Runtime-only verification. The controlled provider is deliberately a test fixture. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { DeepSeekHarness } from '@deepseek-ai/dsh-sdk-client';
import { prepareProfile } from './prepare-profile.mjs';

const requests = [];
const server = createServer(async (req, res) => {
  const chunks = []; for await (const c of req) chunks.push(c);
  const body = JSON.parse(Buffer.concat(chunks).toString() || '{}');
  requests.push({ path: req.url, body });
  res.setHeader('Content-Type', 'application/json');
  if (req.url.endsWith('/schemas')) return res.end(JSON.stringify([{
    name: 'courses.list', model_name: 'campus_courses_list', description: 'Read test courses', effects: 'read',
    input_schema: { type: 'object', properties: {}, additionalProperties: false },
  }]));
  if (req.url.endsWith('/tool')) return res.end(JSON.stringify({
    status: 'ok', data: { items: [{ id: 'synthetic_course', title: '合成课程' }] }, evidence: [],
    coverage: { complete: true }, freshness: { fetched_at: null, stale: true }, warnings: [],
    next_cursor: null, job_id: null, error: null,
  }));
  const toolResult = [...body.messages].reverse().find(m => m.role === 'tool');
  if (!toolResult) return res.end(JSON.stringify({ tool_calls: [{ id: 'test_call_1', name: 'campus_courses_list', arguments: {} }] }));
  // The result is copied from tool data, not a hardcoded domain answer.
  const text = toolResult.content.filter(b => b.type === 'text').map(b => b.text).join('');
  res.end(JSON.stringify({ content: text }));
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const home = await mkdtemp(resolve('../../var/runtime-smoke-'));
const profile = await prepareProfile(home);
const harness = new DeepSeekHarness({
  ...profile, provider: 'campus-model', model: 'controlled-smoke', processCwd: process.cwd(),
  initializeTimeoutMs: 30000, env: {
    PATH: process.env.PATH, SystemRoot: process.env.SystemRoot, USERPROFILE: process.env.USERPROFILE,
    CAMPUS_BRIDGE_URL: `http://127.0.0.1:${server.address().port}`, CAMPUS_BRIDGE_TOKEN: 'smoke-only',
  },
});
try {
  const result = await harness.run('Read the available courses once, and return the tool result.');
  assert.match(result.finalResponse, /synthetic_course/);
  assert(requests.some(r => r.path.endsWith('/tool')));
  for (const r of requests.filter(r => r.path.endsWith('/model'))) {
    assert.deepEqual(r.body.tools.map(t => t.name), ['campus_courses_list']);
  }
  await writeFile(resolve(home, 'runtime-evidence.json'), JSON.stringify({
    runtime_version: '0.2.0-rc.2', plugins: profile.entries, tool_names: ['campus_courses_list'],
    events: result.events, final_response: result.finalResponse,
  }, null, 2));
  console.log(JSON.stringify({ ok: true, home, event_count: result.events.length, tool_names: ['campus_courses_list'] }));
} finally {
  await harness.close();
  await new Promise(resolve => server.close(resolve));
}
