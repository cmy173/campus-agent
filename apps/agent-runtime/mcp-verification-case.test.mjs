import assert from 'node:assert/strict';
import test from 'node:test';
import { calculatorReply } from './mcp-verification-case.mjs';
const target = 'mcp__calculator__multiply';
const user = { role: 'user', content: [{ type: 'text', text: 'MCP_CASE_OK' }] };
const result = value => ({ role: 'tool', content: [{ type: 'text', text: JSON.stringify(value) }] });
const loader = { name: 'load_tools', parameters: { properties: { group: { enum: ['mcp'] } } } };
const tool = { name: target, parameters: { type: 'object', properties: { a: { type: 'number' }, b: { type: 'number' } } } };

test('controlled verification loads MCP then sees schema then calls then summarizes final result', () => {
  const first = calculatorReply({ messages: [user], tools: [loader] }, 1);
  assert.equal(first.tool_calls[0].name, 'load_tools');
  const messages = [user, result({ available: [target] })];
  const second = calculatorReply({ messages, tools: [loader, tool] }, 2);
  assert.equal(second.tool_calls[0].name, target);
  assert.deepEqual(second.tool_calls[0].arguments, { a: 36, b: 7 });
  const last = { role: 'tool', content: [{ type: 'text', text: 'MCP computed: 252' }] };
  assert.equal(calculatorReply({ messages: [...messages, last], tools: [loader, tool] }, 3).content, 'MCP computed: 252');
});

test('controlled verification refuses the old bypass-discovery protocol', () => {
  assert.throws(() => calculatorReply({ messages: [user], tools: [loader, tool] }, 1), /deferred/);
  assert.throws(() => calculatorReply({ messages: [user, result({ available: [target] })], tools: [loader] }, 2), /visible/);
});
