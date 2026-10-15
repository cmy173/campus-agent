import assert from 'node:assert/strict';
import test from 'node:test';
import { CampusModelAdapter } from '../campus-plugin.mjs';

for (const [code, retryable, expected] of [
  ['PROVIDER_TIMEOUT', true, 'TIMEOUT'], ['PROVIDER_RATE_LIMITED', true, 'RATE_LIMIT'],
  ['PROVIDER_UNREACHABLE', true, 'TRANSPORT'], ['PROVIDER_HTTP_ERROR', true, 'SERVER'],
  ['PROVIDER_AUTH_FAILED', false, 'AUTH'], ['PROVIDER_HTTP_ERROR', false, 'BAD_REQUEST'],
]) test(`campus-model preserves ${code}/${retryable} for the installed Harness recovery policy`, async () => {
  const saved = globalThis.fetch, old = process.env.CAMPUS_BRIDGE_URL;
  process.env.CAMPUS_BRIDGE_URL = 'http://127.0.0.1:1';
  globalThis.fetch = async () => ({ ok: false, json: async () => ({ error: {
    code, retryable, message: 'synthetic-untrusted-provider-secret',
  } }) });
  try {
    const chunks = [];
    for await (const chunk of new CampusModelAdapter().stream({ messages: [], tools: [] })) chunks.push(chunk);
    assert.equal(chunks.at(-1).reason.kind, 'error');
    assert.equal(chunks.at(-1).reason.failure.code, expected);
    assert(!JSON.stringify(chunks).includes('synthetic-untrusted-provider-secret'));
  } finally {
    globalThis.fetch = saved;
    if (old === undefined) delete process.env.CAMPUS_BRIDGE_URL; else process.env.CAMPUS_BRIDGE_URL = old;
  }
});
