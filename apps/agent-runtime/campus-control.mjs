/** Local human controls over public upstream Agent/subagent APIs, no model turn. */
import { createServer } from 'node:http';

export async function mountControls(ctx, bridge) {
  if (!process.env.CAMPUS_CONVERSATION_ID) return;
  const catalogs = new Map(), pending = new Map();
  function interrupt(childId, parentId) {
    const child = ctx.agents.get(childId);
    if (!child || child.status !== 'running') {
      pending.set(childId, parentId);
      return;
    }
    if (String(child.session.header.parentSession) !== parentId) throw new Error('Child belongs to a different parent');
    if (catalogs.get(childId)?.mode === 'continuable') ctx.subagents.interruptByParent(childId, parentId, 'continuable');
    else child.cancel({ kind: 'user' });
    pending.delete(childId);
  }
  ctx.on('session/event', (session, event) => {
    if (event.type === 'subagent/catalog') catalogs.set(String(event.data.childId), { ...event.data, parentId: String(session.id) });
  });
  ctx.on('agent/status', ({ agent, status }) => {
    if (status === 'running' && pending.has(String(agent.id))) {
      try { interrupt(String(agent.id), pending.get(String(agent.id))); }
      catch { pending.delete(String(agent.id)); }
    }
  });
  const server = createServer(async (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    if (req.method !== 'POST' || req.url !== '/interrupt' || req.headers.authorization !== 'Bearer ' + process.env.CAMPUS_BRIDGE_TOKEN) {
      res.statusCode = 403; return res.end('{"error":"CONTROL_DENIED"}');
    }
    try {
      let text = '';
      for await (const chunk of req) { text += chunk; if (text.length > 4096) throw new Error('Invalid request'); }
      const data = JSON.parse(text);
      if (typeof data.child_id !== 'string' || typeof data.parent_id !== 'string') throw new Error('Invalid request');
      const catalog = catalogs.get(data.child_id);
      if (catalog && catalog.parentId !== data.parent_id) throw new Error('Invalid parent');
      interrupt(data.child_id, data.parent_id);
      res.end(JSON.stringify({ accepted: true, child_id: data.child_id }));
    } catch {
      res.statusCode = 409; res.end('{"error":"CHILD_CONTROL_FAILED"}');
    }
  });
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  ctx.effect(() => () => new Promise(done => { server.close(done); server.closeAllConnections(); }));
  await bridge('/api/v1/agent/internal/control-address', {
    conversation_id: process.env.CAMPUS_CONVERSATION_ID, url: `http://127.0.0.1:${server.address().port}`,
  });
}
