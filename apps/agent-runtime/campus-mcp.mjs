/** Thin credential boundary; discovery, execution and reconnect belong to upstream. */
import { apply as applyMcp, Config, inject } from '@deepseek-ai/dsh-mcp-client';
export { inject };
export const name = 'campus-mcp';

export async function apply(ctx, config) {
  const all = JSON.parse(process.env.CAMPUS_MCP_CREDENTIALS || '{}');
  const credentials = all[config.serverName] ?? {};
  return applyMcp(ctx, Config({ ...config,
    ...(config.transport === 'stdio' ? { env: credentials.env ?? {} } : { headers: credentials.headers ?? {} }),
  }));
}
