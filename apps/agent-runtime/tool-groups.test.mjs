import assert from 'node:assert/strict';
import test from 'node:test';
import { Context } from '@deepseek-ai/cordis';
import { createScope } from '@deepseek-ai/dsh-scope';
import { ToolRuntime } from '@deepseek-ai/dsh-tools';
import { groupOf, installToolMask, registerToolLoader } from './campus-tool-groups.mjs';

const definition = name => ({ name, description: name, parameters: { type: 'object', properties: {} },
  output: { schema: { type: 'string' }, render: (_, value) => [{ type: 'text', text: value }] }, execute: () => name });

for (const name of ['spawn_teammate', 'wait_agent', 'list_subagent_models']) test(`${name} belongs to the team catalog`, () => {
  assert.equal(groupOf(name)?.id, 'team');
});

test('scope-owned definitions never cause invalid inherited restrictions', async () => {
  const ctx = new Context(); ctx.provide('systemPrompt', { tools() {} }); new ToolRuntime(ctx);
  ctx.tools.register(definition('read')); registerToolLoader(ctx);
  const agent = {}; const scope = createScope(ctx, agent); agent.ctx = scope.ctx;
  agent.ctx.tools.register(definition('subagent'));
  try { assert.doesNotThrow(() => installToolMask(agent)); }
  finally { await scope.dispose(); }
});

test('loader schema requires a nonempty group or tool selection', () => {
  const ctx = new Context(); ctx.provide('systemPrompt', { tools() {} }); new ToolRuntime(ctx);
  registerToolLoader(ctx);
  const schema = ctx.tools.get('load_tools').parameters;
  assert.deepEqual(schema.anyOf, [{ required: ['group'] }, { required: ['tools'] }]);
  assert.equal(schema.properties.tools.minItems, 1);
});

test('exact-name requests explain whole-group loading and unknown names', async () => {
  const ctx = new Context(); ctx.provide('systemPrompt', { tools() {} }); new ToolRuntime(ctx);
  for (const name of ['read', 'job_list', 'job_output']) ctx.tools.register(definition(name));
  registerToolLoader(ctx);
  const agent = {}; const scope = createScope(ctx, agent); agent.ctx = scope.ctx; installToolMask(agent);
  try {
    const result = await ctx.tools.get('load_tools', agent).execute({ tools: ['job_list', 'not_registered'] }, { agent });
    assert(result.available.includes('job_output'));
    assert.match(result.note, /not_registered/);
    assert.match(ctx.tools.get('load_tools', agent).description, /整组/);
  } finally { await scope.dispose(); }
});

test('loading in a child releases ancestor masks but never unrelated-scope policies', async () => {
  const ctx = new Context(); ctx.provide('systemPrompt', { tools() {} }); new ToolRuntime(ctx);
  ctx.tools.register(definition('job_list')); registerToolLoader(ctx);
  const parent = {}, child = {}, sameParentChild = {}, unrelated = {};
  const scopes = [createScope(ctx, parent), createScope(ctx, child, { parent }),
    createScope(ctx, sameParentChild, { parent }), createScope(ctx, unrelated)];
  [parent, child, sameParentChild, unrelated].forEach((agent, i) => { agent.ctx = scopes[i].ctx; installToolMask(agent); });
  try {
    const policy = child.ctx.tools.restrict({ deny: ['job_list'] });
    const result = await ctx.tools.get('load_tools', child).execute({ group: 'jobs', tools: ['job_list'] }, { agent: child });
    assert(!result.available.includes('job_list'));
    assert.match(result.note, /job_list/);
    assert(ctx.tools.get('job_list', parent), 'Inherited masking must be released at the ancestor');
    assert(!ctx.tools.get('job_list', sameParentChild), 'Existing sibling retains its own mask');
    assert(!ctx.tools.get('job_list', unrelated));
    policy(); assert(ctx.tools.get('job_list', child));
  } finally { for (const scope of scopes.reverse()) await scope.dispose(); }
});

test('scope-owned tools remain visible before and after late registration', async () => {
  const ctx = new Context(); ctx.provide('systemPrompt', { tools() {} }); new ToolRuntime(ctx);
  ctx.tools.register(definition('read')); registerToolLoader(ctx);
  const agent = {}; const scope = createScope(ctx, agent); agent.ctx = scope.ctx;
  installToolMask(agent);
  try {
    assert.doesNotThrow(() => agent.ctx.tools.register(definition('subagent_fork')));
    assert(ctx.tools.get('subagent_fork', agent));
  } finally { await scope.dispose(); }
});

test('loader rejects empty, unknown-group and duplicate tool arguments', async () => {
  const ctx = new Context(); ctx.provide('systemPrompt', { tools() {} }); new ToolRuntime(ctx); registerToolLoader(ctx);
  const loader = ctx.tools.get('load_tools');
  for (const args of [{}, { group: 'not-a-group' }, { tools: [] }, { tools: ['job_list', 'job_list'] }, { group: 'jobs', owner: 'model' }]) {
    await assert.rejects(loader.execute(args, {}), /invalid tool args/);
  }
});
