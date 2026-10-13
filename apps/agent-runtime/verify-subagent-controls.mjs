/** Concurrent actual child agents and selective human cancellation, no paid model. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DeepSeekHarness } from '@deepseek-ai/dsh-sdk-client';
import { prepareProfile } from './prepare-profile.mjs';

const directory = await mkdtemp(fileURLToPath(new URL('../../var/v2-subagent-controls-', import.meta.url)));
await writeFile(resolve(directory, 'material.md'), 'Independent child source marker 398\n');
const rootId = 'control-root', token = 'fixture-control-only', requests = [], cases = [];
const text = m => (m?.content ?? []).filter(b => b.type === 'text').map(b => b.text).join('\n');
const send = (res, data) => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data)); };
let controls, state;
const server = createServer(async (req, res) => {
  const chunks = []; for await (const chunk of req) chunks.push(chunk);
  const body = JSON.parse(Buffer.concat(chunks).toString() || '{}');
  requests.push({ path: req.url, body });
  if (req.url.endsWith('/schemas')) return send(res, []);
  if (req.url.endsWith('/control-address')) { controls = body.url; return send(res, { ok: true }); }
  if (req.url.endsWith('/tool-lease')) return send(res, { ok: true });
  const user = [...body.messages].reverse().find(m => m.role === 'user' && text(m).includes('CONTROL_'));
  const marker = text(user);
  const tools = body.messages.slice(body.messages.lastIndexOf(user) + 1).filter(m => m.role === 'tool');
  const call = (id, name, args) => ({ id, name, arguments: args });
  if (marker.includes('CONTROL_CHILD_A')) {
    if (!tools.length) return send(res, { tool_calls: [call('a_' + requests.length, 'pwsh', {
      command: 'Start-Sleep -Seconds 30; Write-Output "SHOULD_NOT_COMPLETE"', description: 'Long independent child computation' })] });
    return send(res, { content: tools.map(text).join('\n'), tool_calls: [] });
  }
  if (marker.includes('CONTROL_CHILD_B')) {
    if (!tools.length) return send(res, { tool_calls: [call('b_' + requests.length, 'read', { file_path: 'material.md' })] });
    state.releaseB = () => send(res, { content: tools.map(text).join('\n'), tool_calls: [] });
    state.bReady.resolve();
    return;
  }
  if (!tools.length) return send(res, { tool_calls: [
    call('spawn_a_' + requests.length, 'subagent', { description: 'Independent child A', prompt: 'CONTROL_CHILD_A Perform the long computation.', run_in_background: state.background }),
    call('spawn_b_' + requests.length, 'subagent', { description: 'Independent child B', prompt: 'CONTROL_CHILD_B Read the material and report its actual text.', run_in_background: state.background }),
  ] });
  const complete = () => send(res, { content: tools.map(text).join('\n') + '\n' + (state.bOutput ?? ''), tool_calls: [] });
  if (state.background && !state.bFinished) state.releaseRoot = complete;
  else complete();
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const profile = await prepareProfile(resolve(directory, 'home'), { workspace: directory });
const harness = new DeepSeekHarness({ ...profile, provider: 'campus-model', model: 'controlled-subagents',
  cwd: directory, processCwd: fileURLToPath(new URL('.', import.meta.url)), initializeTimeoutMs: 60000,
  env: { PATH: process.env.PATH, PATHEXT: process.env.PATHEXT, SystemRoot: process.env.SystemRoot,
    ComSpec: process.env.ComSpec, USERPROFILE: process.env.USERPROFILE, TEMP: directory, TMP: directory,
    CAMPUS_BRIDGE_URL: `http://127.0.0.1:${server.address().port}`, CAMPUS_BRIDGE_TOKEN: token, CAMPUS_CONVERSATION_ID: rootId } });

async function bounded(promise) {
  let timer;
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Actual child lifecycle timed out')), 20000); })]); }
  finally { clearTimeout(timer); }
}

try {
  for (const background of [false, true]) {
    state = { background, aRunning: Promise.withResolvers(), bReady: Promise.withResolvers(), aEnded: Promise.withResolvers(),
      bEnded: Promise.withResolvers(), notifications: [] };
    const run = harness.run('CONTROL_ROOT_' + (background ? 'BACKGROUND' : 'FOREGROUND') + ' Complete two independent children.', {
      sessionId: rootId,
      onNotification: n => {
        state.notifications.push(n);
        if (n.method === 'session.event' && n.params.event.type === 'tool/call') {
          const name = n.params.event.data.name;
          if (name === 'pwsh') { state.aId = n.params.sessionId; state.aRunning.resolve(); }
          if (name === 'read') state.bId = n.params.sessionId;
        }
        if (n.method === 'subagent.finished') {
          if (n.params.childSessionId === state.aId) { state.aFinish = n.params; state.aEnded.resolve(); }
          if (n.params.childSessionId === state.bId) {
            state.bFinished = true; state.bFinish = n.params; state.bOutput = text({ content: n.params.lastAssistantMessage });
            state.bEnded.resolve(); state.releaseRoot?.();
          }
        }
      },
    });
    await bounded(Promise.all([state.aRunning.promise, state.bReady.promise]));
    assert(controls && state.aId !== state.bId, 'Both independent children must be live');
    const denied = await fetch(controls + '/interrupt', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parent_id: rootId, child_id: state.aId }) });
    assert.equal(denied.status, 403);
    const response = await fetch(controls + '/interrupt', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
      body: JSON.stringify({ parent_id: rootId, child_id: state.aId }) });
    assert.equal(response.status, 200);
    await bounded(state.aEnded.promise);
    assert.equal(state.aFinish.stopReason, 'aborted');
    assert.equal(state.bFinished, undefined, 'Stopping A must not stop or finish B');
    state.releaseB();
    await bounded(state.bEnded.promise);
    assert.equal(state.bFinish.status, 'ok');
    assert.match(state.bOutput, /Independent child source marker 398/);
    const result = await bounded(run);
    assert(!result.finalResponse.includes('SHOULD_NOT_COMPLETE'));
    cases.push({ background, a: state.aFinish, b: state.bFinish, notifications: state.notifications, events: result.events });
  }
  await writeFile(resolve(directory, 'evidence.json'), JSON.stringify({ kind: 'actual_parallel_children_selective_control_controlled_model', cases, requests }, null, 2));
  console.log(JSON.stringify({ ok: true, evidence: resolve(directory, 'evidence.json'), cases: ['foreground', 'continuable_background'] }));
} finally {
  await harness.close();
  server.closeAllConnections(); await new Promise(done => server.close(done));
  await writeFile(resolve(directory, 'diagnostic.json'), JSON.stringify({ cases, requests }, null, 2));
}
