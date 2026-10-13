import { LlmAdapter, attributionHeaders } from '@deepseek-ai/dsh-llm';
import { Service } from '@deepseek-ai/cordis';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const name = 'campus';
export const inject = ['tools', 'llm', 'shellEnv'];

async function bridge(path, payload, signal) {
  const base = process.env.CAMPUS_BRIDGE_URL;
  if (!base || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) throw new Error('Campus bridge must be an explicit loopback endpoint');
  const response = await fetch(base + path, {
    method: 'POST', signal, redirect: 'error',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + process.env.CAMPUS_BRIDGE_TOKEN },
    body: JSON.stringify(payload),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error?.message || 'Campus bridge request failed');
  return value;
}

export class CampusModelAdapter extends LlmAdapter {
  async *stream(options) {
    const result = await bridge('/api/v1/agent/internal/model', {
      conversation_id: process.env.CAMPUS_CONVERSATION_ID || String(options.sessionId), messages: options.messages,
      tools: options.tools || [], max_tokens: options.maxTokens || 2048,
      attribution_headers: attributionHeaders(),
    }, options.signal);
    let index = 0;
    if (result.content) {
      yield { type: 'block-start', index, blockType: 'text' };
      yield { type: 'text-delta', index, text: result.content };
      yield { type: 'block-end', index, block: { type: 'text', text: result.content } };
      index++;
    }
    for (const call of result.tool_calls || []) {
      yield { type: 'block-start', index, blockType: 'tool-call' };
      yield { type: 'tool-call-delta', index, id: call.id, name: call.name, argumentsDelta: JSON.stringify(call.arguments) };
      yield { type: 'block-end', index, block: { type: 'tool-call', id: call.id, name: call.name, arguments: JSON.stringify(call.arguments) } };
      index++;
    }
    yield { type: 'finish', reason: (result.tool_calls || []).length ? 'tool-calls' : 'stop' };
  }
}

export async function apply(ctx) {
  const python = process.env.CAMPUS_PYTHON_EXECUTABLE || fileURLToPath(new URL('../api/.venv/Scripts/python.exe', import.meta.url));
  ctx.effect(() => ctx.shellEnv.register({
    name: 'campus-executables',
    variables: { DSH_PYTHON_EXECUTABLE: { description: 'Absolute path to the Campus Python environment; invoke with & $env:DSH_PYTHON_EXECUTABLE.' } },
    resolve: () => ({ DSH_PYTHON_EXECUTABLE: resolve(python) }),
  }));
  const inventory = await bridge('/api/v1/agent/internal/schemas', {});
  // The registry owns tool availability. Domain validation stays in the campus
  // bridge; a global campus-only guard would block upstream and MCP tools.
  for (const spec of inventory) {
    ctx.effect(() => ctx.tools.register({
      name: spec.model_name, description: spec.description, parameters: spec.input_schema,
      // Typed domain envelope comes from the strict server-side contract.
      output: {
        schema: {
          type: 'object', additionalProperties: false,
          required: ['status', 'data', 'evidence', 'coverage', 'freshness', 'warnings', 'next_cursor', 'job_id', 'error'],
          properties: {
            status: { type: 'string', enum: ['ok', 'partial', 'queued', 'needs_user', 'error'] },
            data: { type: 'object' }, evidence: { type: 'array' }, coverage: { type: 'object' },
            freshness: { type: 'object' }, warnings: { type: 'array' },
            next_cursor: { oneOf: [{ type: 'string' }, { type: 'null' }] },
            job_id: { oneOf: [{ type: 'string' }, { type: 'null' }] },
            error: { oneOf: [{ type: 'object' }, { type: 'null' }] },
          },
        },
        render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
      },
      timeoutMs: 20000, isConcurrencySafe: spec.effects === 'read',
      async execute(args, exec) {
        return bridge('/api/v1/agent/internal/tool', {
          name: spec.name, arguments: args,
          conversation_id: process.env.CAMPUS_CONVERSATION_ID || String(exec.agent.session.id),
          call_id: String(exec.agent.session.id) === process.env.CAMPUS_CONVERSATION_ID || !process.env.CAMPUS_CONVERSATION_ID
            ? String(exec.callId) : String(exec.agent.session.id) + ':' + String(exec.callId),
        }, exec.signal);
      },
    }));
  }
  ctx.effect(() => ctx.llm.registerAdapter(['campus-model'], new CampusModelAdapter()));
  ctx.on('llm/stream', async function* (options, next) {
    if (options.provider !== 'campus-model') {
      await bridge('/api/v1/agent/internal/model-lease', {
        conversation_id: process.env.CAMPUS_CONVERSATION_ID || String(options.sessionId),
      }, options.signal);
    }
    yield* next();
  });
  // This service appears only after schemas and adapters are registered. SDK
  // initialization must wait for it; async plugin apply alone is not a fence.
  class CampusReady extends Service {
    constructor(context) { super(context, 'campusReady'); }
  }
  new CampusReady(ctx);
}
