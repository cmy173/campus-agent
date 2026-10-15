import { createConnection } from '@playwright/mcp';
import { Client, InMemoryTransport } from '@modelcontextprotocol/client';
import { writeFile } from 'node:fs/promises';
const server = await createConnection({ browser: { browserName: 'chromium', isolated: false }, webmcp: true }, () => { throw new Error('Audit must not launch a browser'); });
const [local, remote] = InMemoryTransport.createLinkedPair();
await server.connect(remote);
const client = new Client({ name: 'audit-schema-only', version: '1.0.0' }, { capabilities: {} });
await client.connect(local);
try {
 const result = await client.listTools();
 await writeFile(new URL('../../docs/audit/browser-static-schemas.json', import.meta.url), JSON.stringify({ kind: 'local_inmemory_mcp_discovery_no_browser_no_model', tools: result.tools }, null, 2));
 console.log(JSON.stringify({ count: result.tools.length, names: result.tools.map(t => t.name) }));
} finally { await client.close(); await server.close(); }
