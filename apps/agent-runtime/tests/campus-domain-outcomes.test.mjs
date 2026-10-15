/** Public installed Cordis + ToolRuntime offline integration; no model/bridge/process calls. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Context } from '@deepseek-ai/cordis';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import * as campus from '../campus-plugin.mjs';

const schema = { type: 'object', additionalProperties: false,
  required: ['status', 'data', 'evidence', 'coverage', 'freshness', 'warnings', 'next_cursor', 'job_id', 'error'],
  properties: { status: { type: 'string', enum: ['ok', 'partial', 'queued', 'needs_user', 'error'] },
    data: { type: 'object' }, evidence: { type: 'array' }, coverage: { type: 'object' },
    freshness: { type: 'object' }, warnings: { type: 'array' },
    next_cursor: { oneOf: [{ type: 'string' }, { type: 'null' }] },
    job_id: { oneOf: [{ type: 'string' }, { type: 'null' }] },
    error: { oneOf: [{ type: 'object' }, { type: 'null' }] } } };
const envelope = status => ({ status, data: { items: [] }, evidence: [{ source: 'fixture-only' }],
  coverage: { complete: false }, freshness: { stale: true }, warnings: ['fixture-only'], next_cursor: 'fixture-cursor',
  job_id: 'fixture-job', error: status === 'error' ? { code: 'VALIDATION_ERROR', message: 'untrusted-text-fixture',
    details: { field: 'course_id', expected_type: 'string' } } : null });

async function fixture(names, run) {
  const ctx = new Context();
  try {
    new SystemPrompt(ctx, { includeHarnessIdentity: false });
    const tools = new ToolRuntime(ctx, { mode: 'native' });
    campus.registerCampusDomainOutcomes?.(ctx, names.map(model_name => ({ model_name })));
    await run(ctx, tools);
  } finally { await ctx.fiber.dispose(); }
}
function register(tools, name, value) {
  tools.register({ name, description: 'Offline domain fixture', parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: { schema, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    async execute() { return value; } });
}
const exec = name => ({ callId: 'fixture-call', name, arguments: {}, signal: new AbortController().signal });

test('validated campus error envelope is a failure and preserves complete model content', async () => {
  await fixture(['campus_fixture'], async (ctx, tools) => {
    const value = envelope('error');
    register(tools, 'campus_fixture', value);
    let observed;
    ctx.on('tools/result', (_exec, result) => { observed = result; });
    const result = await tools.execute(exec('campus_fixture'));
    assert.equal(result.isError, true, 'HTTP200 status:error must not be a canonical success');
    assert.equal(result.error.info.code, 'VALIDATION_ERROR');
    assert(!result.error.message.includes('untrusted-text-fixture'));
    assert.equal(result.value, undefined);
    assert.deepEqual(JSON.parse(result.content[0].text), value, 'keep details/evidence/coverage/cursor/job_id for recovery');
    assert.equal(observed.isError, true, 'authoritative result observer sees failure');
    assert.deepEqual(observed.content, result.content);
  });
});

test('ok partial queued needs_user remain successful envelopes', async () => {
  await fixture(['campus_fixture'], async (_ctx, tools) => {
    for (const status of ['ok', 'partial', 'queued', 'needs_user']) {
      const value = envelope(status);
      const dispose = tools.register({ name: 'campus_fixture', description: 'Offline fixture', parameters: { type: 'object' },
        output: { schema, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] }, async execute() { return value; } });
      try {
        const result = await tools.execute(exec('campus_fixture'));
        assert.equal(result.isError, false, status);
        assert.deepEqual(result.value, value);
      } finally { dispose(); }
    }
  });
});

test('native and unregistered campus-prefixed tools are not reclassified', async () => {
  await fixture(['campus_fixture'], async (_ctx, tools) => {
    for (const name of ['native_fixture', 'campus_not_in_inventory']) {
      register(tools, name, envelope('error'));
      assert.equal((await tools.execute(exec(name))).isError, false, name);
    }
  });
});

test('schema violations retain upstream failure without domain envelope mapping', async () => {
  await fixture(['campus_fixture'], async (_ctx, tools) => {
    register(tools, 'campus_fixture', { ...envelope('error'), status: 'invalid-fixture-status' });
    const result = await tools.execute(exec('campus_fixture'));
    assert.equal(result.isError, true);
    assert.equal(result.error.info.code, 'INVALID_TOOL_OUTPUT');
    assert.match(result.content[0].text, /^Error:/);
  });
});
