/** Project-owned model capability fence. Discovery/loading is never authorization. */
import { readdir, lstat } from 'node:fs/promises';
import { isAbsolute, join, relative, resolve, sep } from 'node:path';
import { scopeChainOf, scopeOf } from '@deepseek-ai/dsh-scope';
import { isModelBrowserTool } from './browser-action-policy.mjs';
import SandboxedFileSystem from '@deepseek-ai/dsh-fs-sandbox';
import { FsError } from '@deepseek-ai/dsh-fs';
import z from '@deepseek-ai/schemastery';

/** Primitive read fence also covers skill catalogs/instruction loaders, which
 * read via ctx.fs outside tools/pre-execute. Never replace the upstream write
 * sandbox; retain it and add read authority over canonical paths only.
 * As with upstream fs containment, concurrent trusted-host symlink swaps are
 * outside this model-path threat boundary (not kernel isolation).
 */
export default class CampusFileSystem extends SandboxedFileSystem {
  static Config = z.object({
    cwd: z.string().default(process.cwd()), diffBasisMaxBytes: z.number().default(10485760),
    workspace: z.string().required(), skillDirs: z.array(String).default([]),
    attachmentPaths: z.array(String).default([]), privateRoots: z.array(String).default([]),
  });
  constructor(ctx, config) { super(ctx, config); this.capabilityConfig = config; }
  async checkedReadTarget(target) {
    const config = this.capabilityConfig;
    const fresh = await super.resolve(String(target.targetKey));
    const path = String(fresh.targetKey);
    const roots = await Promise.all([config.workspace, ...config.skillDirs].map(root => super.resolve(root)));
    const files = await Promise.all(config.attachmentPaths.map(file => super.resolve(file)));
    const privateRoots = await Promise.all(config.privateRoots.map(root => super.resolve(root)));
    const attached = files.some(file => path === String(file.targetKey));
    if (isCredentialPath(target.displayPath) || isCredentialPath(path)
        || !attached && privateRoots.some(root => isPathWithin(path, String(root.targetKey)))
        || !roots.some(root => isPathWithin(path, String(root.targetKey))) && !attached) {
      throw new FsError('项目能力策略拒绝读取此路径。', 'CAMPUS_CAPABILITY_DENIED');
    }
    return fresh;
  }
  async readText(target, signal) { return super.readText(await this.checkedReadTarget(target), signal); }
  async streamText(target, signal) { return super.streamText(await this.checkedReadTarget(target), signal); }
  async readBytes(target, signal, maxBytes) { return super.readBytes(await this.checkedReadTarget(target), signal, maxBytes); }
}

const policies = new WeakMap();
const deniedCode = new Set(['bash', 'shell', 'pwsh', 'powershell', 'workflow', 'run_code', 'ralph']);
const resourceTools = new Set(['list_mcp_resources', 'list_mcp_resource_templates', 'read_mcp_resource']);
const pathTools = new Map([['read', 'file_path'], ['read_image', 'file_path'], ['write', 'file_path'],
  ['edit', 'file_path'], ['glob', 'path'], ['grep', 'path'], ['publish_file', 'path']]);
const mutations = new Set(['write', 'edit']);
const searches = new Set(['glob', 'grep']);
const deny = reason => ({ kind: 'deny', reason, info: { code: 'CAMPUS_CAPABILITY_DENIED' } });
const message = '项目能力策略拒绝此工具或路径；加载工具或请求审批不会扩大权限。';
const comparable = path => process.platform === 'win32' ? path.toLowerCase() : path;
export function isPathWithin(path, root) {
  const rel = relative(comparable(root), comparable(path));
  return rel === '' || rel !== '..' && !rel.startsWith('..' + sep) && !isAbsolute(rel);
}

/** Deny credential stores even when the user selected a broad workspace/skill directory. */
export function isCredentialPath(path) {
  const parts = path.replaceAll('\\', '/').toLowerCase().split('/');
  return parts.some(part => /^(?:\.env(?:\..*)?|\.credentials(?:\..*)?|credentials?|secrets?|\.ssh|\.aws|\.azure|\.kube|\.gnupg|\.npmrc|\.netrc|\.pypirc|login data|cookies|local state|web data|browser-profile|browser-profiles)$/.test(part)
    || /^(?:id_rsa|id_ed25519|id_ecdsa)(?:\..*)?$/.test(part)
    || /\.(?:key|pem|p12|pfx|kdbx)$/.test(part));
}

function state(ctx) {
  let value = policies.get(ctx.root);
  if (!value) { value = { servers: new Set() }; policies.set(ctx.root, value); }
  return value;
}

/** Raw tool names, not hints or wildcard patterns; lossily-normalized names fail closed. */
export function mcpToolPolicy(config) {
  const server = config?.serverName;
  if (typeof server !== 'string' || !/^[A-Za-z0-9_-]{1,32}$/.test(server)) throw new Error('Invalid Campus MCP tool policy');
  const allowed = config.allowedTools ?? [];
  const prefix = `mcp__${server}__`;
  if (!Array.isArray(allowed) || allowed.length > 256 || new Set(allowed).size !== allowed.length
      || allowed.some(raw => typeof raw !== 'string' || !/^[A-Za-z0-9_-]+$/.test(raw) || prefix.length + raw.length > 64)) {
    throw new Error('Invalid Campus MCP tool policy');
  }
  return { server, prefix, allowed: new Set(allowed.map(raw => prefix + raw)), internal: server === 'campus_browser',
    // An explicitly configured + enabled server with no project whitelist is the
    // user's own choice of service, so all of its tools are permitted. A non-empty
    // list stays a strict whitelist; the bundled browser server is always governed
    // by the reviewed interaction set instead.
    unrestricted: allowed.length === 0 };
}
function permits(policy, name) {
  return name.startsWith(policy.prefix) && (policy.internal
    ? isModelBrowserTool(name.slice(policy.prefix.length))
    : policy.unrestricted || policy.allowed.has(name));
}

/** Installed before MCP connects. Both global and scoped clients are owned by their Cordis effects. */
export function registerMcpCapabilityPolicy(ctx, config) {
  const policy = { ...mcpToolPolicy(config), owner: scopeOf(ctx) };
  const shared = state(ctx);
  const cleanup = ctx.effect(() => {
    shared.servers.add(policy);
    return () => shared.servers.delete(policy);
  });
  const reason = exec => {
    if (!exec.name.startsWith(policy.prefix)) return;
    const registered = ctx.tools.get(exec.name, policy.owner);
    if (!registered || ctx.tools.get(exec.name, exec.agent) !== registered || !permits(policy, exec.name)) return message;
  };
  // A model calling a registered but non-whitelisted name is denied BEFORE its
  // body/network/stdio invocation. readOnlyHint is deliberately never inspected.
  const middleware = ctx.on('tools/pre-execute', (exec, next) => reason(exec) ? deny(message) : next(), { prepend: true });
  const guard = ctx.tools.guard(reason);
  return () => { guard(); middleware(); cleanup(); };
}

function staticReason(ctx, exec) {
  if (deniedCode.has(exec.name) || resourceTools.has(exec.name)) return message;
  if (!exec.name.startsWith('mcp__')) return;
  const chain = scopeChainOf(exec.agent);
  for (const policy of state(ctx).servers) {
    if (policy.owner !== undefined && !chain.includes(policy.owner)) continue;
    const registered = ctx.tools.get(exec.name, policy.owner);
    if (permits(policy, exec.name) && registered && ctx.tools.get(exec.name, exec.agent) === registered) return;
  }
  return message; // Unknown server/name: fail without asking the user.
}

/**
 * Strict model path checks over the public fs.resolve seam (canonical realpath
 * of the deepest existing ancestor). The trusted profile pins roots; a child
 * changing its cwd cannot enlarge them. Attachments are exact files, not roots.
 */
export function installCapabilityPolicy(ctx, options = {}) {
  state(ctx);
  const workspace = resolve(options.workspace ?? process.cwd());
  const skillDirs = (options.skillDirs ?? []).map(path => resolve(path));
  const attachmentPaths = (options.attachmentPaths ?? []).map(path => resolve(path));
  const privateRoots = (options.privateRoots ?? []).map(path => resolve(path));
  const approved = new WeakSet();
  const canonical = async (path, exec) => String((await ctx.fs.resolve(path, { cwd: workspace, signal: exec.signal })).targetKey);
  async function pathReason(exec) {
    if (!pathTools.has(exec.name)) return;
    if (!ctx.get('fs')) return message;
    const args = exec.arguments;
    const key = pathTools.get(exec.name);
    const requested = args?.[key] ?? (searches.has(exec.name) ? '.' : undefined);
    if (typeof requested !== 'string' || !requested.trim() || /[\x00-\x1f]/.test(requested)) return message;
    // Windows device/UNC/ADS spellings and drive-relative paths are not local
    // document paths. Reject them before even probing the filesystem.
    if (/^(?:\\\\|\/\/|[A-Za-z]:[^\\/])/.test(requested) || /:/.test(requested.replace(/^[A-Za-z]:/, ''))) return message;
    const cwd = exec.agent?.session?.header?.cwd ?? workspace;
    const lexical = resolve(cwd, requested);
    if (isCredentialPath(lexical)) return message;
    const root = await canonical(workspace, exec);
    const roots = [root, ...await Promise.all(skillDirs.map(path => canonical(path, exec)))];
    const files = await Promise.all(attachmentPaths.map(path => canonical(path, exec)));
    const target = await canonical(lexical, exec);
    const privateCanonical = await Promise.all(privateRoots.map(path => canonical(path, exec)));
    const attached = files.includes(target);
    if (isCredentialPath(target) || !attached && privateCanonical.some(path => isPathWithin(target, path))) return message;
    const writable = isPathWithin(target, root);
    if (mutations.has(exec.name) ? !writable : !roots.some(path => isPathWithin(target, path)) && !files.includes(target)) return message;
    if (args?.sandbox_permissions !== undefined) return message; // Never turn a project denial into a host approval.
    if (searches.has(exec.name)) {
      // ripgrep bypasses ctx.fs and recursively reads a tree, including hidden
      // files. Preflight the entire requested tree, rejecting credential names,
      // links, special files and unreadable/oversized trees. No content is read.
      // A narrower safe subtree/file still works without a popup.
      const pending = [target];
      let visited = 0;
      while (pending.length) {
        if (exec.signal?.aborted || ++visited > 20000) return message;
        const path = pending.pop();
        if (isCredentialPath(path) || !files.includes(path) && privateCanonical.some(root => isPathWithin(path, root))) return message;
        const info = await lstat(path);
        if (info.isSymbolicLink() || (!info.isDirectory() && !info.isFile())) return message;
        if (info.isDirectory()) for (const entry of await readdir(path)) pending.push(join(path, entry));
      }
    }
  }
  const middleware = ctx.on('tools/pre-execute', async (exec, next) => {
    if (staticReason(ctx, exec)) return deny(message);
    try { if (await pathReason(exec)) return deny(message); }
    catch { return deny(message); } // Never echo filesystem/private credential diagnostics.
    approved.add(exec);
    return next();
  }, { prepend: true });
  // guards are monotonic and survive loader mask removal and an around-policy
  // accidentally returning allow. Path-bearing calls MUST traverse middleware.
  const guard = ctx.tools.guard(exec => staticReason(ctx, exec)
    ?? (pathTools.has(exec.name) && !approved.has(exec) ? message : undefined));
  return () => { guard(); middleware(); };
}
