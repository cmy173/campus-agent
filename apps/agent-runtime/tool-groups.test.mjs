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
  assert(!schema.properties.group.enum.includes('campus_reviews'), 'control schema keeps the existing group catalog');
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

test('review tools are deferred only when the experiment is enabled and can be loaded as a whole group', async () => {
  const setup = deferGroups => {
    const ctx = new Context(); ctx.provide('systemPrompt', { tools() {} }); new ToolRuntime(ctx);
    for (const name of ['campus_reviews_preflight', 'campus_reviews_generate', 'campus_reviews_read', 'campus_reviews_export', 'campus_assignments_list']) {
      ctx.tools.register(definition(name));
    }
    registerToolLoader(ctx, { deferGroups });
    const agent = {}; const scope = createScope(ctx, agent); agent.ctx = scope.ctx;
    installToolMask(agent, { deferGroups });
    return { ctx, agent, scope };
  };
  const control = setup([]);
  const treatment = setup(['campus_reviews']);
  const names = agent => agent.ctx.tools.schemas(agent).map(schema => schema.name);
  try {
    assert(names(control.agent).includes('campus_reviews_generate'), 'default behavior keeps every domain tool visible');
    assert(!control.ctx.tools.get('load_tools').parameters.properties.group.enum.includes('campus_reviews'));
    assert(!names(treatment.agent).some(name => name.startsWith('campus_reviews_')));
    assert(names(treatment.agent).includes('campus_assignments_list'), 'experiment is limited to the review family');
    assert(treatment.ctx.tools.get('load_tools').parameters.properties.group.enum.includes('campus_reviews'));
    const result = await treatment.ctx.tools.get('load_tools', treatment.agent).execute({ group: 'campus_reviews' }, { agent: treatment.agent });
    assert.equal(result.loaded_groups.includes('campus_reviews'), true);
    assert(names(treatment.agent).filter(name => name.startsWith('campus_reviews_')).length === 4);
    assert(names(control.agent).includes('campus_reviews_generate'), 'treatment cannot alter the independent control runtime');
  } finally {
    await treatment.scope.dispose(); await control.scope.dispose();
  }
});
