/** Deterministic local verification peer, not a language-model capability evaluation. */
import assert from 'node:assert/strict';
const target = 'mcp__calculator__multiply';
export const messageText = message => (message?.content ?? []).filter(b => b.type === 'text').map(b => b.text).join('\n');

export function calculatorReply(body, index) {
  const user = [...body.messages].reverse().find(m => m.role === 'user' && messageText(m).includes('MCP_CASE_'));
  const name = messageText(user);
  const responses = body.messages.slice(body.messages.lastIndexOf(user) + 1).filter(m => m.role === 'tool');
  const call = (name, args) => ({ content: '', tool_calls: [{ id: `mcp_${index}`, name, arguments: args }] });
  if (!responses.length) {
    assert(!body.tools.some(t => t.name === target), 'MCP schema must be deferred on the first request');
    const loader = body.tools.find(t => t.name === 'load_tools');
    assert(loader, 'Deferred MCP tools require a visible loader');
    assert(loader.parameters.properties.group.enum.includes('mcp'));
    return call('load_tools', { group: 'mcp' });
  }
  if (responses.length === 1) {
    const tool = body.tools.find(t => t.name === target);
    assert(tool, 'MCP schema must be visible on the request after loading');
    assert.equal(tool.parameters.type, 'object');
    assert(tool.parameters.properties.a && tool.parameters.properties.b, 'Server parameter schema must survive discovery');
    const loaded = JSON.parse(messageText(responses[0]));
    assert(loaded.available.includes(target), 'Loader must report actual MCP visibility');
    return call(target, { a: 36, b: 7, ...(name.includes('ERROR') ? { report_error: true } : {}),
      ...(name.includes('DROP') ? { disconnect_once: true } : {}) });
  }
  return { content: messageText(responses.at(-1)), tool_calls: [] };
}
