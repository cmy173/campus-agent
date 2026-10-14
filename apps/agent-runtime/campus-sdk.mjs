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
export const inject = ['agents', 'sessionPersistence', 'campusReady', 'goals'];

export function apply(ctx, config) {
  const registry = ctx.agents;
  const agents = new Proxy(registry, {
    get(target, property) {
      if (property === 'create') return async options => {
        const persisted = await ctx.sessionPersistence.stat(options.sessionId);
        const handle = !persisted ? await registry.create(options) : await registry.resume({ resumeSessionId: options.sessionId,
          agentOptions: options.agentOptions, ...(options.signal ? { signal: options.signal } : {}) });
        // Host-attested human resume, supplied only from the owned API's durable
        // admission receipt. Upstream deliberately refuses model resume of paused goals.
        if (String(options.sessionId) === process.env.CAMPUS_CONVERSATION_ID && process.env.CAMPUS_GOAL_RESUME) {
          const request = JSON.parse(process.env.CAMPUS_GOAL_RESUME);
          const agent = handle.agent;
          let goal = ctx.goals.get(agent);
          if (!goal || goal.phase === 'complete' || goal.id !== request.id || goal.revision !== request.revision) throw new Error('Saved goal changed');
          if (request.maxGoalRounds !== undefined) goal = ctx.goals.edit(agent, { id: goal.id, revision: goal.revision }, { maxGoalRounds: request.maxGoalRounds });
          ctx.goals.resume(agent, { id: goal.id, revision: goal.revision });
        }
        return handle;
      };
      return Reflect.get(target, property);
    },
  });
  serveSdk(ctx.extend({ agents }), config);
}
