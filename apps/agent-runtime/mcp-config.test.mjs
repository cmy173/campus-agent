import assert from 'node:assert/strict';
import test from 'node:test';
import { apply } from './campus-mcp.mjs';

test('malformed credential JSON never appears in the diagnostic', async () => {
  const prior = process.env.CAMPUS_MCP_CREDENTIALS;
  process.env.CAMPUS_MCP_CREDENTIALS = 'fixture-do-not-log-credential';
  try {
    await assert.rejects(apply({}, { serverName: 'fixture', transport: 'stdio', command: 'unused' }), error => {
      assert(!error.message.includes('fixture-do-not-log-credential'));
      assert.equal(error.message, 'Invalid Campus MCP credential configuration');
      return true;
    });
  } finally {
    if (prior === undefined) delete process.env.CAMPUS_MCP_CREDENTIALS;
    else process.env.CAMPUS_MCP_CREDENTIALS = prior;
  }
});
import { credentialConfig } from './campus-mcp.mjs';

test('stdio receives only its own environment, HTTP only its own headers', () => {
  const serialized = JSON.stringify({ a: { env: { FIXTURE: 'a-only' }, headers: { Authorization: 'a-header' } },
    b: { env: { FIXTURE: 'b-only' }, headers: { Authorization: 'b-header' } } });
  assert.deepEqual(credentialConfig({ serverName: 'a', transport: 'stdio', command: 'fixture', env: { stale: 'x' }, headers: { stale: 'x' } }, serialized),
    { serverName: 'a', transport: 'stdio', command: 'fixture', env: { FIXTURE: 'a-only' } });
  assert.deepEqual(credentialConfig({ serverName: 'b', transport: 'streamable-http', url: 'http://127.0.0.1:1', env: { stale: 'x' } }, serialized),
    { serverName: 'b', transport: 'streamable-http', url: 'http://127.0.0.1:1', headers: { Authorization: 'b-header' } });
});

for (const value of ['null', '[]', '{"fixture":null}', '{"fixture":{"env":[]}}', '{"fixture":{"env":{"A":1}}}']) {
  test(`invalid credential shape is rejected (${value.length} bytes)`, () => {
    assert.throws(() => credentialConfig({ serverName: 'fixture', transport: 'stdio' }, value),
      { message: 'Invalid Campus MCP credential configuration' });
  });
}

test('missing server credentials use an empty map, not object prototype properties', () => {
  assert.deepEqual(credentialConfig({ serverName: 'constructor', transport: 'stdio' }, '{}').env, {});
});

test('the module must not export a plain `Config` (the harness would treat it as the plugin schema)', async () => {
  // Regression: an exported `Config` function made the harness skip this plugin
  // entirely — no error, no MCP tools, so every mcp__* tool disappeared from the
  // model's tool list and the browser could never be driven.
  const module = await import('./campus-mcp.mjs');
  assert.equal(typeof module.Config, 'undefined');
  assert.equal(typeof module.transportConfig, 'function');
  const resolved = module.transportConfig({ serverName: 'fixture', transport: 'stdio', command: 'unused' });
  assert.equal(resolved.serverName, 'fixture');
  assert.deepEqual(module.inject, ['tools']);
});
