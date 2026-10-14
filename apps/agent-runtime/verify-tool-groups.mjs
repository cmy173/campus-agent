/** Public upstream registry/scopes; no model and no private runtime patches. */
import assert from 'node:assert/strict';
import { Context } from '@deepseek-ai/cordis';
import { createScope } from '@deepseek-ai/dsh-scope';
import { ToolRuntime } from '@deepseek-ai/dsh-tools';
import { installToolMask, registerToolLoader } from './campus-tool-groups.mjs';

const ctx = new Context();
ctx.provide('systemPrompt', { tools() {} });
new ToolRuntime(ctx);
const definition = name => ({ name, description: name, parameters: { type: 'object', properties: {} },
  output: { schema: { type: 'string' }, render: (_, value) => [{ type: 'text', text: value }] }, execute: () => name });
for (const name of ['read', 'campus_query', 'future_tool', 'job_list', 'subagent', 'mcp__campus_browser__browser_navigate']) {
  ctx.tools.register(definition(name));
}
registerToolLoader(ctx);
const parent = {}, sibling = {}, child = {};
const scopes = [createScope(ctx, parent), createScope(ctx, sibling), createScope(ctx, child, { parent })];
[parent, sibling, child].forEach((agent, i) => { agent.ctx = scopes[i].ctx; installToolMask(agent); });
const names = agent => agent.ctx.tools.schemas(agent).map(s => s.name);
const load = (agent, args) => ctx.tools.get('load_tools', agent).execute(args, { agent });
try {
  assert.deepEqual(names(parent), ['read', 'campus_query', 'future_tool', 'load_tools']);
  await load(child, { group: 'browser' });
  assert(names(child).includes('mcp__campus_browser__browser_navigate'));
  assert(!names(sibling).includes('mcp__campus_browser__browser_navigate'));
  assert(!names(child).includes('job_list'));
  const drop = ctx.tools.register(definition('mcp__calculator__multiply'));
  assert(!names(parent).includes('mcp__calculator__multiply'));
  await load(child, { tools: ['mcp__calculator__multiply'] });
  assert(names(child).includes('mcp__calculator__multiply'));
  assert(!names(sibling).includes('mcp__calculator__multiply'));
  drop();
  ctx.tools.register(definition('mcp__calculator__multiply'));
  assert(names(child).includes('mcp__calculator__multiply'));
  // Loading our group must never remove a separate upstream capability policy.
  const policy = child.ctx.tools.restrict({ deny: ['job_list'] });
  await load(child, { group: 'jobs' });
  assert(!names(child).includes('job_list'));
  policy();
  assert(names(child).includes('job_list'));
  await load(child, { group: 'browser' });
  assert(!names(child).includes('subagent'));
  console.log('PASS default, child loading, sibling isolation, late registration, reconnect, independent policy, repeated load');
} finally {
  for (const scope of scopes.reverse()) await scope.dispose();
}
