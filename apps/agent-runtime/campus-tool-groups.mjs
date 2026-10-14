/**
 * On-demand tool discovery for the Campus agent scope.
 *
 * Capability breadth must not mean shipping every tool schema on every request:
 * the default surface keeps the tools an ordinary campus task needs, and the rest
 * stay out of the model context until the model (or the user's wording) asks for
 * them. This uses the pinned upstream mechanism rather than a second loop:
 * `ctx.tools.restrict({ deny })` masks global tools for the calling agent scope,
 * `ctx.tools.schemas()` is the live catalog, and `tools/change` re-applies the
 * mask when another plugin (MCP, teams) registers later.
 *
 * The mask is fail-open: a tool nobody classified, or a name that vanished between
 * enumeration and application, stays visible instead of breaking the turn.
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

/**
 * Per-agent state. The loader is registered once for the whole composition (that
 * path is proven safe); the mask is applied per agent, because `restrict` only
 * governs the calling agent scope.
 *
 * `Agent` is a live object, so a WeakMap keys state without leaking agents.
 */
const stateOf = new WeakMap();

export function discoveryEnabled() {
  return (process.env.CAMPUS_TOOL_DISCOVERY || 'on') !== 'off';
}

export function maskEnabled() {
  return (process.env.CAMPUS_TOOL_MASK || 'on') !== 'off';
}

function agentState(agent) {
  let state = stateOf.get(agent);
  if (!state) {
    state = { lifted: new Set(), current: null, logged: false, applying: false };
    stateOf.set(agent, state);
  }
  return state;
}

/**
 * Replace one agent's mask with one matching its live catalog.
 *
 * Never re-entered: `restrict` changes availability, which emits `tools/change`,
 * and mutating the registry from inside that notification would recurse.
 */
export function applyMask(agent) {
  const state = agentState(agent);
  const tools = agent?.ctx?.tools;
  if (!tools || state.applying) return null;
  state.applying = true;
  try {
    const { deferred, counts } = deferredNames(tools.schemas().map(schema => schema.name), state.lifted);
    const previous = state.current;
    state.current = null;
    if (previous) previous();
    if (deferred.length && maskEnabled()) {
      try {
        state.current = tools.restrict({ deny: deferred });
      } catch (error) {
        // A concurrent registration or scope change must not break the turn.
        if (!state.logged) {
          state.logged = true;
          console.error('[campus] tool mask skipped:', error?.message || error);
        }
      }
    }
    return counts;
  } finally {
    state.applying = false;
  }
}

/** Registry mutations stay off the dispatch that asked for them. */
function applyMaskLater(agent) {
  setTimeout(() => { applyMask(agent); }, 0);
}

const SUBSCRIPTIONS = new WeakMap();

/**
 * Install the per-agent mask. Called after agent creation resolves: a registry
 * mutation made from inside `agent/created` (or the SDK create/resume setup)
 * deadlocks, because those listeners are awaited before creation resolves.
 * Safe to call repeatedly for the same agent.
 */
export function installToolMask(agent) {
  if (!discoveryEnabled() || !agent?.ctx?.tools) return;
  if (SUBSCRIPTIONS.has(agent)) return;
  const unsubscribe = agent.ctx.on('tools/change', () => { applyMaskLater(agent); });
  SUBSCRIPTIONS.set(agent, unsubscribe);
  applyMask(agent);
}

/**
 * Register the on-demand loader once for the whole composition.
 *
 * The loader must live in a scope the agent's call can resolve — registering it
 * inside the agent scope deadlocked. It is never masked (`protectedName`).
 */
export function registerToolLoader(ctx) {
  if (!discoveryEnabled()) return;
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
      const before = tools.schemas().map(schema => schema.name);
      const requested = new Set(Array.isArray(args.tools) ? args.tools : []);
      for (const group of GROUPS) {
        if (args.group === group.id || before.some(name => requested.has(name) && group.match(name))) {
          state.lifted.add(group.id);
        }
      }
      const counts = deferredNames(before, new Set()).counts;
      // The requesting step already has its schema list; lifted tools become
      // visible from the next request, so the mask updates off this dispatch.
      applyMaskLater(agent);
      const hidden = new Set(deferredNames(before, state.lifted).deferred);
      return {
        available: before.filter(name => !hidden.has(name)),
        loaded_groups: [...state.lifted],
        note: Object.entries(counts).map(([id, count]) => `${id}:${count}`).join(' ')
          + '；本组工具在下一轮请求生效。',
      };
    },
  });
}
