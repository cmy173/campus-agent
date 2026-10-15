/** Actual upstream goal rounds and persistence; controlled local model only. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DeepSeekHarness } from '@deepseek-ai/dsh-sdk-client';
import { prepareProfile } from './prepare-profile.mjs';
import { runWithGoalLifecycle } from './goal-lifecycle.mjs';

const directory = await mkdtemp(fileURLToPath(new URL('../../var/v2-goal-', import.meta.url)));
const token = 'fixture-goal-only', cid = 'goal-proof', requests = [], notifications = [];
let mode = process.argv[2] ?? 'complete';
assert(['complete', 'pause', 'cap'].includes(mode));
let controls, goalSchema, updateSchema;
const text = m => (m?.content ?? []).filter(b => b.type === 'text').map(b => b.text).join('\n');
const send = (res, value) => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(value)); };
const call = (name, args) => ({ id: name + '_' + requests.length, name, arguments: args });
const server = createServer(async (req, res) => {
  const chunks = []; for await (const c of req) chunks.push(c);
  const body = JSON.parse(Buffer.concat(chunks).toString() || '{}');
  if (req.url.endsWith('/schemas')) return send(res, []);
  if (req.url.endsWith('/control-address')) { controls = body.url; return send(res, { ok: true }); }
  if (req.url.endsWith('/tool-lease')) return send(res, { ok: true });
  requests.push(body);
  const user = [...body.messages].reverse().find(m => m.role === 'user' &&
    (text(m).includes('GOAL_TEST_START') || text(m).includes('GOAL_TEST_RESUME') || text(m).includes('<goal_round>')));
  const recent = body.messages.slice(body.messages.lastIndexOf(user) + 1).filter(m => m.role === 'tool');
  const marker = text(user);
  if (!goalSchema) {
    goalSchema = body.tools.find(t => t.name === 'create_goal');
    updateSchema = body.tools.find(t => t.name === 'update_goal');
  }
  if (marker.includes('GOAL_TEST_RESUME')) {
    return send(res, { content: 'GOAL_TEST goal resumed from the persisted checkpoint', tool_calls: [] });
  }
  if (marker.includes('GOAL_TEST_START') && !recent.length)
    return send(res, { tool_calls: [call('create_goal', { objective: 'GOAL_TEST finish the real local task and persist progress', max_goal_rounds: mode === 'cap' ? 2 : 4 })] });
  if (marker.includes('<goal_round>')) {
    if (mode === 'cap') return send(res, { content: 'GOAL_TEST progress saved, objective remains unfinished', tool_calls: [] });
    if (mode === 'pause') {
      await new Promise(done => setTimeout(done, 2000));
      if (!res.destroyed) send(res, { content: 'GOAL_TEST delayed model answer', tool_calls: [] });
      return;
    }
    if (!recent.length) {
      const response = await fetch(controls + '/goal', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
        body: JSON.stringify({ operation: 'read' }) });
      const { goal } = await response.json();
      return send(res, { tool_calls: [call('update_goal', { goal_id: goal.id, revision: goal.revision, action: 'complete' })] });
    }
    return send(res, { content: 'GOAL_TEST actual goal completed', tool_calls: [] });
  }
  return send(res, { content: 'GOAL_TEST initial progress saved', tool_calls: [] });
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const profile = await prepareProfile(resolve(directory, 'home'), { workspace: directory });
const harness = new DeepSeekHarness({ ...profile, provider: 'campus-model', model: 'goal-controlled', cwd: directory,
  processCwd: fileURLToPath(new URL('.', import.meta.url)), initializeTimeoutMs: 60000,
  env: { ...process.env, CAMPUS_BRIDGE_URL: `http://127.0.0.1:${server.address().port}`,
    CAMPUS_BRIDGE_TOKEN: token, CAMPUS_CONVERSATION_ID: cid } });
const evidence = { kind: 'actual_harness_native_goal_controlled_model', directory, mode };
try {
  const result = await runWithGoalLifecycle(harness, 'GOAL_TEST_START Create the completion goal and continue it.', {
    sessionId: cid, onNotification: n => notifications.push(n),
    readGoal: async () => {
      assert(controls && goalSchema && updateSchema);
      const state = await fetch(controls + '/goal', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
        body: JSON.stringify({ operation: 'read' }) });
      assert.equal(state.status, 200);
      evidence.final_goal = await state.json();
      evidence.goal_when_run_returned ??= evidence.final_goal;
      if (mode === 'pause' && evidence.final_goal.goal?.phase === 'active' && evidence.final_goal.goal.roundsStarted === 1 && !evidence.pause_ack) {
        const goal = evidence.final_goal.goal;
        const paused = await fetch(controls + '/goal', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
          body: JSON.stringify({ operation: 'pause', id: goal.id, revision: goal.revision }) });
        assert.equal(paused.status, 200);
        evidence.pause_ack = await paused.json();
        assert.equal(evidence.pause_ack.goal.phase, 'paused');
      }
      return evidence.final_goal;
    },
  });
  evidence.final_response = result.finalResponse;
  assert.equal(evidence.final_goal.goal?.phase, mode === 'complete' ? 'complete' : mode === 'pause' ? 'paused' : 'blocked');
  assert.equal(evidence.final_goal.goal.activation, 'disarmed');
  assert.equal(evidence.final_goal.agent_status, 'idle');
  assert.equal(evidence.final_goal.goal.roundsStarted, mode === 'cap' ? 2 : 1);
  if (mode === 'complete') assert.equal(result.finalResponse, 'GOAL_TEST actual goal completed');
  if (mode === 'cap') assert.equal(result.finalResponse, 'GOAL_TEST progress saved, objective remains unfinished');
  if (mode === 'cap') assert.equal(evidence.final_goal.goal.blockedReason.code, 'round-limit');
  assert(requests.some(b => b.messages.some(m => text(m).includes('<goal_round>'))));
  assert(notifications.filter(n => n.params?.event?.type === 'assistant/message').length >= (mode === 'pause' ? 1 : 2),
    'The public subscription must remain alive after the first run() boundary');
  if (mode === 'pause') {
    await harness.close();
    mode = 'complete';
    const resumed = new DeepSeekHarness({ ...harness.client.options,
      env: { ...harness.client.options.env, CAMPUS_GOAL_RESUME: JSON.stringify({ id: evidence.final_goal.goal.id, revision: evidence.final_goal.goal.revision }) } });
    try {
      const result = await runWithGoalLifecycle(resumed, 'GOAL_TEST_RESUME Please resume the saved goal and finish it.', {
        sessionId: cid, onNotification: n => notifications.push(n),
        readGoal: async () => {
          const response = await fetch(controls + '/goal', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
            body: JSON.stringify({ operation: 'read' }) });
          assert.equal(response.status, 200);
          evidence.cold_resume = await response.json();
          return evidence.cold_resume;
        },
      });
      assert.equal(evidence.cold_resume.goal.id, evidence.final_goal.goal.id);
      assert.equal(evidence.cold_resume.goal.phase, 'complete');
      assert.equal(evidence.cold_resume.goal.roundsStarted, 2);
      assert.equal(evidence.cold_resume.goal.revision, 4);
      assert.equal(result.finalResponse, 'GOAL_TEST actual goal completed');
    } finally { await resumed.close(); }
  }
  evidence.ok = true;
} finally {
  await harness.close();
  await new Promise(done => server.close(done));
  await writeFile(resolve(directory, 'evidence.json'), JSON.stringify({ ...evidence, goalSchema, updateSchema, notifications }, null, 2));
  console.log(JSON.stringify({ ...evidence, evidence: resolve(directory, 'evidence.json') }));
}
