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
    const agent = {};
    const fetch = (url, callId, caller = agent) => ctx.tools.execute({ name: 'web_fetch', arguments: { url },
      agent: caller, callId, signal: new AbortController().signal });
    const result = await fetch('https://www.zju.edu.cn/', 'blocked');
    assert.equal(result.isError, true); assert.equal(result.error.info.code, 'WEB_BLOCKED_URL');
    const text = result.content.map(b => b.text).join('\n');
    assert.match(text, /campus.sync/); assert.match(text, /load_tools/);
    assert.match(text, /不证明整机断网/); assert.equal(requests, 1);
    const repeated = await fetch('https://www.zju.edu.cn/another-path?query=discarded', 'blocked-again');
    assert.equal(repeated.isError, true); assert.equal(repeated.error.info.code, 'WEB_BLOCKED_URL');
    assert.match(repeated.content.map(b => b.text).join('\n'), /no network request was repeated/);
    assert.equal(requests, 1, 'same host must not be fetched again in this Agent turn');
    await fetch('https://www.intl.zju.edu.cn/', 'other-host');
    assert.equal(requests, 2, 'another hostname remains eligible');
    await fetch('https://www.zju.edu.cn/', 'new-agent', {});
    assert.equal(requests, 3, 'a new Agent turn may retry the prior hostname');
    await ctx.serial(agent, 'agent/turn-stopping', {
      agent, turn: 1, signal: new AbortController().signal,
    });
    await fetch('https://www.zju.edu.cn/', 'next-turn');
    assert.equal(requests, 4, 'the same persistent Agent may retry the host in its next user turn');
  } finally { await ctx.fiber.dispose(); }
});
