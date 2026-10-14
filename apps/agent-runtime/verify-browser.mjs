/** Real Edge + real Playwright MCP, local pages; no model/school/paid service. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomBytes } from 'node:crypto';

const root = fileURLToPath(new URL('../../', import.meta.url));
const directory = await mkdtemp(resolve(root, 'var/v2-browser-'));
const credential = 'browser-fixture-cookie-920637';
const password = 'fixture-password-663851';
const submitted = [];
let cookieVerified = false;
let slowStarted;
const slowTimers = new Set();
const fixture = createServer(async (req, res) => {
  if (req.url.startsWith('/search')) {
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    return res.end(req.url.includes('?') ? '<h1>找到公开资料-922</h1>' :
      '<form action="/search" method="get" role="search"><input id="query" name="q" type="search"><button>搜索</button></form>');
  }
  if (req.url === '/slow') {
    slowStarted?.();
    const timer = setTimeout(() => { slowTimers.delete(timer); res.end('<p>slow response</p>'); }, 10000);
    slowTimers.add(timer);
    res.on('close', () => { clearTimeout(timer); slowTimers.delete(timer); });
    return;
  }
  if (req.url === '/login') {
    res.setHeader('Set-Cookie', `session=${credential}; HttpOnly; SameSite=Lax; Max-Age=86400; Path=/`);
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    return res.end('<title>本地测试登录</title><a href="/form">继续到表单</a>');
  }
  if (req.url === '/attachment') {
    res.setHeader('Content-Disposition', 'attachment; filename="campus-fixture.txt"');
    return res.end('browser-download-record-773\n');
  }
  if (req.url === '/submit') {
    const chunks = []; for await (const c of req) chunks.push(c);
    submitted.push(new URLSearchParams(Buffer.concat(chunks).toString()).get('student'));
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    return res.end('<title>提交回执</title><p>已收到测试材料</p><a href="/form">返回表单</a>');
  }
  if (req.url === '/form') {
    cookieVerified = req.headers.cookie?.includes(credential) || false;
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    return res.end(`<title>校园测试材料</title><h1>${cookieVerified ? '登录状态已保留' : '未登录'}</h1>
      <a id="download" href="/attachment">下载附件</a>
      <form action="/submit" method="post"><label>姓名<input id="student" name="student"></label>
      <label>口令<input id="password" type="password" name="password" value="${password}"></label>
      <input type="hidden" name="csrf" value="${credential}"><button id="submit">提交材料</button></form>
      <textarea id="message" aria-label="消息">初始消息</textarea><button id="send" type="button"
        onclick="fetch('/submit',{method:'POST',body:'student='+encodeURIComponent(document.getElementById('message').value)}).then(()=>this.innerText='发送已完成')">发送消息</button>`);
  }
  res.statusCode = 404; res.end('missing');
});
await new Promise(done => fixture.listen(0, '127.0.0.1', done));
const url = `http://127.0.0.1:${fixture.address().port}`;
let host;
const evidence = { kind: 'actual_owned_edge_browser_no_model', checks: [], tool_names: [] };
async function start() {
  const token = randomBytes(32).toString('hex');
  const proc = spawn(process.execPath, ['browser-host.mjs'], {
    cwd: fileURLToPath(new URL('.', import.meta.url)), windowsHide: true,
    env: { ...process.env, CAMPUS_BROWSER_HOME: resolve(directory, 'browser'), CAMPUS_BROWSER_TOKEN: token },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  let diagnostic = '';
  proc.stderr.on('data', c => { diagnostic += c; });
  const address = await new Promise((done, reject) => {
    let input = '';
    const timer = setTimeout(() => reject(new Error('Browser host readiness timeout')), 30000);
    proc.once('exit', code => { clearTimeout(timer); reject(new Error(`Host exited ${code}: ${diagnostic}`)); });
    proc.stdout.on('data', chunk => {
      input += chunk;
      if (input.includes('\n')) { clearTimeout(timer); done(JSON.parse(input.split('\n')[0]).url); }
    });
  });
  host = { proc, address, token };
}
async function rpc(method, fields = {}, ok = true) {
  const response = await fetch(host.address + '/rpc', {
    method: 'POST', headers: { Authorization: 'Bearer ' + host.token, 'Content-Type': 'application/json' },
    body: JSON.stringify({ conversation_id: 'browser_fixture', method, ...fields }), signal: AbortSignal.timeout(150000),
  });
  const result = await response.json();
  assert.equal(result.ok, ok, JSON.stringify(result));
  return ok ? result.result : result;
}
const call = (name, args = {}) => rpc('call', { name, arguments: args });
const plain = result => result.content.filter(b => b.type === 'text').map(b => b.text).join('\n');
async function close() {
  if (!host) return;
  const owned = host; host = undefined;
  const end = new Promise((done, reject) => {
    const timer = setTimeout(() => { owned.proc.kill(); reject(new Error('Owned host did not close')); }, 15000);
    owned.proc.once('exit', code => { clearTimeout(timer); assert.equal(code, 0); done(); });
  });
  owned.proc.stdin.end();
  await end;
}
try {
  await start();
  const denied = await fetch(host.address + '/rpc', { method: 'POST', body: '{}' });
  assert.equal(denied.status, 403);
  evidence.checks.push('loopback_rpc_requires_private_authority');
  evidence.tool_names = (await rpc('tools')).tools.map(t => t.name);
  assert(evidence.tool_names.includes('browser_fill_form'));
  assert(evidence.tool_names.includes('browser_prepare_action'));
  await call('browser_navigate', { url: url + '/search' });
  assert.match(plain(await call('browser_type', { target: '#query', text: '公开资料', submit: true })), /找到公开资料-922/);
  assert.equal((await rpc('status')).reviews.length, 0);
  evidence.checks.push('ordinary_get_search_executes_without_confirmation');
  await call('browser_navigate', { url: url + '/login' });
  const form = await call('browser_navigate', { url: url + '/form' });
  assert.match(plain(form), /登录状态已保留/);
  assert(!plain(form).includes(password));
  assert(!plain(form).includes(credential));
  await call('browser_fill_form', { fields: [{ target: '#student', name: '姓名', type: 'textbox', value: '测试学生-819' }] });
  const action = JSON.parse(plain(await call('browser_click', { target: '#submit' })));
  assert.equal(action.status, 'needs_user');
  // A real school search stalled here: the model waited with browser_wait_for for ~70 s and
  // burned its turn budget instead of ending the turn for the user to confirm.
  assert.match(action.message, /立即结束本轮/);
  assert.match(action.message, /不要用 browser_wait_for 空等/);
  assert.equal(action.preview.action_url, url + '/submit');
  assert.equal(action.preview.method, 'post');
  assert.equal(submitted.length, 0);
  assert.match(JSON.stringify(action.preview), /测试学生-819/);
  assert(!JSON.stringify(action).includes(password));
  assert(!JSON.stringify(action).includes(credential));
  const confirmed = await rpc('confirm', { action_id: action.action_id });
  assert.equal(confirmed.action.status, 'executed');
  assert.deepEqual(submitted, ['测试学生-819']);
  assert.equal((await rpc('confirm', { action_id: action.action_id })).action.status, 'executed');
  assert.equal(submitted.length, 1);
  evidence.checks.push('fill_then_concrete_review_one_confirmation_no_duplicate_submit');
  await call('browser_navigate', { url: url + '/form' });
  await call('browser_fill_form', { fields: [{ target: '#student', name: '姓名', type: 'textbox', value: '变更前' }] });
  const stale = JSON.parse(plain(await call('browser_click', { target: '#submit' })));
  await call('browser_fill_form', { fields: [{ target: '#student', name: '姓名', type: 'textbox', value: '变更后' }] });
  assert.equal((await rpc('confirm', { action_id: stale.action_id }, false)).code, 'BROWSER_ACTION_CHANGED');
  assert.equal(submitted.length, 1);
  evidence.checks.push('changed_form_requires_fresh_review');
  const passwordReview = JSON.parse(plain(await call('browser_click', { target: '#submit' })));
  await call('browser_type', { target: '#password', text: 'changed-fixture-password-283' });
  assert.equal((await rpc('confirm', { action_id: passwordReview.action_id }, false)).code, 'BROWSER_ACTION_CHANGED');
  assert.equal(submitted.length, 1);
  evidence.checks.push('password_change_invalidates_review_without_exposing_password');
  const downloaded = await call('browser_click', { target: '#download' });
  assert(!downloaded.isError, plain(downloaded));
  const status = await rpc('status');
  assert.equal(status.downloads.length, 1);
  assert.equal(await readFile(status.downloads[0].path, 'utf8'), 'browser-download-record-773\n');
  evidence.checks.push('actual_attachment_download_matches_bytes');
  const enter = JSON.parse(plain(await call('browser_type', { target: '#student', text: '回车提交-284', submit: true })));
  assert.equal(enter.status, 'needs_user');
  assert.equal(submitted.length, 1);
  assert.equal((await rpc('confirm', { action_id: enter.action_id })).action.status, 'executed');
  assert.deepEqual(submitted, ['测试学生-819', '回车提交-284']);
  evidence.checks.push('type_with_enter_prepares_one_review_before_submission');
  await call('browser_navigate', { url: url + '/form' });
  await call('browser_evaluate', { function: '() => localStorage.setItem("access_token", "fixture-storage-token-1837")' });
  assert(!plain(await call('browser_evaluate', { function: '() => localStorage.getItem("access_token")' })).includes('fixture-storage-token-1837'));
  evidence.checks.push('authentication_storage_values_redacted_from_tool_results');
  const explicitArgs = { target: '#send', description: '发送本地测试消息', recipient: url + '/submit', content: 'JS消息-339' };
  await call('browser_type', { target: '#message', text: explicitArgs.content });
  const jsReview = JSON.parse(plain(await call('browser_prepare_action', explicitArgs)));
  assert(jsReview.preview.fields.some(f => f.name === '消息' && f.value === explicitArgs.content));
  await call('browser_type', { target: '#message', text: 'JS消息已变更-338' });
  assert.equal((await rpc('confirm', { action_id: jsReview.action_id }, false)).code, 'BROWSER_ACTION_CHANGED');
  assert.equal(submitted.length, 2);
  const jsChanged = JSON.parse(plain(await call('browser_prepare_action', { ...explicitArgs, content: 'JS消息已变更-338' })));
  assert.equal((await rpc('confirm', { action_id: jsChanged.action_id })).action.status, 'executed');
  assert.equal((await rpc('confirm', { action_id: jsChanged.action_id })).action.status, 'executed');
  assert.deepEqual(submitted, ['测试学生-819', '回车提交-284', 'JS消息已变更-338']);
  evidence.checks.push('explicit_javascript_action_reviews_actual_fields_and_rejects_changed_content');
  await call('browser_evaluate', { function: '() => localStorage.setItem("fixtureMarker", "persistent-882")' });
  await close();
  await start();
  const restored = await call('browser_navigate', { url: url + '/form' });
  assert.match(plain(restored), /登录状态已保留/);
  assert.equal(cookieVerified, true);
  assert.match(plain(await call('browser_evaluate', { function: '() => localStorage.getItem("fixtureMarker")' })), /persistent-882/);
  assert.equal((await rpc('confirm', { action_id: action.action_id })).action.status, 'executed');
  assert.equal(submitted.length, 3);
  evidence.checks.push('owned_host_restart_preserves_cookie_storage_and_submission_receipt');
  await rpc('stop');
  assert.equal((await rpc('status')).running, false);
  await call('browser_navigate', { url: url + '/form' });
  assert.equal((await rpc('status')).running, true);
  evidence.checks.push('stop_closes_owned_context_then_reopen_reuses_profile');
  const navigationStarted = new Promise(done => { slowStarted = done; });
  const navigating = rpc('call', { name: 'browser_navigate', arguments: { url: url + '/slow' }, turn_id: 'slow-turn' }, false);
  await navigationStarted;
  const cancelStarted = performance.now();
  assert.equal((await rpc('stop', { turn_id: 'other-turn' })).interrupted, false);
  assert.equal((await rpc('stop', { turn_id: 'slow-turn' })).interrupted, true);
  assert.equal((await navigating).code, 'BROWSER_STOPPED');
  assert(performance.now() - cancelStarted < 5000, 'Browser stop waited for the slow page response');
  assert.equal((await rpc('status')).running, false);
  evidence.checks.push('scoped_stop_closes_actual_page_and_cancels_navigation_without_stopping_another_turn');
  const output = resolve(directory, 'browser/outputs');
  const { readdir } = await import('node:fs/promises');
  for (const name of await readdir(output)) if (/\.(yml|yaml|log|txt|json)$/.test(name)) {
    const content = await readFile(resolve(output, name), 'utf8');
    assert(!content.includes(password) && !content.includes(credential) && !content.includes('changed-fixture-password-283'));
  }
  evidence.checks.push('model_visible_snapshot_files_do_not_contain_test_credentials');
  assert(!JSON.stringify(evidence).includes(credential));
  assert(!JSON.stringify(evidence).includes(password));
  await writeFile(resolve(directory, 'evidence.json'), JSON.stringify(evidence, null, 2));
  console.log(JSON.stringify({ ok: true, evidence: resolve(directory, 'evidence.json'), checks: evidence.checks }));
} finally {
  await close();
  for (const timer of slowTimers) clearTimeout(timer);
  await new Promise(done => fixture.close(done));
}
