/** Preserve web failures as failures, with a legal next step for model recovery. */
export function installWebRecovery(ctx) {
  return ctx.on('tools/execute', async (exec, next) => {
    const result = await next();
    if (exec.name !== 'web_fetch' || !result.isError) return result;
    const code = result.error?.info?.code;
    if (code !== 'WEB_BLOCKED_URL') return result;
    return { ...result, content: [...result.content, { type: 'text', text:
      '恢复提示（WEB_BLOCKED_URL）：这是匿名网页工具的地址安全策略限制，不证明整机断网、页面不存在或需要登录。'
      + '不要反复更换同类域名重试，也不要修改网络设置绕过防护。'
      + '校园通知可用campus.sync(capability=notices, query={})取得已登记官方摘要；摘要只能支持已返回的标题与日期。'
      + '必须核对公开正文时，可load_tools(group=browser)后在授权浏览器读取原官方网址；登录页不等于业务数据。'
      + '已有证据足够时及时回答并注明未核验部分；其他合法路径也失败则明确限制，不无限搜索。',
    }] };
  });
}
