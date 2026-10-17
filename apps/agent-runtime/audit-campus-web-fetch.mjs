/**
 * Credential-free audit of the project-owned fetch transport. No model is called.
 *
 * Proves on the real network that:
 * - a configured university public host resolving to campus-private addresses is
 *   readable through `web_fetch` (previously WEB_BLOCKED_URL),
 * - an ordinary public host still works,
 * - plain private/loopback literals are still refused.
 */
import { writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Context } from '@deepseek-ai/cordis';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import { WebRuntime } from '@deepseek-ai/dsh-web';
import { applyWebFetchTool } from '@deepseek-ai/dsh-tool-web';
import { createCampusFetchProvider } from './campus-web-fetch.mjs';

const runtime = fileURLToPath(new URL('.', import.meta.url));
const ctx = new Context();
new SystemPrompt(ctx, { includeHarnessIdentity: false });
new ToolRuntime(ctx, { mode: 'native' });
new WebRuntime(ctx);
ctx.web.registerFetchProvider(createCampusFetchProvider());
applyWebFetchTool(ctx, 15000, 24000);

const cases = [
  { name: 'trusted-university-host', url: 'https://www.zju.edu.cn/' },
  { name: 'trusted-university-events', url: 'http://rd.zju.edu.cn/cs/list.htm' },
  { name: 'ordinary-public-host', url: 'https://example.org/' },
  { name: 'private-ip-literal', url: 'http://10.203.4.70/' },
  { name: 'loopback-ip-literal', url: 'http://127.0.0.1:8021/api/v1/health' },
];
const items = [];
try {
  for (const [index, item] of cases.entries()) {
    const started = performance.now();
    let result;
    try {
      result = await ctx.tools.execute({ name: 'web_fetch', arguments: { url: item.url },
        callId: 'campus-web-fetch-' + index, agent: { session: { id: 'credential-free-audit' } },
        signal: AbortSignal.timeout(20000) });
    } catch (error) {
      items.push({ ...item, ok: false, thrown: String(error?.code ?? error?.message ?? error),
        elapsed_ms: Math.round(performance.now() - started) });
      continue;
    }
    const text = result.content?.filter(x => x.type === 'text').map(x => x.text).join('\n') ?? '';
    items.push({ ...item, ok: !result.isError, code: result.error?.info?.code ?? null,
      status: result.value?.statusCode ?? null,
      rendered_chars: text.length,
      elapsed_ms: Math.round(performance.now() - started) });
  }
  const report = { observed_at: new Date().toISOString(), kind: 'credential-free-fetch-transport-audit',
    model_called: false, credentials: false, items,
    checks: {
      university_host_readable: items[0].ok === true,
      university_events_readable: items[1].ok === true,
      ordinary_public_host_readable: items[2].ok === true,
      private_literal_blocked: items[3].ok === false,
      loopback_literal_blocked: items[4].ok === false,
    } };
  const target = resolve(runtime, '../../docs/audit/campus-web-fetch.json');
  await writeFile(target, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  if (!Object.values(report.checks).every(Boolean)) process.exitCode = 1;
} finally {
  await ctx.fiber.dispose();
}
