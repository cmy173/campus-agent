import { scopeChainOf } from '@deepseek-ai/dsh-scope';

/**
 * On-demand tool discovery for the Campus agent scope.
 *
 * Capability breadth must not mean shipping every tool schema on every request:
 * the default surface keeps the tools an ordinary campus task needs, and the rest
 * stay out of the model context until the model (or the user's wording) asks for
 * them. This uses the pinned upstream mechanism rather than a second loop:
 * `ctx.tools.restrict({ deny })` masks global tools for the calling agent scope,
 * `ctx.tools.schemas(agent)` is the live catalog, and `tools/change` re-applies the
 * mask when another plugin (MCP, teams) registers later.
 *
 * Unclassified tools stay available. Registry mutations are synchronous and
 * guarded against their own notifications, without timers or SDK wrappers.
 */

/** Always visible: one entry per group the model can pull in on demand. */
export const GROUPS = [
  {
    id: 'browser',
    label: '登录态浏览器',
    hint: '读取/操作已登录网页、下载附件、填写并提交表单',
    match: name => name.startsWith('mcp__campus_browser__'),
  },
  {
    id: 'mcp',
    label: 'MCP 服务与资源',
    hint: '用户配置的 MCP 服务工具与资源',
    match: name => (name.startsWith('mcp__') && !name.startsWith('mcp__campus_browser__'))
      || name === 'list_mcp_resources' || name === 'list_mcp_resource_templates' || name === 'read_mcp_resource',
  },
  {
    id: 'team',
    label: '子 Agent 与并行协作',
    hint: '把独立子任务分给子 Agent、查看进度、汇总或停止',
    match: name => ['subagent', 'subagent_fork', 'send_message', 'list_agents', 'interrupt_agent'].includes(name)
      || name.startsWith('team_task_'),
  },
  {
    id: 'jobs',
    label: '后台任务与工作流',
    hint: '长时间后台任务、脚本化流程及其结果',
    match: name => name.startsWith('job_') || name === 'workflow',
  },
  {
    id: 'planning',
    label: '计划模式',
    hint: '先出计划再执行的任务模式',
    match: name => name === 'exit_plan_mode',
  },
];

/** Tools that must never be masked: the loader itself and Campus domain tools. */
function protectedName(name) {
  return name === 'load_tools' || name === 'publish_file' || name.startsWith('campus_')
    || name.startsWith('schedule_') || ['create_goal', 'get_goal', 'update_goal'].includes(name);
}

export function groupOf(name) {
  return GROUPS.find(group => group.match(name)) || null;
}

/**
 * Names to hide right now, given the live catalog and the groups already lifted.
 * @returns deferred names, and a per-group count for the loader description.
 */
export function deferredNames(catalog, lifted) {
  const deferred = [];
  const counts = Object.fromEntries(GROUPS.map(group => [group.id, 0]));
  for (const name of catalog) {
    if (protectedName(name)) continue;
    const group = groupOf(name);
    if (!group) continue; // Unclassified tools stay available.
    counts[group.id]++;
    if (!lifted.has(group.id)) deferred.push(name);
  }
  return { deferred, counts };
}

export function loaderDescription(counts) {
  const lines = GROUPS.map(group => `- ${group.id}（${group.label}，${counts[group.id]} 项可见）：${group.hint}`);
  return '按需加载更多工具。默认只提供普通校园任务常用的工具；需要浏览器操作、MCP 服务、'
    + '子 Agent 协作、后台任务或计划模式时，先调用本工具再使用它们。'
    + `可选分组：\n${lines.join('\n')}`
    + '\n也可以传 tools 指定具体工具名。返回本次实际可见的工具名。';
}

/** Each restriction is owned by one live agent and has an exact disposer. */
const stateOf = new WeakMap();
function agentState(agent) {
  let state = stateOf.get(agent);
  if (!state) {
    state = { lifted: new Set(), masks: new Map(), catalog: new Set(), applying: false };
    stateOf.set(agent, state);
  }
  return state;
}

export function applyMask(agent) {
  const state = agentState(agent);
  if (state.applying) return;
  state.applying = true;
  try {
    for (const [name, dispose] of state.masks) {
      if (state.lifted.has(groupOf(name)?.id)) {
        state.masks.delete(name);
        dispose();
      }
    }
    // schemas() without an explicit scope reads GLOBAL tools, not agent tools.
    // Only add masks for newly visible definitions. Our own change notification
    // is ignored; no dispose/restrict timer loop and no next-request race.
    for (const { name } of agent.ctx.tools.schemas(agent)) {
      state.catalog.add(name);
      const group = groupOf(name);
      if (!protectedName(name) && group && !state.lifted.has(group.id) && !state.masks.has(name)) {
        state.masks.set(name, agent.ctx.tools.restrict({ deny: [name] }));
      }
    }
  } finally {
    state.applying = false;
  }
}

export function installToolMask(agent) {
  if (!agent?.ctx?.tools || stateOf.has(agent)) return;
  const state = agentState(agent);
  agent.ctx.on('tools/change', () => {
    if (!state.applying) applyMask(agent);
  });
  applyMask(agent);
}

/**
 * Register the on-demand loader once for the whole composition.
 *
 * The loader lives in the shared composition; only masks belong to agents.
 * It is never masked (`protectedName`).
 */
export function registerToolLoader(ctx) {
  return ctx.tools.register({
    name: 'load_tools',
    description: '按需加载更多工具。默认只提供普通校园任务常用的工具；需要浏览器操作、MCP 服务、'
      + '子 Agent 协作、后台任务或计划模式时，先调用本工具再使用它们。'
      + `可选分组：\n${GROUPS.map(group => `- ${group.id}（${group.label}）：${group.hint}`).join('\n')}`
      + '\n也可以传 tools 指定具体工具名。返回本次实际可见的工具名，新加载的分组在下一轮请求生效。',
    parameters: {
      type: 'object', additionalProperties: false,
      properties: {
        group: { type: 'string', enum: GROUPS.map(group => group.id), description: '要加载的工具分组。' },
        tools: { type: 'array', items: { type: 'string' }, description: '要加载的具体工具名（可替代 group）。' },
      },
    },
    output: {
      schema: {
        type: 'object', additionalProperties: false,
        required: ['available', 'loaded_groups', 'note'],
        properties: {
          available: { type: 'array', items: { type: 'string' } },
          loaded_groups: { type: 'array', items: { type: 'string' } },
          note: { type: 'string' },
        },
      },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    isConcurrencySafe: true,
    async execute(args, exec) {
      const agent = exec?.agent;
      const tools = agent?.ctx?.tools;
      if (!tools) return { available: [], loaded_groups: [], note: '当前调用没有可用的工具作用域。' };
      const state = agentState(agent);
      const chain = scopeChainOf(agent).filter(scope => stateOf.has(scope));
      const requested = new Set(Array.isArray(args.tools) ? args.tools : []);
      const catalog = new Set(chain.flatMap(scope => [...agentState(scope).catalog]));
      const groups = GROUPS.filter(group => args.group === group.id
        || [...catalog].some(name => requested.has(name) && group.match(name)));
      // A parent's restriction is inherited by a child. Release only OUR masks
      // along that public scope chain; never lift upstream capability policies.
      for (const scope of chain) for (const group of groups) agentState(scope).lifted.add(group.id);
      for (const scope of [...chain].reverse()) applyMask(scope);
      applyMask(agent);
      return {
        available: tools.schemas(agent).map(schema => schema.name),
        loaded_groups: [...state.lifted],
        note: groups.length ? '所请求分组已加载；后续模型请求可使用。' : '没有匹配的分组或工具。',
      };
    },
  });
}
