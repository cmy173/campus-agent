/** Offline contract regression tests: no provider, credentials, user sessions or external calls. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { EventEmitter } from 'node:events';
import { DeepSeekHarness } from '@deepseek-ai/dsh-sdk-client';
import { BlockAssembler } from '@deepseek-ai/dsh-llm';
import { CampusModelAdapter, nativeModelFailure } from '../campus-plugin.mjs';
import { runWithGoalLifecycle } from '../goal-lifecycle.mjs';

const rootId = 'runtime-audit-fixture';
const assistant = (text, sessionId = rootId) => ({ method: 'session.event', params: { sessionId,
  event: { type: 'assistant/message', data: { message: { content: [{ type: 'text', text }] } } } } });
function fixture(events, result = {}) {
  let closed = false;
  let release;
  const subscription = {
    async *[Symbol.asyncIterator]() {
      for (const event of events) yield event;
      if (!closed) await new Promise(resolve => { release = resolve; });
    },
    close() { closed = true; release?.(); },
  };
  const prompts = [];
  return { prompts, get closed() { return closed; }, harness: {
    client: { subscribeSessionTree(id) { assert.equal(id, rootId); return subscription; } },
    async run(text, options) { prompts.push({ text, options });
      await new Promise(resolve => setImmediate(resolve));
      return { sessionId: rootId, finalResponse: 'initial response', events: [], ...result };
    },
  } };
}
const lifecycleOptions = { sessionId: rootId, readGoal: async () => ({ goal: null, agent_status: 'idle' }) };

test('campus adapter obeys installed rc.2 terminal FinishReason envelopes', async () => {
  const originalFetch = globalThis.fetch;
  const oldBase = process.env.CAMPUS_BRIDGE_URL;
  const oldToken = process.env.CAMPUS_BRIDGE_TOKEN;
  process.env.CAMPUS_BRIDGE_URL = 'http://127.0.0.1:1';
  process.env.CAMPUS_BRIDGE_TOKEN = 'runtime-audit-fixture-only';
  try {
    for (const [value, kind] of [
      [{ content: 'offline answer' }, 'stop'],
      [{ tool_calls: [{ id: 'fixture-call', name: 'fixture_tool', arguments: { a: 1 } }] }, 'tool-calls'],
    ]) {
      globalThis.fetch = async (url, options) => {
        assert.equal(url, 'http://127.0.0.1:1/api/v1/agent/internal/model');
        assert.equal(options.redirect, 'error');
        const request = JSON.parse(options.body);
        assert.equal(request.max_tokens, 1234);
        return { ok: true, json: async () => value };
      };
      const chunks = [];
      for await (const chunk of new CampusModelAdapter().stream({ sessionId: rootId, messages: [], maxTokens: 1234 })) chunks.push(chunk);
      assert.deepEqual(chunks.at(-1), { type: 'finish', reason: { kind } });
      const assembler = new BlockAssembler();
      for (const chunk of chunks) assembler.push(chunk);
      assert.deepEqual(assembler.finish, { kind }, 'installed assembler receives a valid finish reason');
      const call = chunks.find(chunk => chunk.type === 'block-end' && chunk.block.type === 'tool-call');
      if (call) assert.equal(call.block.arguments, '{"a":1}');
    }
  } finally {
    globalThis.fetch = originalFetch;
    if (oldBase === undefined) delete process.env.CAMPUS_BRIDGE_URL; else process.env.CAMPUS_BRIDGE_URL = oldBase;
    if (oldToken === undefined) delete process.env.CAMPUS_BRIDGE_TOKEN; else process.env.CAMPUS_BRIDGE_TOKEN = oldToken;
  }
});

test('lifecycle preserves native ownership and excludes child final answers', async () => {
  const f = fixture([assistant('root answer'), assistant('child answer', 'child-fixture')]);
  const result = await runWithGoalLifecycle(f.harness, 'offline task', lifecycleOptions);
  assert.equal(result.finalResponse, 'root answer');
  assert.equal(result.eventCount, 2);
  assert.equal(f.prompts.length, 1, 'observer must not add Goal continuation prompts');
  assert(f.closed);
});

test('lifecycle surfaces a native root turn error rather than returning success', async () => {
  const failure = { method: 'session.event', params: { sessionId: rootId,
    event: { type: 'turn/end', data: { reason: { kind: 'error', error: { code: 'AUTH', message: 'untrusted-provider-body-fixture' } } } } } };
  const f = fixture([assistant('partial answer'), failure]);
  await assert.rejects(runWithGoalLifecycle(f.harness, 'offline task', lifecycleOptions), error =>
    error.code === 'AUTH' && !error.message.includes('untrusted-provider-body-fixture'));
  assert(f.closed);
});

test('lifecycle polls through native Goal admission gap without sending prompts', async () => {
  const f = fixture([assistant('native answer')]);
  let polls = 0;
  const states = [];
  const result = await runWithGoalLifecycle(f.harness, 'offline task', { ...lifecycleOptions,
    readGoal: async () => ++polls === 1
      ? { goal: { phase: 'active', activation: 'armed' }, agent_status: 'idle' }
      : { goal: { phase: 'complete', activation: 'disarmed' }, agent_status: 'idle' },
    onGoalState: state => states.push(state),
  });
  assert.equal(result.finalResponse, 'native answer');
  assert.equal(polls, 2);
  assert.equal(states.length, 2);
  assert.equal(f.prompts.length, 1);
  assert(f.closed);
});

test('native provider journal classification does not copy remote diagnostics', () => {
  assert.deepEqual(nativeModelFailure({ code: 'AUTH', message: 'remote-sensitive-fixture' }), {
    code: 'PROVIDER_AUTH_FAILED', message: 'DeepSeek 服务拒绝了凭据，请检查模型密钥与权限。', retryable: false,
  });
  assert(!JSON.stringify(nativeModelFailure({ message: 'remote-sensitive-fixture' })).includes('remote-sensitive-fixture'));
});

test('installed SDK settles an error turn at idle; lifecycle must inspect root turn/end', async () => {
  const receipt = { method: 'session.event', params: { sessionId: rootId, event: {
    type: 'agent/inbox/spliced', data: { inserted: [{ id: 'fixture-message-id' }] },
  } } };
  const errorEvent = { type: 'turn/end', data: { reason: {
    kind: 'error', error: { code: 'AUTH', message: 'fixture-provider-body' },
  } } };
  const events = [receipt, assistant('partial answer'),
    { method: 'session.event', params: { sessionId: rootId, event: errorEvent } },
    { method: 'session.status', params: { sessionId: rootId, status: 'idle' } }];
  const client = { start() {}, async initialize() {}, async close() {},
    async prompt(id, blocks) { assert.equal(id, rootId); assert.equal(blocks[0].text, 'offline prompt'); return 'fixture-message-id'; },
    subscribeSessionTree() { let index = 0; return { async next() { return events[index++]; }, close() {} }; },
  };
  const harness = new DeepSeekHarness({}, () => client);
  try {
    const result = await harness.run('offline prompt', { sessionId: rootId });
    assert.equal(result.finalResponse, 'partial answer', 'the installed SDK does not reject native turn errors');
    assert(result.events.includes(errorEvent));
    const f = fixture([], result);
    await assert.rejects(runWithGoalLifecycle(f.harness, 'offline task', lifecycleOptions), { code: 'AUTH' });
    assert(f.closed);
  } finally { await harness.close(); }
});

test('child turn failure does not fail the root activity interval', async () => {
  const failure = { method: 'session.event', params: { sessionId: 'child-fixture', event: {
    type: 'turn/end', data: { reason: { kind: 'error', error: { code: 'AUTH' } } },
  } } };
  const f = fixture([failure, assistant('root answer')]);
  assert.equal((await runWithGoalLifecycle(f.harness, 'offline task', lifecycleOptions)).finalResponse, 'root answer');
  assert(f.closed);
});

test('goal-state failure closes its observer without hiding the failure', async () => {
  const f = fixture([]);
  await assert.rejects(runWithGoalLifecycle(f.harness, 'offline task', { ...lifecycleOptions,
    readGoal: async () => { throw new Error('offline state failure'); },
  }), /offline state failure/);
  assert(f.closed);
});

test('cancellation skips goal polling and still closes subscription', async () => {
  const f = fixture([]);
  await runWithGoalLifecycle(f.harness, 'offline task', { ...lifecycleOptions,
    isCancelled: () => true, readGoal: async () => { throw new Error('unexpected poll'); },
  });
  assert(f.closed);
});

// Evaluate the unchanged runner body with explicit in-memory dependency fixtures.
// This exercises its protocol/error branches without spawning (sandbox denies Node spawn).
async function runnerFixture(input, { marks = false } = {}) {
  const source = await readFile(new URL('../runner.mjs', import.meta.url), 'utf8');
  const body = source.replace(/^import .*;$/gm, '');
  const lines = new EventEmitter();
  lines.close = () => lines.emit('close');
  const output = [], diagnostics = [];
  const processFixture = {
    env: marks ? { CAMPUS_RUNNER_MARKS: '1' } : {},
    stdin: {}, stdout: { write: value => output.push(value) },
    stderr: { write: value => diagnostics.push(value) },
    on() {}, cwd: () => '.',
  };
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const run = new AsyncFunction('createInterface', 'DeepSeekHarness', 'prepareProfile',
    'mcpCredentialEnvironment', 'runWithGoalLifecycle', 'process', 'performance', 'AbortSignal', 'fetch', body);
  const task = run(() => { queueMicrotask(() => lines.emit('line', JSON.stringify(input))); return lines; },
    class {}, async () => { throw new Error('sensitive-profile-path-fixture'); },
    () => '{}', () => { throw new Error('unexpected model call'); },
    processFixture, performance, AbortSignal, () => { throw new Error('unexpected network call'); });
  let error;
  try { await task; } catch (value) { error = value; }
  return { frames: output.join('').trim().split('\n').filter(Boolean).map(JSON.parse), diagnostics: diagnostics.join(''), error };
}

test('runner emits sanitized protocol error on initial profile failure', async () => {
  const fixture = await runnerFixture({ home: {}, conversation_id: rootId, text: 'offline fixture' });
  assert.equal(fixture.frames.at(-1)?.kind, 'error', 'initial profile failure must be framed');
  assert.equal(fixture.frames.at(-1).data.code, 'HARNESS_FAILED');
  assert.equal(fixture.frames.at(-1).turn, 1);
  assert.equal(fixture.diagnostics, '', 'default stderr must remain empty');
  assert.equal(fixture.error, undefined, 'failure is represented in protocol rather than unhandled throw');
});

test('runner diagnostic marks never include thrown profile/provider text', async () => {
  const fixture = await runnerFixture({ home: {}, conversation_id: rootId, text: 'offline fixture' }, { marks: true });
  assert.match(fixture.diagnostics, /MARK modules-loaded/);
  assert(!fixture.diagnostics.includes('sensitive-profile-path-fixture'));
  assert.equal(fixture.frames.at(-1)?.kind, 'error');
});
