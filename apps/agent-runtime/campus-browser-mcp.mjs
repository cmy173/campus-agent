/** Per-turn MCP adapter; persistent browser belongs to the API, not this child. */
import { Server } from '@modelcontextprotocol/server';
import { StdioServerTransport } from '@modelcontextprotocol/server/stdio';

const base = process.env.CAMPUS_BRIDGE_URL;
const token = process.env.CAMPUS_BRIDGE_TOKEN;
const conversation_id = process.env.CAMPUS_CONVERSATION_ID;
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(base || '') || !token || !conversation_id) throw new Error('Browser bridge unavailable');
async function rpc(body) {
  const response = await fetch(base + '/api/v1/agent/internal/browser', {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
    body: JSON.stringify({ conversation_id, ...body }), signal: AbortSignal.timeout(150000),
  });
  let result;
  try { result = await response.json(); }
  catch { return { isError: true, content: [{ type: 'text', text: '浏览器网关未返回有效结果，请检查应用连接后重试。' }] }; }
  if (!response.ok) return { isError: true, content: [{ type: 'text', text: result.error?.message || '浏览器操作未完成' }] };
  return result;
}
const server = new Server({ name: 'campus-browser', version: '1.0.0' }, { capabilities: { tools: {} } });
server.setRequestHandler('tools/list', async () => {
  const result = await rpc({ method: 'tools' });
  if (!result.tools) throw new Error('Browser tool discovery failed');
  return result;
});
server.setRequestHandler('tools/call', async request => rpc({ method: 'call', name: request.params.name,
  arguments: request.params.arguments || {} }));
await server.connect(new StdioServerTransport());
