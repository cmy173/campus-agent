// Thin public Harness ToolRegistry adapter; Python owns scheduling, never an LLM loop.
const timing = {
  type: 'object', additionalProperties: false, required: ['kind', 'time_zone'], properties: {
    kind: { type: 'string', enum: ['at', 'after', 'every', 'daily', 'weekly'] },
    time_zone: { type: 'string', description: 'Explicit IANA zone, e.g. Asia/Shanghai. Never infer ambiguous dates silently.' },
    at: { type: 'string', description: 'Future ISO 8601 date/time with explicit UTC offset; kind=at.' },
    seconds: { type: 'integer', minimum: 1, description: 'kind=after: delay; kind=every: fixed interval, minimum 60.' },
    time: { type: 'string', description: 'HH:MM:SS local wall-clock time for daily/weekly.' },
    weekdays: { type: 'array', items: { type: 'integer', minimum: 1, maximum: 7 }, description: 'ISO weekdays 1 Monday–7 Sunday; weekly only.' },
  },
};
const content = {
  title: { type: 'string', minLength: 1, maxLength: 120 },
  prompt: { type: 'string', minLength: 1, maxLength: 6000, description: 'Instruction to execute in this same conversation when due. External submission still requires concrete review.' },
  timing,
  missed_policy: { type: 'string', enum: ['latest', 'skip'], description: 'latest: one latest missed occurrence on restart; skip: discard occurrences missed while application was stopped.' },
};

export function mountSchedules(ctx, bridge) {
  const specs = [
    ['schedule_create', 'create', 'Create a persistent local task in this conversation. Application must be running to execute; sleep/shutdown do not wake the computer. Return the exact time, time zone, missed policy and task ID to the user. DST gaps are skipped; repeated times use the earlier instant once.', { ...content }, ['title', 'prompt', 'timing']],
    ['schedule_list', 'list', 'List active and disabled local tasks with exact next execution time and revision. Task cancellation does not disable future recurrences; explicitly disable a schedule when requested.', {}, []],
    ['schedule_update', 'update', 'Edit, disable or resume a local task using the current revision from schedule_list. Disabling cancels occurrences not yet admitted; an already running turn must be stopped separately. Timing changes reanchor in the future.', { id: { type: 'string' }, revision: { type: 'integer', minimum: 1 }, ...content, enabled: { type: 'boolean' } }, ['id', 'revision']],
    ['schedule_delete', 'delete', 'Delete future scheduling using the current revision. Preserve admission and execution history; already admitted turns are not canceled or replayed.', { id: { type: 'string' }, revision: { type: 'integer', minimum: 1 } }, ['id', 'revision']],
    ['schedule_history', 'history', 'Read scheduled occurrence receipts and actual turn status. Admission is not successful task completion; interrupted/failed turns are never automatically replayed.', { id: { type: 'string' } }, ['id']],
  ];
  for (const [name, operation, description, properties, required] of specs) {
    ctx.effect(() => ctx.tools.register({
      name, description, parameters: { type: 'object', additionalProperties: false, properties, required },
      output: { schema: { type: 'object' }, render: (_args, result) => [{ type: 'text', text: JSON.stringify(result) }] },
      isConcurrencySafe: false, timeoutMs: 30000,
      async execute(args, exec) {
        const { id, ...arguments_ } = args;
        return bridge('/api/v1/agent/internal/schedules', {
          conversation_id: process.env.CAMPUS_CONVERSATION_ID || String(exec.agent.session.id),
          call_id: String(exec.agent.session.id) + ':' + String(exec.callId), operation,
          ...(id ? { id } : {}), arguments: arguments_,
        }, exec.signal);
      },
    }));
  }
}
