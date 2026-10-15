import assert from 'node:assert/strict';
import test from 'node:test';
import { createMcpToolDefinition } from '@deepseek-ai/dsh-mcp-client';

const definition = call => createMcpToolDefinition({}, { name: 'mcp__fixture__multiply', rawName: 'multiply',
  description: 'Synthetic multiplication', inputSchema: { type: 'object', required: ['a', 'b'],
    properties: { a: { type: 'number' }, b: { type: 'number' } }, additionalProperties: false }, call });
const exec = () => ({ signal: new AbortController().signal });

test('upstream MCP bridge preserves content and structured output', async () => {
  const tool = definition(async () => ({ content: [{ type: 'text', text: '252' }], structuredContent: { value: 252 } }));
  const value = await tool.execute({ a: 36, b: 7 }, exec());
  assert.deepEqual(value.structuredContent, { value: 252 });
  assert.equal(tool.output.render({}, value)[0].text, '252');
});

test('upstream MCP isError cannot masquerade as a successful canonical value', async () => {
  const tool = definition(async () => ({ isError: true, content: [{ type: 'text', text: 'fixture tool refused' }] }));
  await assert.rejects(tool.execute({ a: 1, b: 2 }, exec()), /fixture tool refused/);
});

test('malformed MCP results are rejected instead of rendered as success', async () => {
  const tool = definition(async () => ({ content: 'invalid content type' }));
  await assert.rejects(tool.execute({}, exec()), /invalid MCP result/);
});

test('MCP call receives the exact cancellation execution signal', async () => {
  const execution = exec(); let observed;
  const tool = definition(async (_args, passed) => { observed = passed.signal; return { content: [] }; });
  await tool.execute({}, execution);
  assert.equal(observed, execution.signal);
});
