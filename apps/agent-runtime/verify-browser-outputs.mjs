/**
 * Focused real-Edge verification of model-visible browser output paths.
 *
 * The product requires: screenshots/PDFs a model asks for must land inside the
 * conversation workspace (the capability scope it may read), never in the source tree
 * or the private browser home. This script drives the owned browser host directly
 * (no model, no paid service) with a local fixture page.
 *
 *   node verify-browser-outputs.mjs
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomBytes } from 'node:crypto';

const runtime = fileURLToPath(new URL('.', import.meta.url));
const root = fileURLToPath(new URL('../../', import.meta.url));
const directory = await mkdtemp(resolve(root, 'var/v2-browser-outputs-'));
const workspace = resolve(directory, 'workspace');
await mkdir(workspace, { recursive: true });

const fixture = createServer((req, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.end(`<!doctype html><html><body><article><h1>公开活动预告</h1>
    <p>2026-10-18 学术报告：可见的公开内容 472</p>
    <img alt="poster" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='80'%3E%3Crect width='120' height='80' fill='%23336699'/%3E%3C/svg%3E"/>
  </article></body></html>`);
});
await new Promise(done => fixture.listen(0, '127.0.0.1', done));
const url = `http://127.0.0.1:${fixture.address().port}/`;

let host;
async function start() {
  const token = randomBytes(32).toString('hex');
  const proc = spawn(process.execPath, ['browser-host.mjs'], {
    cwd: runtime, windowsHide: true,
    env: { ...process.env, CAMPUS_BROWSER_HOME: resolve(directory, 'browser'),
      CAMPUS_BROWSER_TOKEN: token, CAMPUS_BROWSER_WORKSPACE: workspace },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  let diagnostic = '';
  proc.stderr.on('data', chunk => { diagnostic += chunk; });
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
    body: JSON.stringify({ conversation_id: 'browser_outputs_fixture', method, ...fields }),
    signal: AbortSignal.timeout(150000),
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
  const ended = new Promise((done, reject) => {
    const timer = setTimeout(() => { owned.proc.kill(); reject(new Error('Owned host did not close')); }, 15000);
    owned.proc.once('exit', code => { clearTimeout(timer); assert.equal(code, 0); done(); });
  });
  owned.proc.stdin.end();
  await ended;
}

const evidence = { kind: 'actual_owned_edge_browser_output_scope', checks: [], model_called: false };
try {
  await start();
  const navigated = await call('browser_navigate', { url });
  assert.match(plain(navigated), /公开活动预告/);
  evidence.checks.push('fixture_page_loaded_with_inline_content');

  // A model-chosen relative name must land in <workspace>/browser-outputs.
  await call('browser_take_screenshot', { filename: 'proof.png', target: 'article' });
  const screenshot = resolve(workspace, 'browser-outputs/proof.png');
  assert.equal(existsSync(screenshot), true, 'screenshot missing from workspace');
  const bytes = await readFile(screenshot);
  assert.equal(bytes.subarray(0, 8).toString('hex'), '89504e470d0a1a0a', 'not a PNG');
  evidence.checks.push('relative_screenshot_saved_inside_workspace');
  evidence.screenshot_path = screenshot;

  // An absolute destination is refused by the browser action policy before execution.
  const escaped = await rpc('call', { name: 'browser_take_screenshot',
    arguments: { filename: resolve(root, 'apps/agent-runtime/escape-proof.png') } }, false);
  assert.equal(escaped.code, 'BROWSER_APPROVAL_REQUIRED');
  assert.equal(existsSync(resolve(root, 'apps/agent-runtime/escape-proof.png')), false);
  evidence.checks.push('absolute_screenshot_path_refused_without_writing');

  // Snapshot/console side files also stay inside the workspace, not the source tree.
  const outputFiles = await readdir(resolve(workspace, 'browser-outputs'));
  assert(outputFiles.some(name => /^page-.*\.yml$/.test(name)));
  assert.equal(existsSync(resolve(runtime, 'browser-outputs')), false);
  assert.equal(existsSync(resolve(runtime, 'proof.png')), false);
  evidence.checks.push('snapshot_files_stay_inside_workspace');
  evidence.output_files = outputFiles;
  evidence.ok = true;
  await writeFile(resolve(directory, 'evidence.json'), JSON.stringify(evidence, null, 2));
  console.log(JSON.stringify({ ok: true, evidence: resolve(directory, 'evidence.json'), checks: evidence.checks }));
} finally {
  await close();
  await new Promise(done => fixture.close(done));
}
