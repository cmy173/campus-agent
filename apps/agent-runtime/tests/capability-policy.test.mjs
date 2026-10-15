/** Installed Cordis + ToolRuntime: execute middleware, not source-text assertions. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, mkdir, writeFile, readFile, rm, symlink } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { Context } from '@deepseek-ai/cordis';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import LocalFileSystem from '@deepseek-ai/dsh-fs-local';
import { apply as applyFs, Config as FsConfig } from '@deepseek-ai/dsh-tool-fs';
import { createScope } from '@deepseek-ai/dsh-scope';
import CampusFileSystem, { installCapabilityPolicy, registerMcpCapabilityPolicy, isPathWithin } from '../capability-policy.mjs';
import { transportConfig as McpConfig, credentialConfig, claimCredentialConfig } from '../campus-mcp.mjs';
import { scrubbedParentEnv } from '@deepseek-ai/dsh-subprocess';
import { prepareProfile, mcpConfig } from '../prepare-profile.mjs';
import { registerToolLoader, installToolMask } from '../campus-tool-groups.mjs';

const exec = (name, args = {}, agent) => ({ name, arguments: args, agent,
  callId: name, signal: new AbortController().signal });
function register(ctx, name, seen) {
  ctx.tools.register({ name, description: 'Offline policy fixture', parameters: { type: 'object' },
    output: { schema: { type: 'string' }, render: (_, value) => [{ type: 'text', text: value }] },
    async execute() { seen.push(name); return name; } });
}
async function fixture(run, options = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'campus-capability-'));
  const workspace = join(directory, 'workspace'), skills = join(directory, 'skills');
  await mkdir(workspace); await mkdir(skills);
  const ctx = new Context(), seen = [];
  try {
    new SystemPrompt(ctx, { includeHarnessIdentity: false });
    new ToolRuntime(ctx, { mode: 'native' });
    new LocalFileSystem(ctx, LocalFileSystem.Config({ cwd: workspace }));
    installCapabilityPolicy(ctx, { workspace, skillDirs: [skills], ...options });
    await run({ ctx, seen, directory, workspace, skills });
  } finally { await ctx.fiber.dispose(); await rm(directory, { force: true, recursive: true }); }
}
function denied(result) {
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /项目能力策略/);
  assert(!result.content[0].text.includes('fixture-private-value'));
}

test('shell/workflow/PTC denied before body and approval even after model tool loading', async () => {
  await fixture(async ({ ctx, seen, workspace }) => {
    for (const name of ['pwsh', 'bash', 'shell', 'workflow', 'ralph']) register(ctx, name, seen);
    registerToolLoader(ctx);
    const agent = { session: { header: { cwd: workspace } } };
    const scope = createScope(ctx, agent); agent.ctx = scope.ctx; installToolMask(agent);
    let reachedLaterPolicy = 0;
    ctx.on('tools/pre-execute', (_exec, next) => { reachedLaterPolicy++; return next(); });
    try {
      const load = await ctx.tools.execute(exec('load_tools', { group: 'jobs', tools: ['pwsh', 'workflow'] }, agent));
      assert.equal(load.isError, false);
      assert(load.value.available.includes('workflow'), 'visible is not authorized');
      reachedLaterPolicy = 0;
      for (const name of ['pwsh', 'bash', 'shell', 'workflow', 'ralph', 'run_code']) {
        denied(await ctx.tools.execute(exec(name, { command: '$env:DEEPSEEK_API_KEY', script: 'process.env' }, agent)));
      }
      assert.equal(reachedLaterPolicy, 0, 'pre-execute denies before approval middleware');
      assert.deepEqual(seen, []);
    } finally { await scope.dispose(); }
  });
});

test('unknown/disabled MCP tools and resource surfaces fail without asking; exact allow works', async () => {
  await fixture(async ({ ctx, seen }) => {
    registerMcpCapabilityPolicy(ctx, { serverName: 'fixture', allowedTools: ['read_course'] });
    for (const name of ['mcp__fixture__read_course', 'mcp__fixture__delete_course', 'mcp__fixture__read_course_extra',
      'mcp__unknown__read_course', 'mcp__fixtureX__read_course', 'list_mcp_resources', 'read_mcp_resource']) register(ctx, name, seen);
    registerToolLoader(ctx);
    const agent = {}; const scope = createScope(ctx, agent); agent.ctx = scope.ctx; installToolMask(agent);
    try {
      assert.equal((await ctx.tools.execute(exec('load_tools', { group: 'mcp' }, agent))).isError, false);
      for (const name of ['mcp__fixture__delete_course', 'mcp__fixture__read_course_extra', 'mcp__unknown__read_course',
        'mcp__fixtureX__read_course', 'mcp__fixture__missing', 'list_mcp_resources', 'read_mcp_resource']) {
        denied(await ctx.tools.execute(exec(name, {}, agent)));
      }
      assert.equal((await ctx.tools.execute(exec('mcp__fixture__read_course', {}, agent))).isError, false);
      assert.deepEqual(seen, ['mcp__fixture__read_course']);
    } finally { await scope.dispose(); }
  });
});

test('scope-local tools cannot bypass global policy and unrelated MCP scopes cannot grant authority', async () => {
  await fixture(async ({ ctx, seen }) => {
    const a = {}, b = {}, child = {};
    const sa = createScope(ctx, a), sb = createScope(ctx, b), sc = createScope(ctx, child, { parent: a });
    a.ctx = sa.ctx; b.ctx = sb.ctx; child.ctx = sc.ctx;
    try {
      registerMcpCapabilityPolicy(a.ctx, { serverName: 'scoped', allowedTools: ['read'] });
      register(a.ctx, 'mcp__scoped__read', seen);
      register(b.ctx, 'mcp__scoped__read', seen);
      register(child.ctx, 'pwsh', seen);
      denied(await ctx.tools.execute(exec('pwsh', {}, child)));
      denied(await ctx.tools.execute(exec('mcp__scoped__read', {}, b)));
      assert.equal((await ctx.tools.execute(exec('mcp__scoped__read', {}, child))).isError, false);
      assert.deepEqual(seen, ['mcp__scoped__read']);
    } finally { await sc.dispose(); await sb.dispose(); await sa.dispose(); }
  });
});

test('a child MCP name shadow cannot reuse the global server allowlist', async () => {
  await fixture(async ({ ctx, seen }) => {
    registerMcpCapabilityPolicy(ctx, { serverName: 'fixture', allowedTools: ['read'] });
    register(ctx, 'mcp__fixture__read', seen);
    const child = {}; const scope = createScope(ctx, child); child.ctx = scope.ctx;
    try {
      register(child.ctx, 'mcp__fixture__read', seen);
      denied(await ctx.tools.execute(exec('mcp__fixture__read', {}, child)));
      assert.deepEqual(seen, []);
    } finally { await scope.dispose(); }
  });
});

test('internal browser and original trusted tools remain executable', async () => {
  await fixture(async ({ ctx, seen }) => {
    registerMcpCapabilityPolicy(ctx, { serverName: 'campus_browser' });
    register(ctx, 'mcp__campus_browser__browser_evaluate', seen);
    denied(await ctx.tools.execute(exec('mcp__campus_browser__browser_evaluate')));
    for (const name of ['mcp__campus_browser__browser_snapshot', 'campus_courses', 'web_search', 'create_goal', 'subagent']) register(ctx, name, seen);
    for (const name of [...['mcp__campus_browser__browser_snapshot', 'campus_courses', 'web_search', 'create_goal', 'subagent']]) {
      assert.equal((await ctx.tools.execute(exec(name))).isError, false, name);
    }
    assert.equal(seen.length, 5);
  });
});

test('real filesystem tool reads workspace/declared skills; strict host and credentials boundaries', async () => {
  await fixture(async ({ ctx, directory, workspace, skills }) => {
    applyFs(ctx, FsConfig({}));
    await writeFile(join(workspace, 'course.txt'), 'course material');
    await writeFile(join(skills, 'SKILL.md'), 'declared skill');
    await writeFile(join(directory, 'outside.txt'), 'fixture-private-value');
    await writeFile(join(workspace, '.env'), 'fixture-private-value');
    await mkdir(join(workspace, 'secrets'));
    await writeFile(join(workspace, 'secrets', 'provider.key'), 'fixture-private-value');
    const agent = { session: { header: { cwd: workspace } } };
    assert.equal((await ctx.tools.execute(exec('read', { file_path: 'course.txt' }, agent))).isError, false);
    assert.equal((await ctx.tools.execute(exec('read', { file_path: join(skills, 'SKILL.md') }, agent))).isError, false);
    for (const path of ['../outside.txt', '.env', 'secrets/provider.key', '\\\\host\\share\\secret', 'C:relative', 'course.txt:hidden']) {
      denied(await ctx.tools.execute(exec('read', { file_path: path }, agent)));
    }
    agent.session.header.cwd = directory;
    denied(await ctx.tools.execute(exec('read', { file_path: 'outside.txt' }, agent)));
    assert.equal(isPathWithin(workspace + '-neighbor', workspace), false);
  });
});

test('read_image/write/edit/publish and recursive search are fenced before bodies', async () => {
  await fixture(async ({ ctx, seen, directory, workspace, skills }) => {
    for (const name of ['read_image', 'write', 'edit', 'publish_file', 'glob', 'grep']) register(ctx, name, seen);
    await writeFile(join(workspace, 'safe.txt'), 'safe');
    await writeFile(join(workspace, '.env'), 'fixture-private-value');
    for (const name of ['read_image', 'write', 'edit']) denied(await ctx.tools.execute(exec(name, { file_path: join(directory, 'outside') })));
    denied(await ctx.tools.execute(exec('write', { file_path: join(skills, 'new.txt') })));
    denied(await ctx.tools.execute(exec('write', { file_path: 'new.txt', sandbox_permissions: 'danger-full-access' })));
    denied(await ctx.tools.execute(exec('publish_file', { path: '../outside' })));
    for (const name of ['glob', 'grep']) {
      denied(await ctx.tools.execute(exec(name, { pattern: '*', path: workspace })));
      denied(await ctx.tools.execute(exec(name, { pattern: '*', path: directory })));
      assert.equal((await ctx.tools.execute(exec(name, { pattern: '*', path: join(workspace, 'safe.txt') }))).isError, false);
    }
    assert.deepEqual(seen, ['glob', 'grep']);
  });
});

test('canonical symlink escape denied and recursive traversal refuses links', async t => {
  await fixture(async ({ ctx, seen, directory, workspace }) => {
    register(ctx, 'read', seen); register(ctx, 'grep', seen);
    const link = join(workspace, 'escape');
    try { await symlink(directory, link, process.platform === 'win32' ? 'junction' : 'dir'); }
    catch (error) { if (error.code === 'EPERM') { t.skip('symlink privilege unavailable'); return; } throw error; }
    await writeFile(join(directory, 'outside.txt'), 'fixture-private-value');
    denied(await ctx.tools.execute(exec('read', { file_path: join(link, 'outside.txt') })));
    denied(await ctx.tools.execute(exec('grep', { path: workspace, pattern: 'fixture' })));
    assert.deepEqual(seen, []);
  });
});

test('exact attachment file option does not authorize sibling files or its directory', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'campus-attachment-policy-'));
  try {
    const attachment = join(directory, 'selected.txt'); await writeFile(attachment, 'selected');
    await fixture(async ({ ctx, seen }) => {
      register(ctx, 'read', seen); register(ctx, 'glob', seen);
      assert.equal((await ctx.tools.execute(exec('read', { file_path: attachment }))).isError, false);
      denied(await ctx.tools.execute(exec('read', { file_path: join(directory, 'unselected.txt') })));
      denied(await ctx.tools.execute(exec('glob', { path: directory, pattern: '*' })));
      assert.deepEqual(seen, ['read']);
    }, { attachmentPaths: [attachment], privateRoots: [directory] });
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('a configured MCP server with no project whitelist exposes its own tools only', async () => {
  await fixture(async ({ ctx, seen }) => {
    // No allowedTools: the user configured and enabled this server, so its own tools
    // are usable; another server's namespace and MCP resource surfaces are not.
    registerMcpCapabilityPolicy(ctx, { serverName: 'fixture' });
    for (const name of ['mcp__fixture__add', 'mcp__other__add', 'list_mcp_resources']) register(ctx, name, seen);
    registerToolLoader(ctx);
    const agent = {}; const scope = createScope(ctx, agent); agent.ctx = scope.ctx; installToolMask(agent);
    try {
      assert.equal((await ctx.tools.execute(exec('load_tools', { group: 'mcp' }, agent))).isError, false);
      denied(await ctx.tools.execute(exec('mcp__other__add', {}, agent)));
      denied(await ctx.tools.execute(exec('list_mcp_resources', {}, agent)));
      assert.equal((await ctx.tools.execute(exec('mcp__fixture__add', {}, agent))).isError, false);
      assert.deepEqual(seen, ['mcp__fixture__add']);
    } finally { await scope.dispose(); }
  });
});

test('an explicit whitelist still refuses names outside it', async () => {
  await fixture(async ({ ctx, seen }) => {
    registerMcpCapabilityPolicy(ctx, { serverName: 'fixture', allowedTools: ['read_course'] });
    for (const name of ['mcp__fixture__read_course', 'mcp__fixture__delete_course']) register(ctx, name, seen);
    registerToolLoader(ctx);
    const agent = {}; const scope = createScope(ctx, agent); agent.ctx = scope.ctx; installToolMask(agent);
    try {
      assert.equal((await ctx.tools.execute(exec('load_tools', { group: 'mcp' }, agent))).isError, false);
      denied(await ctx.tools.execute(exec('mcp__fixture__delete_course', {}, agent)));
      assert.equal((await ctx.tools.execute(exec('mcp__fixture__read_course', {}, agent))).isError, false);
      assert.deepEqual(seen, ['mcp__fixture__read_course']);
    } finally { await scope.dispose(); }
  });
});

test('project attributes accepted by MCP Config; transport credentials remain private', async () => {
  const config = { serverName: 'fixture', transport: 'stdio', command: 'unused', allowedTools: ['read'] };
  assert.deepEqual(McpConfig(config).allowedTools, ['read']);
  assert.deepEqual(McpConfig({ ...config, allowedTools: undefined }).allowedTools, []);
  assert(!JSON.stringify(mcpConfig({ ...config, env: { PRIVATE: 'fixture-private-value' }, headers: { Authorization: 'fixture-private-value' } })).includes('fixture-private-value'));
  assert.deepEqual(credentialConfig(config, '{"fixture":{"env":{"PRIVATE":"fixture-private-value"}}}').allowedTools, ['read']);
  for (const allowedTools of [['*'], ['read', 'read'], ['namespace.read'], ['x'.repeat(64)]]) {
    assert.throws(() => McpConfig({ ...config, allowedTools }));
  }
});

test('MCP all-server credential vault is private, never inherited by stdio children', () => {
  const prior = process.env.CAMPUS_MCP_CREDENTIALS;
  try {
    process.env.CAMPUS_MCP_CREDENTIALS = JSON.stringify({ a: { env: { PRIVATE: 'a-private' } }, b: { env: { PRIVATE: 'b-private' } } });
    const a = claimCredentialConfig({ serverName: 'a', transport: 'stdio' });
    assert.equal(a.env.PRIVATE, 'a-private');
    assert.equal(process.env.CAMPUS_MCP_CREDENTIALS, undefined);
    assert.equal(scrubbedParentEnv().CAMPUS_MCP_CREDENTIALS, undefined);
    const b = claimCredentialConfig({ serverName: 'b', transport: 'stdio' });
    assert.equal(b.env.PRIVATE, 'b-private');
    assert(!JSON.stringify(a).includes('b-private'));
  } finally {
    if (prior === undefined) delete process.env.CAMPUS_MCP_CREDENTIALS;
    else process.env.CAMPUS_MCP_CREDENTIALS = prior;
  }
});

test('monotonic guard still denies shell and unknown MCP if another pre-policy short-circuits allow', async () => {
  await fixture(async ({ ctx, seen }) => {
    register(ctx, 'pwsh', seen); register(ctx, 'mcp__unknown__read', seen);
    ctx.on('tools/pre-execute', () => ({ kind: 'allow' }), { prepend: true });
    denied(await ctx.tools.execute(exec('pwsh')));
    denied(await ctx.tools.execute(exec('mcp__unknown__read')));
    assert.deepEqual(seen, []);
  });
});

test('primitive fs read fence covers skill/instruction paths without tool middleware', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'campus-fs-primitive-'));
  const workspace = join(directory, 'workspace'), skillDir = join(directory, 'skills');
  await mkdir(workspace); await mkdir(skillDir);
  const ctx = new Context();
  ctx.provide('sandboxPolicy', { defaultMode: 'workspace-write', resolve: () => ({ mode: 'workspace-write', workspaceRoot: workspace }) });
  try {
    const fs = new CampusFileSystem(ctx, CampusFileSystem.Config({ cwd: workspace, workspace, skillDirs: [skillDir] }));
    await writeFile(join(workspace, 'safe.txt'), 'safe');
    await writeFile(join(skillDir, 'SKILL.md'), 'declared skill');
    await writeFile(join(directory, 'host.txt'), 'fixture-private-value');
    await writeFile(join(workspace, '.env'), 'fixture-private-value');
    assert.equal(await fs.readText(await fs.resolve('safe.txt')), 'safe');
    assert.equal(await fs.readText(await fs.resolve(join(skillDir, 'SKILL.md'))), 'declared skill');
    for (const path of [join(directory, 'host.txt'), join(workspace, '.env')]) {
      for (const method of ['readText', 'streamText', 'readBytes']) await assert.rejects(fs[method](await fs.resolve(path)), error =>
        error.code === 'CAMPUS_CAPABILITY_DENIED' && !error.message.includes('fixture-private-value'));
    }
  } finally { await ctx.fiber.dispose(); await rm(directory, { recursive: true, force: true }); }
});

test('generated profile disables code execution and pins trusted path config without secrets', async () => {
  const home = await mkdtemp(join(tmpdir(), 'campus-profile-capability-'));
  try {
    await prepareProfile(home, { workspace: join(home, 'workspace'), mcpServers: [{ serverName: 'fixture',
      transport: 'stdio', command: 'unused', allowedTools: ['read'], env: { PRIVATE: 'fixture-private-value' } }] });
    const serialized = await readFile(join(home, 'profiles/campus/cordis.patch.yml'), 'utf8');
    assert(!serialized.includes('fixture-private-value'));
    const patch = JSON.parse(serialized);
    for (const id of ['tool-bash', 'tool-pwsh', 'tool-workflow', 'workflow-ptc', 'ptc-runtime']) assert.equal(patch.find(row => row.id === id).disabled, true);
    assert.equal(patch.find(row => row.id === 'skill-filesystem').config.includeDefaultRoots, false);
    assert.deepEqual(patch.find(row => row.id === 'agent-instructions').config.projectRootMarkers, []);
    assert(patch.find(row => row.id === 'fs-sandbox').name.endsWith('/capability-policy.mjs'));
    const entries = patch.find(row => row.insert).insert;
    assert.equal(entries.find(row => row.id === 'campus').config.workspace, resolve(home, 'workspace'));
    assert.deepEqual(entries.find(row => row.id === 'campus-mcp-0').config.allowedTools, ['read']);
  } finally { await rm(home, { recursive: true, force: true }); }
});
