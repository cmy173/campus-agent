/** Credential-free LIVE audit. Not a model/SSO success benchmark. Fixed URLs only. */
import { lookup } from 'node:dns/promises';
import { writeFile } from 'node:fs/promises';
import { Context } from '@deepseek-ai/cordis';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import { WebRuntime } from '@deepseek-ai/dsh-web';
import { HttpFetchProvider } from '@deepseek-ai/dsh-web-fetch-http';
import { applyWebFetchTool } from '@deepseek-ai/dsh-tool-web';
import { installProxyFromEnvironment, proxyRouteFor } from '@deepseek-ai/dsh-http-proxy';
import { createLaunchEnvironmentSnapshot } from '@deepseek-ai/dsh-launch-environment';

const closeProxy = await installProxyFromEnvironment(createLaunchEnvironmentSnapshot([
  { source: 'process', values: process.env },
]), () => {});
const ctx = new Context();
new SystemPrompt(ctx, { includeHarnessIdentity: false });
new ToolRuntime(ctx, { mode: 'native' }); new WebRuntime(ctx);
ctx.web.registerFetchProvider(new HttpFetchProvider({ maxResponseBytes: 500000, maxBodyChars: 24000,
  timeoutMs: 10000, maxRedirects: 5, userAgent: 'CampusAgent-ReadonlyAudit/1.0' }));
// No system-prompt service needed for the standalone fetch registration.
applyWebFetchTool(ctx, 12000, 24000);
const urls = ['https://example.org/', 'https://www.zju.edu.cn/',
  'https://zdbk.zju.edu.cn/jwglxt/xtgl/login_slogin.html', 'https://webvpn.zju.edu.cn/'];
const items = [];
try {
  for (const url of urls) {
    const start = performance.now();
    let dns;
    try { dns = { ok: true, answers: (await Promise.race([lookup(new URL(url).hostname, { all: true }),
      new Promise((_, reject) => { const t = setTimeout(() => reject(new Error('DNS_TIMEOUT')), 5000); t.unref(); })])).map(x => ({ family: x.family,
        private_v4: /^(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)/.test(x.address) })) }; }
    catch (error) { dns = { ok: false, code: error.code ?? 'DNS_TIMEOUT' }; }
    const result = await ctx.tools.execute({ name: 'web_fetch', arguments: { url }, callId: `audit-${items.length}`,
      agent: { session: { id: 'credential-free-audit' } }, signal: AbortSignal.timeout(12000) });
    const text = result.content?.filter(x => x.type === 'text').map(x => x.text).join('\n') ?? '';
    items.push({ url, dns, proxy: proxyRouteFor(new URL(url)).proxied, ok: !result.isError,
      code: result.error?.info?.code ?? null, status: result.value?.statusCode ?? null,
      final_url: result.value?.url ?? null, rendered_chars: text.length,
      model_content_present: !result.isError && text.includes('External web content'),
      body_kind: result.value?.body?.kind ?? null,
      login_page_detected: /统一身份|用户登录|Login|登录/.test(result.value?.body?.content ?? ''),
      elapsed_ms: Math.round(performance.now() - start) });
  }
  const report = { observed_at: new Date().toISOString(), mode: 'live-anonymous-tool-dispatch',
    credentials: false, llm_called: false, tool_visible: ctx.tools.schemas().some(x => x.name === 'web_fetch'), items };
  await writeFile(process.argv[2] ?? '../../docs/audit/transition-network-node.json', JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally { await ctx.fiber.dispose(); await closeProxy(); }
