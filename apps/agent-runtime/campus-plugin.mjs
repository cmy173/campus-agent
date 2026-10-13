import { LlmAdapter, attributionHeaders } from '@deepseek-ai/dsh-llm';
import { Service } from '@deepseek-ai/cordis';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { mountControls } from './campus-control.mjs';

export const name = 'campus';
export const inject = ['tools', 'llm', 'shellEnv', 'agents', 'subagents'];

async function bridge(path, payload, signal) {
  const base = process.env.CAMPUS_BRIDGE_URL;
  if (!base || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) throw new Error('Campus bridge must be an explicit loopback endpoint');
  const response = await fetch(base + path, {
    method: 'POST', signal, redirect: 'error',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + process.env.CAMPUS_BRIDGE_TOKEN },
    body: JSON.stringify(payload),
  });
  const value = await response.json();
  if (!response.ok) throw Object.assign(new Error(value.error?.message || 'Campus bridge request failed'), {
    code: value.error?.code, retryable: value.error?.retryable,
  });
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
  ctx.effect(() => ctx.tools.register({
    name: 'publish_file',
    description: 'Deliver an existing local file to the user as a downloadable snapshot in the conversation file workspace. Use after writing documents, spreadsheets, PDFs or other task results. Relative paths resolve in the task directory. Reading a file is not delivery; publish each finished output.',
    parameters: { type: 'object', additionalProperties: false, required: ['path'], properties: {
      path: { type: 'string', minLength: 1, description: 'Existing file path to publish.' },
    } },
    output: { schema: { type: 'object', additionalProperties: false,
      required: ['id', 'conversation_id', 'kind', 'name', 'size', 'sha256', 'mime_type', 'created_at'],
      properties: { id: { type: 'string' }, conversation_id: { type: 'string' }, kind: { type: 'string', enum: ['input', 'output'] },
        name: { type: 'string' }, size: { type: 'integer' }, sha256: { type: 'string' }, mime_type: { type: 'string' }, created_at: { type: 'string' } },
    }, render: (_args, item) => [{ type: 'text', text: JSON.stringify(item) }] },
    isConcurrencySafe: true, timeoutMs: 30000,
    async execute(args, exec) {
      const item = await bridge('/api/v1/agent/internal/files/publish', {
        conversation_id: process.env.CAMPUS_CONVERSATION_ID || String(exec.agent.session.id),
        call_id: String(exec.agent.session.id) + ':' + String(exec.callId), path: args.path,
      }, exec.signal);
      return item;
    },
  }));
  ctx.on('tools/pre-execute', async (exec, next) => {
    if (!exec.name.startsWith('campus_')) {
      await bridge('/api/v1/agent/internal/tool-lease', {
        conversation_id: process.env.CAMPUS_CONVERSATION_ID || String(exec.agent.session.id),
        call_id: String(exec.agent.session.id) + ':' + String(exec.callId), name: exec.name,
      }, exec.signal);
    }
    return next();
  });
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
    const conversation_id = process.env.CAMPUS_CONVERSATION_ID || String(options.sessionId);
    try {
      if (options.provider !== 'campus-model') {
        await bridge('/api/v1/agent/internal/model-lease', { conversation_id }, options.signal);
      }
      for await (const chunk of next()) {
        if (options.provider !== 'campus-model' && chunk.type === 'finish' && ['error', 'aborted'].includes(chunk.reason?.kind)) {
          const failure = nativeModelFailure(chunk.reason.failure);
          if (chunk.reason.kind === 'error' && chunk.reason.failure?.code !== 'IMAGE_OFFLOAD_REQUIRED' && !options.signal?.aborted) {
            try { await bridge('/api/v1/agent/internal/model-error', { conversation_id, ...failure }); }
            catch { /* Keep the upstream failure authoritative when journaling is unavailable. */ }
          }
          // The upstream session log also records finish chunks. Preserve error
          // classification/retry timing while discarding untrusted remote text.
          const original = chunk.reason.failure ?? { code: 'ABORTED' };
          yield { ...chunk, reason: { ...chunk.reason, failure: {
            code: original.code, message: chunk.reason.kind === 'aborted' ? '本轮模型请求已停止。' : failure.message,
            ...(Number.isInteger(original.status) ? { status: original.status } : {}),
            ...(Number.isFinite(original.providerRetryAfterMs) ? { providerRetryAfterMs: original.providerRetryAfterMs } : {}),
            // Image compaction uses this public recovery hint to retry the same
            // request. Redacting diagnostic text must not disable recovery.
            ...(Number.isSafeInteger(original.offloadImages) && original.offloadImages > 0
              ? { offloadImages: original.offloadImages } : {}),
          } } };
          continue;
        }
        yield chunk;
      }
    } catch (error) {
      if (options.provider !== 'campus-model' && !options.signal?.aborted) {
        const failure = nativeModelFailure(error);
        try { await bridge('/api/v1/agent/internal/model-error', { conversation_id, ...failure }); }
        catch { /* A stopped turn cannot create new runtime-authorized events. */ }
      }
      throw error;
    }
  });
  // This service appears only after schemas and adapters are registered. SDK
  // initialization must wait for it; async plugin apply alone is not a fence.
  await mountControls(ctx, bridge);
  class CampusReady extends Service {
    constructor(context) { super(context, 'campusReady'); }
  }
  new CampusReady(ctx);
}

/** Never copy a remote response body or provider exception into the app journal. */
export function nativeModelFailure(error) {
  const mapped = {
    AUTH: ['PROVIDER_AUTH_FAILED', 'DeepSeek 服务拒绝了凭据，请检查模型密钥与权限。', false],
    RATE_LIMIT: ['PROVIDER_RATE_LIMITED', 'DeepSeek 服务限流，请稍后重试。', true],
    TIMEOUT: ['PROVIDER_TIMEOUT', 'DeepSeek 模型请求超时，可重试或调整运行预算。', true],
    TRANSPORT: ['PROVIDER_UNREACHABLE', '无法连接 DeepSeek 模型服务，请检查网络。', true],
    MALFORMED_RESPONSE: ['PROVIDER_INVALID_RESPONSE', 'DeepSeek 返回的流式响应格式不完整。', true],
    STREAM_CLOSED: ['PROVIDER_INVALID_RESPONSE', 'DeepSeek 流式响应提前结束。', true],
    EMPTY_RESPONSE: ['PROVIDER_INVALID_RESPONSE', 'DeepSeek 没有返回可用回答。', true],
    PROVIDER_CHANGED: ['PROVIDER_CHANGED', '模型配置已变化，请新建对话。', false],
    BUDGET_EXCEEDED: ['BUDGET_EXCEEDED', '本轮模型调用达到配置预算。', false],
    CONSENT_REQUIRED: ['CONSENT_REQUIRED', '尚未同意向当前模型发送对话。', false],
  };
  const [code, message, retryable] = mapped[error?.code] ??
    ['PROVIDER_HTTP_ERROR', 'DeepSeek 模型调用未完成，请检查模型配置与服务状态。', true];
  return { code, message, retryable };
}
