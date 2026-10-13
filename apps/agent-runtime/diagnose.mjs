/** Settings diagnostics without running a conversation or exposing credential material. */
import { Client, StreamableHTTPClientTransport } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';
import { scrubbedParentEnv } from '@deepseek-ai/dsh-subprocess';
import { DeepSeekSearchProvider, DEEPSEEK_DEFAULT_BASE_URL, DEEPSEEK_DEFAULT_MODEL,
  DEEPSEEK_DEFAULT_API_VERSION } from '@deepseek-ai/dsh-web-search-deepseek';

const chunks = []; for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks).toString());
const started = performance.now();
let client;
try {
  let result;
  if (input.kind === 'mcp') {
    const server = input.server;
    client = new Client({ name: 'campus-settings-check', version: '1.0.0' }, {
      capabilities: {}, versionNegotiation: { mode: 'auto' },
    });
    const transport = server.transport === 'stdio' ? new StdioClientTransport({
      command: server.command, args: server.args, cwd: server.cwd || undefined,
      env: { ...scrubbedParentEnv(), ...server.env },
    }) : new StreamableHTTPClientTransport(new URL(server.url), { requestInit: { headers: server.headers } });
    await client.connect(transport);
    const tools = await client.listTools();
    result = { tool_names: tools.tools.map(t => t.name), tool_count: tools.tools.length };
  } else {
    if (!input.key) throw Object.assign(new Error('missing credential'), { code: 'SEARCH_KEY_MISSING' });
    const provider = new DeepSeekSearchProvider(() => ({ apiKey: input.key,
      apiKeyEnv: 'DEEPSEEK_SEARCH_API_KEY', baseURL: input.base_url || DEEPSEEK_DEFAULT_BASE_URL,
      model: input.model || DEEPSEEK_DEFAULT_MODEL, apiVersion: DEEPSEEK_DEFAULT_API_VERSION,
      maxTokens: 1024, maxUses: 1 }));
    const found = await provider.search({ query: 'DeepSeek official API documentation', maxResults: 3 }, AbortSignal.timeout(20000));
    result = { source_count: found.sources.length, sources: found.sources.slice(0, 3) };
  }
  console.log(JSON.stringify({ ok: true, latency_ms: Math.round(performance.now() - started), ...result }));
} catch (error) {
  const known = error.code === 'ENOENT' ? 'MCP_EXECUTABLE_MISSING' :
    error.code === 'SEARCH_KEY_MISSING' ? 'SEARCH_KEY_MISSING' : input.kind === 'mcp' ? 'MCP_CONNECTION_FAILED' : 'SEARCH_CONNECTION_FAILED';
  const message = known === 'MCP_EXECUTABLE_MISSING' ? '找不到 MCP 启动程序，请检查命令或绝对路径。' :
    known === 'SEARCH_KEY_MISSING' ? '搜索密钥未配置，请选择共享模型密钥或保存独立搜索密钥。' :
    input.kind === 'mcp' ? 'MCP 连接未通过，请核对启动命令、服务地址、凭据和服务状态。' :
      '搜索未通过，请核对独立搜索地址、模型、密钥和服务搜索权限；这不代表没有搜索结果。';
  console.log(JSON.stringify({ ok: false, latency_ms: Math.round(performance.now() - started), error: { code: known, message, retryable: true } }));
} finally {
  if (client) await client.close();
}
