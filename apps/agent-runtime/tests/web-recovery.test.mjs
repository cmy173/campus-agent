import assert from 'node:assert/strict';
import test from 'node:test';
import { Context } from '@deepseek-ai/cordis';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import { WebRuntime, WebError } from '@deepseek-ai/dsh-web';
import { applyWebFetchTool } from '@deepseek-ai/dsh-tool-web';
import { installWebRecovery } from '../web-recovery.mjs';

test('installed web tool preserves DNS policy denial and supplies alternate capability guidance', async () => {
  const ctx = new Context();
  try {
    new SystemPrompt(ctx, { includeHarnessIdentity: false }); new ToolRuntime(ctx, { mode: 'native' }); new WebRuntime(ctx);
    let requests = 0;
    ctx.web.registerFetchProvider({ id: 'fixture', available: () => true, fetch: async () => {
      requests++; throw new WebError('URL resolves to a non-public IP address', 'WEB_BLOCKED_URL');
    } });
    applyWebFetchTool(ctx, 1000, 1000); installWebRecovery(ctx);
    const result = await ctx.tools.execute({ name: 'web_fetch', arguments: { url: 'https://www.zju.edu.cn/' },
      agent: {}, callId: 'blocked', signal: new AbortController().signal });
    assert.equal(result.isError, true); assert.equal(result.error.info.code, 'WEB_BLOCKED_URL');
    const text = result.content.map(b => b.text).join('\n');
    assert.match(text, /campus.sync/); assert.match(text, /load_tools/);
    assert.match(text, /不证明整机断网/); assert.equal(requests, 1);
  } finally { await ctx.fiber.dispose(); }
});
