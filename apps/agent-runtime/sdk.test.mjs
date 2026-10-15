import assert from 'node:assert/strict';
import test from 'node:test';
import { apply } from './campus-sdk.mjs';

function deployment(persisted = true) {
  const calls = []; const agent = {};
  const handle = { agent, async dispose() { calls.push(['dispose']); } };
  const ctx = { agents: { async create(options) { calls.push(['create', options]); return handle; },
    async resume(options) { calls.push(['resume', options]); return handle; } },
    sessionPersistence: { async stat(id) { calls.push(['stat', id]); return persisted; } },
    goals: { get() { return { id: 'goal', revision: 2, phase: 'paused' }; }, resume() { calls.push(['goal-resume']); } },
    extend(value) { ctx.proxy = value.agents; throw new Error('fixture: stop before serving stdio'); } };
  assert.throws(() => apply(ctx, {}), /stop before serving/);
  return { ctx, calls, handle };
}

async function withResume(request, run) {
  const previous = [process.env.CAMPUS_CONVERSATION_ID, process.env.CAMPUS_GOAL_RESUME];
  process.env.CAMPUS_CONVERSATION_ID = 'fixture-session'; process.env.CAMPUS_GOAL_RESUME = request;
  try { await run(); }
  finally {
    for (const [i, name] of ['CAMPUS_CONVERSATION_ID', 'CAMPUS_GOAL_RESUME'].entries()) {
      if (previous[i] === undefined) delete process.env[name]; else process.env[name] = previous[i];
    }
  }
}

test('SDK cold create and persisted resume preserve options', async () => {
  for (const persisted of [false, true]) {
    const { ctx, calls, handle } = deployment(persisted);
    const options = { sessionId: 'other-session', agentOptions: { model: 'fixture' }, signal: new AbortController().signal };
    assert.equal(await ctx.proxy.create(options), handle);
    assert.equal(calls[1][0], persisted ? 'resume' : 'create');
    assert.equal(calls[1][1].agentOptions, options.agentOptions);
    assert.equal(calls[1][1].signal, options.signal);
    if (persisted) assert.equal(calls[1][1].resumeSessionId, options.sessionId);
  }
});

test('rejected saved-goal resume disposes the unreturned agent handle', async () => {
  await withResume(JSON.stringify({ id: 'wrong', revision: 2 }), async () => {
    const { ctx, calls } = deployment();
    await assert.rejects(ctx.proxy.create({ sessionId: 'fixture-session' }), /Saved goal changed/);
    assert(calls.some(call => call[0] === 'dispose'));
  });
});

test('malformed host resume receipt emits no raw credential text', async () => {
  await withResume('sensitive-receipt', async () => {
    const { ctx, calls } = deployment();
    await assert.rejects(ctx.proxy.create({ sessionId: 'fixture-session' }), error => {
      assert(!error.message.includes('sensitive'));
      assert.equal(error.message, 'Invalid Campus goal resume request'); return true;
    });
    assert(!calls.some(call => call[0] === 'create' || call[0] === 'resume'));
  });
});

test('matching saved goal resumes once and unrelated sessions ignore the receipt', async () => {
  await withResume(JSON.stringify({ id: 'goal', revision: 2 }), async () => {
    const { ctx, calls, handle } = deployment();
    assert.equal(await ctx.proxy.create({ sessionId: 'fixture-session' }), handle);
    assert.equal(calls.filter(call => call[0] === 'goal-resume').length, 1);
    assert(!calls.some(call => call[0] === 'dispose'));
    await ctx.proxy.create({ sessionId: 'other-session' });
    assert.equal(calls.filter(call => call[0] === 'goal-resume').length, 1);
  });
});

test('an exception while resuming disposes the handle but preserves denial', async () => {
  await withResume(JSON.stringify({ id: 'goal', revision: 2 }), async () => {
    const { ctx, calls } = deployment();
    ctx.goals.resume = () => { throw new Error('fixture denial'); };
    await assert.rejects(ctx.proxy.create({ sessionId: 'fixture-session' }), /fixture denial/);
    assert(calls.some(call => call[0] === 'dispose'));
  });
});
