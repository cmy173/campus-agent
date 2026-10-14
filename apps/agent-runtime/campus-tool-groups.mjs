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
 * Install the loader tool plus the scope mask.
 * @param ctx agent-scoped plugin context carrying the tool registry
 */
export function installOnDemandTools(ctx) {
  if ((process.env.CAMPUS_TOOL_DISCOVERY || 'on') === 'off') return;
  const maskEnabled = (process.env.CAMPUS_TOOL_MASK || 'on') !== 'off';
  const lifted = new Set();
  let current = null;
  let logged = false;
  let applying = false;

  const catalog = () => ctx.tools.schemas().map(schema => schema.name);

  /**
   * Replace the scope mask with one matching the live catalog.
   *
   * Never re-entered: `restrict` changes availability, which emits `tools/change`,
   * and mutating the registry from inside that notification would recurse.
   */
  function reapply() {
    if (applying) return null;
    applying = true;
    try {
      const { deferred, counts } = deferredNames(catalog(), lifted);
      const previous = current;
      current = null;
      if (previous) previous();
      if (deferred.length && maskEnabled) {
        try {
          current = ctx.tools.restrict({ deny: deferred });
        } catch (error) {
          // A concurrent registration or scope change must not break the turn.
          if (!logged) {
            logged = true;
            console.error('[campus] tool mask skipped:', error?.message || error);
          }
        }
      }
      return counts;
    } finally {
      applying = false;
    }
  }

  /** Registry mutations stay off the dispatch that asked for them. */
  function reapplyLater() {
    setTimeout(() => { reapply(); }, 0);
  }

  const loader = ctx.tools.register({
    name: 'load_tools',
    description: loaderDescription(deferredNames(catalog(), lifted).counts),
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
    async execute(args) {
      const before = catalog();
      console.error('[campus] load_tools start', JSON.stringify(args), before.length);
      const requested = new Set(Array.isArray(args.tools) ? args.tools : []);
      for (const group of GROUPS) {
        if (args.group === group.id || before.some(name => requested.has(name) && group.match(name))) {
          lifted.add(group.id);
        }
      }
      const counts = deferredNames(before, new Set()).counts;
      // The requesting step already has its schema list; lifted tools become
      // visible from the next request, so the mask updates off this dispatch.
      reapplyLater();
      const hidden = new Set(deferredNames(before, lifted).deferred);
      console.error('[campus] load_tools end', before.length, hidden.size);
      return {
        available: before.filter(name => !hidden.has(name)),
        loaded_groups: [...lifted],
        note: Object.entries(counts).map(([id, count]) => `${id}:${count}`).join(' ')
          + '；本组工具在下一轮请求生效。',
      };
    },
  });

  const unsubscribe = ctx.on('tools/change', () => { reapplyLater(); });
  ctx.effect(() => {
    reapply();
    return () => {
      unsubscribe();
      loader();
      if (current) { current(); current = null; }
    };
  });
}
