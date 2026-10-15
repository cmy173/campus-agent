/** Real subprocess + local HTTP CONNECT proxy; no internet or paid model. */
import assert from 'node:assert/strict';
import test from 'node:test';
import http from 'node:http';
import net from 'node:net';
import { spawn } from 'node:child_process';
import { once } from 'node:events';

test('standalone search diagnostic uses the Harness proxy policy and preserves source results', async () => {
  let requests = 0, tunnels = 0, forwarded = 0;
  const sockets = new Set();
  const origin = http.createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks));
    requests++;
    assert.equal(req.url, '/v1/messages');
    assert.equal(req.headers.authorization, 'Bearer synthetic-search-key');
    assert.equal(body.tools[0].type, 'web_search_20250305');
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Connection', 'close');
    res.end(JSON.stringify({ content: [{ type: 'web_search_tool_result', content: [
      { type: 'web_search_result', url: 'https://example.org/notice', title: 'Synthetic notice' },
    ] }] }));
  });
  const proxy = http.createServer((req, res) => {
    forwarded++;
    assert.equal(new URL(req.url).hostname, 'search.invalid');
    const upstream = http.request({ host: '127.0.0.1', port: origin.address().port,
      path: new URL(req.url).pathname, method: req.method, headers: req.headers }, response => {
      res.writeHead(response.statusCode, response.headers); response.pipe(res);
    });
    upstream.on('error', () => { res.writeHead(502); res.end(); });
    req.pipe(upstream);
  });
  proxy.on('connect', (req, downstream, head) => {
    tunnels++;
    assert.equal(req.url, 'search.invalid:80');
    sockets.add(downstream); downstream.on('close', () => sockets.delete(downstream));
    const upstream = net.connect(origin.address().port, '127.0.0.1', () => {
      downstream.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      if (head.length) upstream.write(head);
      downstream.pipe(upstream); upstream.pipe(downstream);
    });
    sockets.add(upstream); upstream.on('close', () => sockets.delete(upstream));
    downstream.on('error', () => upstream.destroy());
    upstream.on('error', () => downstream.destroy());
  });
  origin.listen(0, '127.0.0.1'); await once(origin, 'listening');
  proxy.listen(0, '127.0.0.1'); await once(proxy, 'listening');
  const proxyUrl = `http://127.0.0.1:${proxy.address().port}`;
  const child = spawn(process.execPath, [new URL('../diagnose.mjs', import.meta.url).pathname.replace(/^\/(?=[A-Za-z]:)/, '')], {
    env: { ...process.env, HTTP_PROXY: proxyUrl, http_proxy: proxyUrl, HTTPS_PROXY: proxyUrl,
      https_proxy: proxyUrl, ALL_PROXY: '', all_proxy: '', NO_PROXY: '127.0.0.1', no_proxy: '127.0.0.1' },
    stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
  });
  const output = [], errors = [];
  child.stdout.on('data', chunk => output.push(chunk));
  child.stderr.on('data', chunk => errors.push(chunk));
  child.stdin.end(JSON.stringify({ kind: 'search', key: 'synthetic-search-key',
                                 base_url: 'http://search.invalid/v1', model: 'fixture' }));
  const timer = setTimeout(() => child.kill(), 10000);
  try {
    const [code] = await once(child, 'exit');
    assert.equal(code, 0, JSON.stringify({ stderr: Buffer.concat(errors).toString(),
      stdout: Buffer.concat(output).toString(), requests, tunnels, forwarded }));
    const result = JSON.parse(Buffer.concat(output));
    assert.equal(result.ok, true, JSON.stringify(result));
    assert.equal(result.sources[0].url, 'https://example.org/notice');
    assert.equal(requests, 1); assert.equal(tunnels + forwarded, 1);
    assert(!Buffer.concat(output).toString().includes('synthetic-search-key'));
  } finally {
    clearTimeout(timer); if (child.exitCode === null) child.kill();
    for (const socket of sockets) socket.destroy();
    proxy.closeAllConnections(); origin.closeAllConnections();
    await Promise.all([new Promise(r => proxy.close(r)), new Promise(r => origin.close(r))]);
  }
});
