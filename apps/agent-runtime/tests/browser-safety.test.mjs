/** Offline integration using installed ToolRuntime, ApprovalService and MCP discovery. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { Context } from '@deepseek-ai/cordis';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import SandboxedFileSystem from '@deepseek-ai/dsh-fs-sandbox';
import ApprovalService from '@deepseek-ai/dsh-user-approval';
import { createConnection } from '@playwright/mcp';
import { Client, InMemoryTransport } from '@modelcontextprotocol/client';
import { prepareProfile } from '../prepare-profile.mjs';
import { assertModelBrowserCall, isModelBrowserTool, isReadOnlyInteraction } from '../browser-action-policy.mjs';

async function daemonFixture(target = { tag: 'button', text: '发送', is_submit: false }, form = null, contextFixture, baselineSource) {
  const source = baselineSource || await readFile(new URL('../browser-host.mjs', import.meta.url), 'utf8');
  const body = source.slice(source.indexOf('function evaluation('), source.indexOf('\nfunction authorized('));
  const calls = [], ledger = { reviews: [], downloads: [] };
  const raw = async (name, args) => {
    calls.push({ name, args });
    if (name !== 'browser_evaluate') return { content: [{ type: 'text', text: 'executed' }] };
    let value = args.function.includes('const controls =') ? { page_url: 'https://fixture.invalid', fields: [], private_values: [['hidden', 'hidden', 'v1']] }
      : args.function.includes('const t = e.closest') ? target : form;
    return { content: [{ type: 'text', text: '### Result\n' + JSON.stringify(value) }] };
  };
  const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
  // Original dispatch/pending/recheck implementations; only browser IO and disk IO are fixtures.
  const make = new Function('raw', 'ledger', 'hash', 'randomUUID', 'assertModelBrowserCall', 'isReadOnlyInteraction', 'isModelBrowserTool', 'contextFixture', `
    let context = contextFixture, headed = false, generation = 0, activeConversation;
    const stoppedTurns = new Set(), unsafeTurns = new Set(), downloads = new Set();
    const persist = async () => {}, text = value => ({ content: [{ type: 'text', text: JSON.stringify(value) }] });
    const fail = (code, message) => Object.assign(new Error(message), { code });
    ${body}
    return dispatch;
  `);
  const dispatch = make(raw, ledger, hash, randomUUID, assertModelBrowserCall, isReadOnlyInteraction, isModelBrowserTool, contextFixture);
  return { dispatch, calls, ledger, target };
}
const frame = (name, args = {}) => ({ conversation_id: 'fixture', method: 'call', name, arguments: args });
const value = result => JSON.parse(result.content[0].text);

test('profile defaults restrict file writes and use supported fail-closed approval', async () => {
  const home = await mkdtemp(join(tmpdir(), 'campus-profile-safety-'));
  try {
    await prepareProfile(home, { workspace: home });
    const patch = JSON.parse(await readFile(join(home, 'profiles/campus/cordis.patch.yml'), 'utf8'));
    assert.equal(patch.find(p => p.id === 'sandbox-policy').config.mode, 'workspace-write');
    assert.equal(patch.find(p => p.id === 'approval').config.policy, 'ask');
  } finally { await rm(home, { recursive: true, force: true }); }
});

test('installed approval with no answerer returns unavailable promptly with paired audit events', async () => {
  const ctx = new Context(), events = [{ type: 'turn/start', data: {} }];
  const session = { id: 'fixture', get seq() { return events.length; }, eventAt: seq => events[seq],
    append: (type, data) => { events.push({ type, data }); } };
  const agent = { ctx, session };
  try {
    const approval = new ApprovalService(ctx, { policy: 'ask' });
    const outcome = await Promise.race([
      approval.request({ agent, toolName: 'pwsh', callId: 'fixture', reason: 'out-of-workspace', signal: new AbortController().signal }),
      new Promise((_, reject) => { const timer = setTimeout(() => reject(new Error('approval hung')), 1000); timer.unref(); }),
    ]);
    assert.equal(outcome, 'unavailable');
    assert.deepEqual(events.slice(1).map(e => e.type), ['approval/asked', 'approval/decided']);
    assert.equal(events[2].data.outcome, 'unavailable');
  } finally { await ctx.fiber.dispose(); }
});

test('installed Playwright MCP catalog omits script and file-exfiltration tools without launching browser', async () => {
  const server = await createConnection({ browser: { browserName: 'chromium', isolated: false }, webmcp: true }, () => { throw new Error('No browser in audit'); });
  const [local, remote] = InMemoryTransport.createLinkedPair();
  await server.connect(remote);
  const client = new Client({ name: 'campus-safety-audit', version: '1' }, { capabilities: {} });
  try {
    await client.connect(local);
    const { tools } = await client.listTools();
    assert(tools.some(t => t.name === 'browser_evaluate'));
    const visible = tools.filter(t => isModelBrowserTool(t.name)).map(t => t.name);
    assert(visible.includes('browser_snapshot') && visible.includes('browser_click'));
    for (const name of ['browser_evaluate', 'browser_run_code_unsafe', 'browser_file_upload', 'browser_drop', 'browser_drag']) assert(!visible.includes(name));
  } finally { await client.close(); await server.close(); }
});

test('installed ToolRuntime reports blocked browser execution as actionable error without IO', async () => {
  const ctx = new Context(), fixture = await daemonFixture();
  try {
    new SystemPrompt(ctx, { includeHarnessIdentity: false });
    const tools = new ToolRuntime(ctx, { mode: 'native' });
    for (const name of ['browser_evaluate', 'browser_run_code_unsafe', 'browser_file_upload', 'browser_drop', 'browser_drag']) {
      tools.register({ name, description: 'blocked fixture', parameters: { type: 'object' },
        output: { schema: { type: 'object' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
        async execute(args) { return fixture.dispatch(frame(name, args)); } });
      const result = await tools.execute({ callId: name, name, arguments: {}, signal: new AbortController().signal });
      assert.equal(result.isError, true, name);
      assert.match(result.content[0].text, /确认/);
    }
    assert.equal(fixture.calls.length, 0);
  } finally { await ctx.fiber.dispose(); }
});

test('unknown click and Enter prepare one review and replay confirmation does not submit twice', async () => {
  for (const name of ['browser_click', 'browser_press_key']) {
    const f = await daemonFixture();
    const args = name === 'browser_click' ? { target: 'button-ref' } : { key: 'Enter' };
    const first = value(await f.dispatch(frame(name, args)));
    assert.equal(first.status, 'needs_user');
    assert.equal(value(await f.dispatch(frame(name, args))).action_id, first.action_id);
    assert.equal(f.calls.filter(c => c.name === name).length, 0);
    assert.equal(f.ledger.reviews.length, 1);
    const confirm = { conversation_id: 'fixture', method: 'confirm', action_id: first.action_id };
    assert.equal((await f.dispatch(confirm)).action.status, 'executed');
    assert.equal((await f.dispatch(confirm)).action.status, 'executed');
    assert.equal(f.calls.filter(c => c.name === name).length, 1);
  }
});

test('changed target invalidates approval before execution', async () => {
  const f = await daemonFixture();
  const first = value(await f.dispatch(frame('browser_click', { target: 'button-ref' })));
  f.target.text = 'changed recipient';
  await assert.rejects(f.dispatch({ conversation_id: 'fixture', method: 'confirm', action_id: first.action_id }), { code: 'BROWSER_ACTION_CHANGED' });
  assert.equal(f.ledger.reviews[0].status, 'stale');
  assert.equal(f.calls.filter(c => c.name === 'browser_click').length, 0);
});

test('ordinary links, exact pagination and GET search remain automatic; unknown scripts do not', async () => {
  for (const target of [{ tag: 'a', href: 'https://fixture.invalid/courses?page=2', text: '课程' },
    { tag: 'button', text: '下一页', is_submit: false }]) {
    const f = await daemonFixture(target);
    await f.dispatch(frame('browser_click', { target: 'nav' }));
    assert.equal(f.ledger.reviews.length, 0);
    assert.equal(f.calls.filter(c => c.name === 'browser_click').length, 1);
  }
  assert.equal(isReadOnlyInteraction({ tag: 'a', href: 'javascript:send()', text: '下一页' }), false);
  assert.equal(isReadOnlyInteraction({ tag: 'button', text: '下一页', scripted: true }), false);
  const f = await daemonFixture({}, { page_url: 'https://fixture.invalid', read_only_search: true, private_values: [], fields: [] });
  await f.dispatch(frame('browser_type', { target: 'search', text: '公告', submit: true }));
  assert.equal(f.ledger.reviews.length, 0);
  assert.equal(f.calls.filter(c => c.name === 'browser_press_key').length, 1);
});

test('Space activates unknown controls only after confirmation; rejecting dialogs remains automatic', async () => {
  const f = await daemonFixture();
  assert.equal(value(await f.dispatch(frame('browser_press_key', { key: 'Space' }))).status, 'needs_user');
  await assert.rejects(f.dispatch(frame('browser_handle_dialog', { accept: true })), { code: 'BROWSER_APPROVAL_REQUIRED' });
  await f.dispatch(frame('browser_handle_dialog', { accept: false }));
  for (const args of [{ url: 'javascript:send()' }, { url: 'data:text/html,submit' }, { url: 'file:///secrets' }]) {
    await assert.rejects(f.dispatch(frame('browser_navigate', args)), { code: 'BROWSER_APPROVAL_REQUIRED' });
  }
  await assert.rejects(f.dispatch(frame('browser_new_unsafe_tool')), { code: 'BROWSER_APPROVAL_REQUIRED' });
});

test('password and OTP fields cannot be filled by model; no secret-bearing write occurs', async () => {
  for (const name of ['browser_type', 'browser_fill_form']) {
    const f = await daemonFixture({ tag: 'input', sensitive: true, text: '[manual authentication]' });
    const args = name === 'browser_type' ? { target: 'auth', text: 'synthetic-secret' } :
      { fields: [{ target: 'auth', name: 'password', type: 'textbox', value: 'synthetic-secret' }] };
    await assert.rejects(f.dispatch(frame(name, args)), { code: 'BROWSER_AUTH_REQUIRED' });
    assert.equal(f.calls.filter(c => c.name === name).length, 0);
    assert(!JSON.stringify(f.calls).includes('synthetic-secret'));
  }
});

test('installed filesystem allows task workspace write but denies outside it', async () => {
  const ctx = new Context();
  const home = await mkdtemp(join(tmpdir(), 'campus-fs-safety-'));
  try {
    const workspace = join(home, 'workspace');
    const { mkdir } = await import('node:fs/promises');
    await mkdir(workspace);
    const policy = { mode: 'workspace-write', workspaceRoot: workspace };
    ctx.provide('sandboxPolicy');
    ctx.set('sandboxPolicy', { defaultMode: policy.mode, resolve: () => policy });
    const fs = new SandboxedFileSystem(ctx, SandboxedFileSystem.Config({ cwd: workspace }));
    const within = await fs.resolve(join(workspace, 'result.txt'));
    await fs.writeText(within, 'offline-task-result');
    assert.equal(await readFile(join(workspace, 'result.txt'), 'utf8'), 'offline-task-result');
    // cwd is in temp, and upstream explicitly also allows OS temp roots.
    // Use a synthetic path under project to prove denial, never write it.
    const outside = await fs.resolve(new URL('../../../.harness-must-not-create', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
    await assert.rejects(fs.writeText(outside, 'denied'), { code: 'FS_SANDBOX_DENIED' });
  } finally { await ctx.fiber.dispose(); await rm(home, { recursive: true, force: true }); }
});

test('private campus session does not launch browser or expose non-campus domains', async () => {
  const empty = await daemonFixture();
  const privateFrame = { conversation_id: 'campus-session', method: 'campus-session' };
  await assert.rejects(empty.dispatch(privateFrame), { code: 'BROWSER_AUTH_REQUIRED' });
  assert.equal(empty.calls.length, 0);
  const context = { cookies: async () => [
    { domain: '.zjuam.zju.edu.cn', name: 'CAS', value: 'private-fixture' },
    { domain: 'zdbk.zju.edu.cn', name: 'sid', value: 'private-fixture' },
    { domain: '.zju.edu.cn', name: 'parent', value: 'private-fixture' },
    { domain: 'other.zju.edu.cn', name: 'other', value: 'must-not-hand-off' },
    { domain: 'evilzju.edu.cn', name: 'bad', value: 'must-not-hand-off' },
  ] };
  const f = await daemonFixture(undefined, null, context);
  assert.deepEqual((await f.dispatch(privateFrame)).cookies.map(c => c.name), ['CAS', 'sid', 'parent']);
  await assert.rejects(f.dispatch({ ...privateFrame, conversation_id: 'model-conversation' }), { code: 'BROWSER_DENIED' });
  await assert.rejects(f.dispatch({ ...privateFrame, turn_id: 'model-turn' }), { code: 'BROWSER_DENIED' });
});

test('network detail inspection never exposes request payloads or cookie-bearing headers', async () => {
  const f = await daemonFixture();
  for (const part of [undefined, 'request-body', 'request-headers', 'response-headers']) {
    await assert.rejects(f.dispatch(frame('browser_network_request', { index: 1, ...(part ? { part } : {}) })), { code: 'BROWSER_APPROVAL_REQUIRED' });
  }
  assert.equal(f.calls.length, 0);
  await f.dispatch(frame('browser_network_request', { index: 1, part: 'response-body' }));
  assert.equal(f.calls.length, 1);
});

test('before/after deterministic dispatch metrics use actual git HEAD code, not text matching', async () => {
  const snapshot = JSON.parse(await readFile(new URL('./fixtures/browser-host-before.json', import.meta.url), 'utf8'));
  const baseline = snapshot.source;
  const measures = {};
  for (const [phase, source] of [['before_HEAD', baseline], ['after_worktree', undefined]]) {
    const f = await daemonFixture(undefined, null, undefined, source);
    const response = await f.dispatch(frame('browser_click', { target: 'send-ref' }));
    measures[phase] = { unknown_send_raw_calls: f.calls.filter(c => c.name === 'browser_click').length,
      pending_review_count: f.ledger.reviews.length, result_needs_user: response.content[0].text.startsWith('{') && value(response)?.status === 'needs_user' };
  }
  assert.deepEqual(measures.before_HEAD, { unknown_send_raw_calls: 1, pending_review_count: 0, result_needs_user: false });
  assert.deepEqual(measures.after_worktree, { unknown_send_raw_calls: 0, pending_review_count: 1, result_needs_user: true });
  console.log('SAFETY_DISPATCH_METRICS=' + JSON.stringify({ baseline_commit: snapshot.commit, ...measures }));
});

test('new-tab navigation cannot bypass javascript/data/file URL denial', async () => {
  const f = await daemonFixture();
  for (const url of ['javascript:send()', 'data:text/html,submit', 'file:///secrets']) {
    await assert.rejects(f.dispatch(frame('browser_tabs', { action: 'new', url })), { code: 'BROWSER_APPROVAL_REQUIRED' });
  }
  assert.equal(f.calls.length, 0);
  await f.dispatch(frame('browser_tabs', { action: 'new', url: 'https://fixture.invalid' }));
  assert.equal(f.calls.length, 1);
});

test('Playwright output filename cannot escape the owned output directory', async () => {
  const f = await daemonFixture();
  for (const filename of ['../../outside.txt', 'C:\\private.txt', '/etc/private', '..', 'x/y.txt']) {
    await assert.rejects(f.dispatch(frame('browser_snapshot', { filename })), { code: 'BROWSER_APPROVAL_REQUIRED' });
  }
  assert.equal(f.calls.length, 0);
  await f.dispatch(frame('browser_snapshot', { filename: 'page.yml' }));
  assert.equal(f.calls.length, 1);
});
