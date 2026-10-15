/** Thin credential boundary; discovery, execution and reconnect belong to upstream. */
import { apply as applyMcp, Config as UpstreamConfig, inject } from '@deepseek-ai/dsh-mcp-client';
import { mcpToolPolicy, registerMcpCapabilityPolicy } from './capability-policy.mjs';
export { inject };
export const name = 'campus-mcp';

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const stringMap = value => object(value) && Object.values(value).every(item => typeof item === 'string');

/** Parse only the selected server's private credential map; diagnostics never echo it. */
export function credentialConfig(config, serialized = process.env.CAMPUS_MCP_CREDENTIALS || '{}') {
  try {
    const all = JSON.parse(serialized);
    if (!object(all)) throw new Error();
    const credentials = Object.hasOwn(all, config.serverName) ? all[config.serverName] : {};
    if (!object(credentials)) throw new Error();
    const { env: _env, headers: _headers, ...plain } = config;
    const kind = config.transport === 'stdio' ? 'env' : 'headers';
    const values = credentials[kind] ?? {};
    if (!stringMap(values)) throw new Error();
    return { ...plain, [kind]: values };
  } catch {
    throw new Error('Invalid Campus MCP credential configuration');
  }
}

// Upstream subprocess scrubs KEY/PASSWORD/SECRET/TOKEN but not CREDENTIALS.
// Remove the all-server vault from ambient inheritance before starting any MCP;
// subsequent server instances resolve from this private module-owned snapshot.
let privateCredentialVault;
export function claimCredentialConfig(config) {
  const serialized = process.env.CAMPUS_MCP_CREDENTIALS ?? privateCredentialVault ?? '{}';
  const selected = credentialConfig(config, serialized);
  privateCredentialVault = serialized;
  delete process.env.CAMPUS_MCP_CREDENTIALS;
  return selected;
}

/**
 * Accept project permission attributes while only the transport enters upstream config.
 *
 * This must NOT be exported as `Config`: the harness treats a module-level `Config`
 * export as the plugin's configuration schema, and a plain function there makes the
 * plugin fail to activate — silently, with no tools and no error (which is exactly
 * what removed every `mcp__*` tool from the model). Keep the name distinct.
 */
export function transportConfig(config) {
  const policy = mcpToolPolicy(config);
  const { allowedTools: _allowedTools, ...transport } = config;
  return { ...UpstreamConfig(transport), allowedTools: [...policy.allowed].map(name => name.slice(policy.prefix.length)) };
}

export async function apply(ctx, config) {
  const privateConfig = claimCredentialConfig(config);
  let resolved;
  try { resolved = transportConfig(privateConfig); }
  catch { throw new Error('Invalid Campus MCP server configuration'); }
  registerMcpCapabilityPolicy(ctx, resolved);
  const { allowedTools: _allowedTools, ...transport } = resolved;
  return applyMcp(ctx, transport);
}
