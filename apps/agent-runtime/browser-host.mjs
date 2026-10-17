/** Owned persistent browser. Public Playwright + MCP APIs; no model loop. */
import { createServer } from 'node:http';
import { createHash, createHmac, randomUUID, timingSafeEqual } from 'node:crypto';
import { mkdir, readFile, writeFile, rename, readdir } from 'node:fs/promises';
import { basename, isAbsolute, resolve } from 'node:path';
import { existsSync, writeSync } from 'node:fs';
import { chromium } from 'playwright';
import { createConnection } from '@playwright/mcp';
import { Client, InMemoryTransport } from '@modelcontextprotocol/client';
import { assertModelBrowserCall, isReadOnlyInteraction, isModelBrowserTool, isSensitiveElement,
  SENSITIVE_SELECTOR, isAuthenticationURL, createNetworkGate, effectFingerprint } from './browser-action-policy.mjs';
const networkGate = createNetworkGate();

const home = resolve(process.env.CAMPUS_BROWSER_HOME);
const token = process.env.CAMPUS_BROWSER_TOKEN;
if (!token || token.length < 32) throw new Error('Missing browser authority');
await mkdir(home, { recursive: true });
// Model-visible browser files (screenshots/PDFs/console dumps) must land inside the
// conversation workspace: the capability policy refuses reads from the private browser
// home, so the default output directory is the workspace when the API provides one.
const clientWorkspace = process.env.CAMPUS_BROWSER_WORKSPACE
  ? resolve(process.env.CAMPUS_BROWSER_WORKSPACE) : resolve(home, 'outputs');
const clientOutputDir = process.env.CAMPUS_BROWSER_WORKSPACE
  ? resolve(clientWorkspace, 'browser-outputs') : clientWorkspace;
await mkdir(clientOutputDir, { recursive: true });
const ledgerPath = resolve(home, 'actions.json');
let ledger;
try { ledger = JSON.parse(await readFile(ledgerPath, 'utf8')); }
catch (error) { if (error.code !== 'ENOENT') throw error; ledger = { reviews: [], downloads: [] }; }
for (const item of ledger.reviews) if (item.status === 'executing') item.status = 'uncertain';
let context, client, activeConversation, headed = false, generation = 0;
let activeOperation;
const stoppedTurns = new Set();
const unsafeTurns = new Set();
let chain = Promise.resolve();
const downloads = new Set();
const observedSecrets = new Set();
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const activityFrame = data => writeSync(1, '\n' + JSON.stringify({ kind: 'activity', data,
  proof: createHmac('sha256', token).update(JSON.stringify(data)).digest('hex') }) + '\n');
const persist = async () => {
  const temp = ledgerPath + '.tmp';
  await writeFile(temp, JSON.stringify(ledger), { mode: 0o600 });
  await rename(temp, ledgerPath);
};
await persist();
const text = value => ({ content: [{ type: 'text', text: JSON.stringify(value) }] });
const fail = (code, message) => Object.assign(new Error(message), { code });

async function launch() {
  if (context) return context;
  const proxy = process.env.CAMPUS_BROWSER_PROXY;
  const channel = process.env.CAMPUS_BROWSER_CHANNEL || 'msedge';
  const installedEdge = channel === 'msedge' ? ['ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'].map(k =>
    process.env[k] && resolve(process.env[k], 'Microsoft/Edge/Application/msedge.exe')).find(p => p && existsSync(p)) : undefined;
  const executablePath = process.env.CAMPUS_BROWSER_EXECUTABLE || installedEdge;
  context = await chromium.launchPersistentContext(resolve(home, 'profile'), {
    channel, headless: !headed,
    ...(executablePath ? { executablePath } : {}),
    ...(proxy ? { proxy: { server: proxy } } : {}),
    acceptDownloads: true, serviceWorkers: 'block', viewport: { width: 1280, height: 900 },
  });
  context.setDefaultTimeout(10000);
  context.setDefaultNavigationTimeout(60000);
  context.on('close', () => { context = undefined; });
  // Install before exposing a page to the model. Context routing also covers
  // popups, redirects, fetch/XHR and sendBeacon; service workers are disabled.
  await context.route('**/*', async route => {
    const request = route.request();
    const allowed = networkGate.decide({ method: request.method(), url: request.url(),
      body: request.postData() || '', resourceType: request.resourceType(), navigation: request.isNavigationRequest() });
    if (allowed) await route.continue(); else await route.abort('blockedbyclient');
  });
  // WebSocket writes cannot be matched to reviewed HTTP payloads. Block model
  // sockets (including messages from sockets opened earlier in human mode).
  if (context.routeWebSocket) await context.routeWebSocket('**/*', ws => {
    if (networkGate.guarded) { ws.close(); return; }
    const remote = ws.connectToServer();
    ws.onMessage(message => { if (networkGate.guarded) ws.close(); else remote.send(message); });
    remote.onMessage(message => ws.send(message));
  });
  context.on('request', request => {
    for (const [name, value] of Object.entries(request.headers())) {
      if (/^(authorization|proxy-authorization|x-api-key|api-key|x-auth-token)$/i.test(name) && value.length >= 8) {
        observedSecrets.add(value);
        if (/^Bearer /i.test(value)) observedSecrets.add(value.slice(7));
      }
    }
  });
  const watch = page => page.on('download', download => {
    const cid = activeConversation;
    if (!cid) return;
    const pending = (async () => {
      const id = randomUUID();
      const name = basename(download.suggestedFilename()).replace(/[<>:"/\\|?*\x00-\x1f]/g, '_') || 'download.bin';
      const directory = resolve(home, 'downloads', cid, id);
      await mkdir(directory, { recursive: true });
      const path = resolve(directory, name);
      await download.saveAs(path);
      if (await download.failure()) throw fail('BROWSER_DOWNLOAD_FAILED', '附件下载失败');
      ledger.downloads.push({ id, conversation_id: cid, name, path, created_at: new Date().toISOString() });
    })();
    downloads.add(pending);
    pending.catch(() => {}).finally(() => downloads.delete(pending));
  });
  context.pages().forEach(watch);
  context.on('page', watch);
  return context;
}

async function connect() {
  if (client) return client;
  const upstream = await createConnection({
    browser: { browserName: 'chromium', isolated: false },
    outputDir: clientOutputDir, imageResponses: 'allow', filePaths: 'absolute',
    allowUnrestrictedFileAccess: true, saveSession: false, webmcp: true,
    console: { level: 'error' }, timeouts: { action: 10000, navigation: 60000 },
  }, launch);
  const [local, remote] = InMemoryTransport.createLinkedPair();
  await upstream.connect(remote);
  client = new Client({ name: 'campus-owned-browser', version: '1.0.0' }, {
    capabilities: {}, versionNegotiation: { mode: 'auto' },
  });
  await client.connect(local);
  return client;
}

/** Resolve every model-chosen file output inside the conversation workspace.
 *
 * Playwright MCP resolves relative filenames against the caller's workspace root and
 * honours absolute names as-is. Both would let a screenshot/PDF land outside the
 * capability scope (or overwrite source files), so absolute names are reduced to their
 * basename and `_meta.cwd` pins the workspace root for the upstream tool.
 */
function clientArgs(args = {}) {
  const forwarded = { ...args, _meta: { ...(args._meta ?? {}), cwd: clientOutputDir } };
  if (typeof forwarded.filename === 'string' && isAbsolute(forwarded.filename)) {
    forwarded.filename = basename(forwarded.filename);
  }
  return forwarded;
}

async function raw(name, args = {}) {
  const upstream = await connect();
  const forwarded = clientArgs(args);
  if (name !== 'browser_take_screenshot') return upstream.callTool({ name, arguments: forwarded }, undefined, { timeout: 120000 });
  const pages = context.pages();
  if (pages.some(page => isAuthenticationURL(page.url()))) throw fail('BROWSER_AUTH_REQUIRED',
    '认证页面不向模型提供未脱敏截图，请由用户在浏览器中完成登录/MFA后读取普通页面。');
  const masked = [];
  try {
    for (const page of pages) {
      // Mask DOM before upstream captures/writes ANY image, not after base64 IO.
      await page.locator(SENSITIVE_SELECTOR).evaluateAll(elements => {
        for (const e of elements) {
          if (e.hasAttribute('data-campus-secret-mask')) continue;
          e.setAttribute('data-campus-secret-mask', e.getAttribute('style') || '');
          e.style.setProperty('color', 'transparent', 'important');
          e.style.setProperty('text-shadow', 'none', 'important');
          e.style.setProperty('background', '#111', 'important');
          e.style.setProperty('-webkit-text-security', 'disc', 'important');
        }
      });
      masked.push(page);
    }
    return await upstream.callTool({ name, arguments: forwarded }, undefined, { timeout: 120000 });
  } finally {
    for (const page of masked) await page.locator('[data-campus-secret-mask]').evaluateAll(elements => {
      for (const e of elements) { e.setAttribute('style', e.getAttribute('data-campus-secret-mask')); e.removeAttribute('data-campus-secret-mask'); }
    }).catch(() => {});
  }
}

function evaluation(result) {
  if (result.isError) throw fail('BROWSER_TARGET_CHANGED', '页面或目标已改变，请重新读取页面');
  const message = result.content.filter(b => b.type === 'text').map(b => b.text).join('\n');
  const match = /### Result\s*\n([\s\S]*?)(?:\n### |$)/.exec(message);
  if (!match) throw fail('BROWSER_REVIEW_FAILED', '无法读取提交预览');
  return JSON.parse(match[1].trim());
}

// Inspect with owned code only. Arbitrary model scripts are rejected at dispatch.
const inspectForm = element => {
  const target = element || document.activeElement;
  const button = target?.closest('button,input[type=submit],input[type=image]');
  const form = target?.form || target?.closest('form');
  if (!form) return null;
  const visible = item => item.type !== 'hidden' && item.type !== 'password' && !/^(current-password|new-password|one-time-code)$/.test(item.autocomplete || '') && !item.disabled &&
    (!['submit', 'button', 'reset', 'image'].includes(item.type) || (item === button && item.name));
  const fields = [...form.elements].filter(visible).map(item => ({
    name: item.labels?.[0]?.innerText?.trim() || item.name || item.id || item.type,
    value: item.type === 'file' ? [...item.files].map(f => f.name).join(', ') :
      ['checkbox', 'radio'].includes(item.type) ? String(item.checked) : item.value || '',
  }));
  const action_url = button?.hasAttribute('formaction') ? button.formAction : form.action;
  const method = button?.hasAttribute('formmethod') ? button.formMethod : form.method;
  const buttonText = button?.innerText?.trim() || button?.value || '';
  return {
    page_url: location.href, action_url, method, fields,
    read_only_search: method.toLowerCase() === 'get' &&
      (form.getAttribute('role') === 'search' || form.querySelector('input[type=search]') || /搜索|查询|查找|search|find/i.test(buttonText)) &&
      !/提交|发送|报名|购买|支付|submit|send|register|purchase|pay/i.test(buttonText) ? true : false,
    is_submit: !!button && ['submit', 'image'].includes(button.type),
    button: button?.innerText?.trim() || button?.value || '提交',
    private_values: [...form.elements].map(item => [item.name, item.type, item.value, item.checked]),
  };
};

async function formReview(target) {
  const value = evaluation(await raw('browser_evaluate', {
    ...(target ? { target, element: '准备提交的表单' } : {}),
    function: target ? `(${inspectForm.toString()})` : `() => (${inspectForm.toString()})(document.activeElement)`,
  }));
  if (value) { value.private_state = hash(value.private_values); delete value.private_values; }
  return value;
}

async function interactionTarget(target) {
  return evaluation(await raw('browser_evaluate', {
    ...(target ? { target, element: '检查操作目标' } : {}),
    function: `${target ? '(element)' : '()'} => {
      const e = ${target ? 'element' : 'document.activeElement'};
      if (!e) return null;
      const t = e.closest('a,button,input,[role="button"],[role="link"]') || e;
      const sensitive = t.type === 'password' || /^(current-password|new-password|one-time-code)$/.test(t.autocomplete || '');
      return { tag: t.tagName.toLowerCase(), role: t.getAttribute('role'), sensitive,
        text: sensitive ? '[由用户手动填写的认证字段]' : (t.innerText?.trim() || t.value || t.getAttribute('aria-label') || ''),
        href: t.href || null, method: t.getAttribute('data-method') || t.getAttribute('formmethod'),
        scripted: !!t.getAttribute('onclick'),
        editable: t.isContentEditable || t.tagName === 'TEXTAREA' ||
          (t.tagName === 'INPUT' && !['button','submit','image','checkbox','radio','file'].includes(t.type)),
        is_submit: ['submit','image'].includes(t.type) };
    }`,
  }));
}

async function explicitReview(args) {
  const state = evaluation(await raw('browser_evaluate', { function: `() => {
    const controls = [...document.querySelectorAll('input,textarea,select,[contenteditable="true"]')];
    const value = e => e.type === 'file' ? [...e.files].map(f => f.name).join(', ') :
      ['checkbox', 'radio'].includes(e.type) ? String(e.checked) : e.value ?? e.innerText ?? '';
    return { page_url: location.href,
      fields: controls.filter(e => e.type !== 'hidden' && e.type !== 'password' && !/^(current-password|new-password|one-time-code)$/.test(e.autocomplete || '') && !e.disabled && e.getClientRects().length)
        .map(e => ({ name: e.labels?.[0]?.innerText?.trim() || e.getAttribute('aria-label') || e.name || e.id || '内容', value: value(e) })),
      private_values: controls.map(e => [e.name || e.id, e.type, value(e)]) };
  }` }));
  const private_state = hash(state.private_values);
  delete state.private_values;
  const target_state = await interactionTarget(args.target);
  return { kind: 'explicit', description: args.description, recipient: args.recipient,
    content: args.content, attachments: args.attachments || [], ...state, private_state,
    target_text: target_state?.text || '', target_state,
  };
}

async function redact(result) {
  if (!context) return result;
  for (const value of (await context.cookies()).map(c => c.value).filter(v => v.length >= 12)) observedSecrets.add(value);
  for (const page of context.pages()) {
    try { for (const value of await page.locator('input[type=password]').evaluateAll(es => es.map(e => e.value)))
      if (value.length >= 4) observedSecrets.add(value); } catch { /* Navigation may replace this document. */ }
    try {
      for (const value of await page.evaluate(() => [localStorage, sessionStorage].flatMap(storage =>
        Object.keys(storage).filter(key => /token|secret|api.?key|password|session|auth/i.test(key)).map(key => storage.getItem(key)))))
        if (value && value.length >= 12) observedSecrets.add(value);
    } catch { /* Internal browser pages may have no web storage. */ }
  }
  const visit = value => {
    if (typeof value === 'string') { for (const secret of observedSecrets) value = value.replaceAll(secret, '[已隐藏凭据]'); return value; }
    if (Array.isArray(value)) return value.map(visit);
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) =>
      [k, value.type === 'image' && k === 'data' ? v : visit(v)]));
    return value;
  };
  // Upstream writes snapshots/logs in outputDir. Sanitize their contents too,
  // before a model-visible path can be read by ordinary file tools.
  const output = clientOutputDir;
  for (const entry of await readdir(output, { withFileTypes: true }).catch(() => [])) {
    if (!entry.isFile() || !/\.(yml|yaml|log|txt|json)$/.test(entry.name)) continue;
    const path = resolve(output, entry.name);
    const content = await readFile(path, 'utf8');
    const clean = visit(content);
    if (clean !== content) await writeFile(path, clean);
  }
  const clean = visit(result);
  for (const block of clean.content || []) {
    if (block.type !== 'text') continue;
    const link = /\[Snapshot\]\(([^)]+)\)/.exec(block.text);
    if (link && resolve(link[1]).startsWith(output + (process.platform === 'win32' ? '\\' : '/'))) {
      block.text += '\n### Page content\n' + visit(await readFile(resolve(link[1]), 'utf8'));
    }
  }
  return clean;
}

async function pending(cid, call, preview) {
  const { private_state, ...publicPreview } = preview;
  preview = await redact(publicPreview);
  const fingerprint = hash({ call, preview, private_state });
  let review = ledger.reviews.find(r => r.conversation_id === cid && r.fingerprint === fingerprint && r.status === 'pending');
  if (!review) {
    review = { id: randomUUID(), conversation_id: cid, status: 'pending', fingerprint,
      call, preview, private_state, created_at: new Date().toISOString(), result: null };
    ledger.reviews.push(review);
    await persist();
  }
  return text({ status: 'needs_user', action_id: review.id, preview: review.preview,
    message: '已准备具体操作，等待用户在浏览器工作区确认一次；不要用其他工具绕过确认。'
      + '现在请立即结束本轮并说明正在等待用户确认：不要用 browser_wait_for 空等（真实校方表单提交的'
      + '确认由用户在界面上完成，模型等多久都不会自动生效），也不要重复调用同一个操作。' });
}

async function stop() {
  generation++;
  const previousClient = client, previousContext = context;
  client = context = undefined;
  if (previousContext) await previousContext.close().catch(() => {});
  if (previousClient) await previousClient.close().catch(() => {});
}

async function dispatch(body) {
  const cid = body.conversation_id;
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(cid || '')) throw fail('INVALID_INPUT', '会话标识无效');
  if (body.turn_id && stoppedTurns.has(body.turn_id)) throw fail('BROWSER_STOPPED', '本轮浏览器操作已停止');
  if (body.method === 'tools') {
    const result = await (await connect()).listTools();
    return { tools: [...result.tools.filter(tool => isModelBrowserTool(tool.name)).map(tool =>
      tool.name === 'browser_network_request' ? { ...tool, description: tool.description + ' Campus policy: specify part=response-body; request payloads and cookie-bearing headers are not available to the model.' } : tool), { name: 'browser_prepare_action',
      description: 'Prepare a concrete external click/send action for one user confirmation. Use for non-form submit buttons.',
      inputSchema: { type: 'object', properties: { target: { type: 'string' }, description: { type: 'string' },
        recipient: { type: 'string' }, content: { type: 'string' }, attachments: { type: 'array', items: { type: 'string' } } },
        required: ['target', 'description', 'recipient', 'content'], additionalProperties: false } }] };
  }
  if (body.method === 'status') return redact({ running: !!context, headed,
    pages: context ? await Promise.all(context.pages().map(async (p, index) => ({ index, url: p.url(), title: await p.title().catch(() => '') }))) : [],
    reviews: ledger.reviews.filter(r => r.conversation_id === cid).map(({ call, fingerprint, private_state, ...rest }) => rest),
    downloads: ledger.downloads.filter(r => r.conversation_id === cid) });
  // Private owner bridge only: never registered as a model tool, no browser launch.
  // These bytes go directly to the API's reviewed campus adapter, never to status/logs.
  if (body.method === 'campus-session') {
    if (cid !== 'campus-session' || body.turn_id) throw fail('BROWSER_DENIED', '不支持的会话授权');
    if (!context) throw fail('BROWSER_AUTH_REQUIRED', '请先在浏览器窗口中完成校园登录');
    const allowed = new Set(['zjuam.zju.edu.cn', 'zdbk.zju.edu.cn', 'eta.zju.edu.cn', 'zju.edu.cn']);
    return { cookies: (await context.cookies()).filter(cookie => allowed.has(cookie.domain.replace(/^\./, '').toLowerCase())) };
  }
  activeConversation = cid;
  if (body.method === 'open') {
    if (!headed || !context) { await stop(); headed = true; await launch(); }
    if (body.url) await raw('browser_navigate', { url: body.url });
    const page = context.pages().at(-1);
    if (page) await page.bringToFront();
    return { running: true, headed: true };
  }
  if (body.method === 'confirm' || body.method === 'reject') {
    const review = ledger.reviews.find(r => r.id === body.action_id && r.conversation_id === cid);
    if (!review) throw fail('NOT_FOUND', '待确认操作不存在');
    const publicAction = () => ({ ...review, call: undefined, fingerprint: undefined, private_state: undefined });
    if (review.status !== 'pending') return { action: publicAction() };
    if (body.method === 'reject') { review.status = 'rejected'; await persist(); return { action: publicAction() }; }
    const current = review.preview.kind === 'explicit' ? await explicitReview({ ...review.preview, target: review.call.review_target || review.call.arguments.target }) :
      await formReview(review.call.review_target || review.call.arguments.target);
    const { private_state, ...publicPreview } = current || {};
    if (hash({ call: review.call, preview: await redact(current ? publicPreview : null), private_state }) !== review.fingerprint) {
      review.status = 'stale'; await persist(); throw fail('BROWSER_ACTION_CHANGED', '提交内容或目标已改变，请重新生成预览后确认');
    }
    review.status = 'executing'; await persist();
    try {
      review.result = await redact(await raw(review.call.name, review.call.arguments));
      review.status = review.result.isError ? 'uncertain' : 'executed';
    } catch { review.status = 'uncertain'; }
    await persist();
    return { action: publicAction(),
      message: review.status === 'executed' ? '已执行提交操作，请核对页面回执。' : '无法确定提交结果，请先核对页面，不自动重复提交。' };
  }
  if (body.method !== 'call') throw fail('INVALID_INPUT', '不支持的浏览器操作');
  const { name, arguments: args = {} } = body;
  assertModelBrowserCall(name, args);
  // Inspect before any model-driven fill, even when no submit was requested.
  // Password/OTP values belong in the human browser, never in tool arguments.
  const fillTargets = name === 'browser_type' ? [args.target] :
    name === 'browser_fill_form' ? (args.fields || []).map(field => field.target) : [];
  for (const target of fillTargets) {
    if ((await interactionTarget(target))?.sensitive) throw fail('BROWSER_AUTH_REQUIRED',
      '密码和一次性验证码请由用户在浏览器窗口中手动填写，不要复制到对话或工具参数。');
  }
  if (name === 'browser_close') { await stop(); return text({ running: false, message: '浏览器已关闭，登录配置保留。' }); }
  const began = generation;
  let result, executeName = name, executeArgs = args;
  if (name === 'browser_prepare_action') {
    const call = { name: 'browser_click', arguments: { target: args.target } };
    return pending(cid, call, await explicitReview(args));
  }
  if (name === 'browser_click' || (name === 'browser_type' && args.submit) || (name === 'browser_press_key' && ['Enter', 'Space', ' '].includes(args.key))) {
    if (name === 'browser_type') await raw(name, { ...args, submit: false });
    const preview = await formReview(args.target);
    if (preview && !preview.read_only_search && (preview.is_submit || (name !== 'browser_click' && !['Space', ' '].includes(args.key)))) {
      const call = name === 'browser_type' ? { name: 'browser_press_key', arguments: { key: 'Enter' } } : { name, arguments: args };
      // For Enter, retain the input target for rechecking the same form.
      if (name === 'browser_type') call.review_target = args.target;
      return pending(cid, call, await redact(preview));
    }
    if (name === 'browser_type') {
      executeName = 'browser_press_key'; executeArgs = { key: 'Enter' };
    }
    if (!preview?.read_only_search && !isReadOnlyInteraction(await interactionTarget(args.target), executeArgs.key)) {
      const call = { name: executeName, arguments: executeArgs };
      if (name === 'browser_type') call.review_target = args.target;
      return pending(cid, call, await explicitReview({ target: args.target,
        description: '检查并执行当前页面操作', recipient: '当前页面显示的目标',
        content: '按预览中的实际字段和目标执行；网站成功仍需核对回执。' }));
    }
  }
  try { result = await raw(executeName, executeArgs); }
  catch (error) { if (generation !== began) throw fail('BROWSER_STOPPED', '浏览器操作已停止'); throw error; }
  await Promise.all([...downloads]);
  await persist();
  if (generation !== began) throw fail('BROWSER_STOPPED', '浏览器操作已停止');
  return redact(result);
}

function authorized(value) {
  const expected = Buffer.from('Bearer ' + token), actual = Buffer.from(value || '');
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}
const server = createServer(async (req, res) => {
  res.setHeader('Content-Type', 'application/json');
  if (req.method !== 'POST' || req.url !== '/rpc' || req.headers.origin || !authorized(req.headers.authorization)) {
    res.statusCode = 403; return res.end('{"ok":false,"code":"BROWSER_DENIED"}');
  }
  try {
    const chunks = []; let size = 0;
    for await (const chunk of req) { size += chunk.length; if (size > 2_000_000) throw fail('INVALID_INPUT', '浏览器请求过大'); chunks.push(chunk); }
    const body = JSON.parse(Buffer.concat(chunks).toString());
    const task = body.method === 'stop' ? (async () => {
      const reset_required = body.turn_id ? unsafeTurns.has(body.turn_id) : unsafeTurns.size > 0;
      if (body.turn_id) {
        stoppedTurns.add(body.turn_id);
        if (activeOperation?.turn_id !== body.turn_id && !reset_required) return { running: !!context, interrupted: false };
        if (activeOperation?.turn_id && activeOperation.turn_id !== body.turn_id)
          throw fail('BROWSER_OTHER_TURN_ACTIVE', '浏览器正在处理另一个轮次，停止收尾仍在等待');
      }
      await stop(); return { running: false, interrupted: true, reset_required };
    })() : chain.then(async () => {
      activeOperation = { turn_id: body.turn_id, conversation_id: body.conversation_id };
      activityFrame({ ...activeOperation, name: body.name || body.method });
      try { return await dispatch(body); } finally {
        activeOperation = undefined;
        activityFrame(null);
      }
    });
    if (body.method !== 'stop') chain = task.catch(() => {});
    const result = await task;
    res.end(JSON.stringify({ ok: true, result }));
  } catch (error) {
    res.statusCode = 409;
    res.end(JSON.stringify({ ok: false, code: typeof error.code === 'string' && /^(BROWSER_|INVALID_INPUT|NOT_FOUND)/.test(error.code) ? error.code : 'BROWSER_OPERATION_FAILED',
      message: ['BROWSER_ACTION_CHANGED', 'BROWSER_TARGET_CHANGED', 'BROWSER_APPROVAL_REQUIRED', 'BROWSER_AUTH_REQUIRED'].includes(error.code) ? error.message : '浏览器操作未完成，请检查页面、浏览器安装或网络后重试。' }));
  }
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
process.stdout.write(JSON.stringify({ ready: true, url: `http://127.0.0.1:${server.address().port}` }) + '\n');
let closing = false;
async function shutdown() {
  if (closing) return;
  closing = true;
  await stop();
  await Promise.allSettled([...downloads]);
  await chain.catch(() => {});
  await persist();
  server.closeAllConnections();
  await new Promise(done => server.close(done));
}
process.stdin.resume();
process.stdin.on('end', () => { void shutdown(); });
process.on('SIGTERM', () => { void shutdown(); });
