/** Preserve web failures as failures, with a legal next step for model recovery. */
export function installWebRecovery(ctx) {
  // WEB_BLOCKED_URL is a DNS/address-policy denial, not a transient HTTP error.
  // Remember only exact hostnames while this Agent turn is open so model retries
  // cannot repeatedly trigger the same rejected provider request. Clear the set
  // at the turn boundary; other hosts and later user turns remain eligible.
  const blockedHostsByAgent = new WeakMap();
  ctx.on('agent/turn-stopping', ({ agent }) => {
    if (agent && (typeof agent === 'object' || typeof agent === 'function')) blockedHostsByAgent.delete(agent);
  });
  return ctx.on('tools/execute', async (exec, next) => {
    if (exec.name === 'web_fetch') {
      let host;
      try {
        const url = new URL(exec.arguments?.url);
        if (url.protocol === 'http:' || url.protocol === 'https:') host = url.hostname.toLowerCase();
      } catch { /* The web tool owns malformed-URL validation. */ }
      const agent = exec.agent && (typeof exec.agent === 'object' || typeof exec.agent === 'function')
        ? exec.agent : undefined;
      const seen = agent ? blockedHostsByAgent.get(agent) : undefined;
      if (host && seen?.has(host)) {
        const message = 'This hostname was already blocked by the web address safety policy in this Agent turn; no network request was repeated. Use a different trusted source or report that this source could not be fetched.';
        return { isError: true, error: { message, info: { name: 'WebError', code: 'WEB_BLOCKED_URL' } },
          content: [{ type: 'text', text: `Error: ${message}` }, { type: 'text', text: recoveryHint() }] };
      }
    }
    const result = await next();
    if (exec.name !== 'web_fetch' || !result.isError) return result;
    const code = result.error?.info?.code;
    if (code !== 'WEB_BLOCKED_URL') return result;
    let host;
    try { host = new URL(exec.arguments?.url).hostname.toLowerCase(); } catch { /* Keep the original error. */ }
    const agent = exec.agent && (typeof exec.agent === 'object' || typeof exec.agent === 'function')
      ? exec.agent : undefined;
    if (host && agent) {
      let seen = blockedHostsByAgent.get(agent);
      if (!seen) { seen = new Set(); blockedHostsByAgent.set(agent, seen); }
      seen.add(host);
    }
    return { ...result, content: [...result.content, { type: 'text', text: recoveryHint() }] };
  });
}

function recoveryHint() {
  return '恢复提示（WEB_BLOCKED_URL）：这是匿名网页工具的地址安全策略限制，不证明整机断网、页面不存在或需要登录。'
    + '不要反复更换同类域名重试，也不要修改网络设置绕过防护。'
    + '校园通知可用campus.sync(capability=notices, query={})取得已登记官方摘要；摘要只能支持已返回的标题与日期。'
    + '必须核对公开正文时，可load_tools(group=browser)后在授权浏览器读取原官方网址；登录页不等于业务数据。'
    + '已有证据足够时及时回答并注明未核验部分；其他合法路径也失败则明确限制，不无限搜索。';
}
