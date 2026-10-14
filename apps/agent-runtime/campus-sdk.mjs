/** Persistence-aware deployment of the official SDK server.
 *
 * Pinned upstream 0.2.0-rc.2's SDK server uses agents.create for every cold
 * process. Use public Cordis context extension and the public persistence/agent
 * APIs to select resume. Protocol, event handling, disposal and agent loop all
 * remain upstream code; no upstream source or private method is patched.
 */
import { apply as serveSdk, Config } from '@deepseek-ai/dsh-sdk-jsonrpc-server';

export { Config };
export const name = 'campus-sdk';
export const inject = ['agents', 'sessionPersistence', 'campusReady'];

export function apply(ctx, config) {
  const registry = ctx.agents;
  const agents = new Proxy(registry, {
    get(target, property) {
      if (property === 'create') return async options => {
        const persisted = await ctx.sessionPersistence.stat(options.sessionId);
        if (!persisted) return registry.create(options);
        return registry.resume({ resumeSessionId: options.sessionId, agentOptions: options.agentOptions,
          ...(options.signal ? { signal: options.signal } : {}) });
      };
      return Reflect.get(target, property);
    },
  });
  serveSdk(ctx.extend({ agents }), config);
}
