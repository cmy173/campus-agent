/**
 * Project-owned `web_fetch` transport: the upstream public-address policy plus an
 * explicit allowlist of university public-content hostnames.
 *
 * Why this exists: the school's public sites resolve to campus-private addresses
 * (10.x) through the campus network, so the upstream "public unicast only" guard
 * rejects them with `WEB_BLOCKED_URL`. The Agent then had to fall back to its
 * browser, which is slow and costs tool calls. The allowlist below is the whole
 * safety boundary: it is exact-host, configurable, and every other hostname keeps
 * the upstream strict resolver (loopback/link-local/metadata stays unreachable).
 *
 * Safety properties:
 * - Only exact, lower-cased hostnames are trusted (no suffix/wildcard matching,
 *   so `www.zju.edu.cn.evil.example` and `evil-zju.edu.cn` stay strict).
 * - Trusted hosts may resolve to campus ranges, but never to loopback, link-local,
 *   unspecified, multicast or IPv4-mapped loopback/link-local addresses.
 * - The upstream transport still pins the resolved addresses and refuses
 *   cross-origin redirects, so the allowlist cannot be turned into an open proxy.
 */
import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';
import { HttpFetchProvider } from '@deepseek-ai/dsh-web-fetch-http';
import { WebError } from '@deepseek-ai/dsh-web';

export const name = 'campus-web-fetch';
export const inject = ['web'];

/** Exact public-content hostnames of the university. Extend only after review. */
export const DEFAULT_TRUSTED_HOSTS = Object.freeze([
  'www.zju.edu.cn',
  'rd.zju.edu.cn',
  'bksy.zju.edu.cn',
  'zdbk.zju.edu.cn',
  'www.intl.zju.edu.cn',
  'www.chem.zju.edu.cn',
  'www.psych.zju.edu.cn',
  'www.math.zju.edu.cn',
  'lsi.zju.edu.cn',
]);

const DEFAULT_LIMITS = Object.freeze({
  maxResponseBytes: 5_000_000,
  maxBodyChars: 100_000,
  timeoutMs: 30_000,
  maxRedirects: 5,
  userAgent: 'deepseek-harness/0.0.1 (campus-agent)',
});

function normalizeHost(hostname) {
  return String(hostname ?? '').toLowerCase().replace(/\.$/, '');
}

/** True only for a configured exact hostname; never a suffix match. */
export function isTrustedCampusHost(hostname, trustedHosts = DEFAULT_TRUSTED_HOSTS) {
  return trustedHosts.includes(normalizeHost(hostname));
}

/** Reject addresses that must never be reachable even for a trusted hostname. */
export function isBlockedCampusAddress(input) {
  const address = String(input ?? '').replace(/^\[/, '').replace(/\]$/, '');
  const family = isIP(address);
  if (family === 4) {
    const [a, b] = address.split('.').map(Number);
    return a === 0 || a === 127 || (a === 169 && b === 254) || a >= 224;
  }
  if (family === 6) {
    const lower = address.toLowerCase();
    if (lower === '::' || lower === '::1') return true;
    if (lower.startsWith('fe80:') || lower.startsWith('ff')) return true;
    const mapped = lower.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
    if (mapped) return isBlockedCampusAddress(mapped[1]);
    return false;
  }
  return true; // not a valid IP: refuse rather than guess
}

/**
 * Build a resolver for the fetch provider.
 * Trusted exact hosts use the raw campus answer set (private ranges allowed, but
 * dangerous destinations refused); every other hostname goes through `strictResolve`,
 * which is the upstream public-unicast-only resolver.
 */
export function createCampusResolver({ trustedHosts = DEFAULT_TRUSTED_HOSTS, strictResolve, lookupImpl = lookup } = {}) {
  if (typeof strictResolve !== 'function') throw new Error('campus-web-fetch: strictResolve is required');
  const hosts = trustedHosts.map(normalizeHost);
  return async function resolveAddresses(hostname, signal) {
    if (!isTrustedCampusHost(hostname, hosts)) return strictResolve(hostname, signal);
    const normalized = normalizeHost(hostname);
    const resolved = await lookupImpl(normalized, { all: true, order: 'verbatim' });
    if (!Array.isArray(resolved) || resolved.length === 0) {
      throw new WebError(`hostname "${hostname}" resolved to no addresses`, 'WEB_PROVIDER_ERROR');
    }
    const addresses = [];
    for (const entry of resolved) {
      const family = entry.family ?? isIP(entry.address);
      if ((family !== 4 && family !== 6) || isIP(entry.address) !== family) {
        throw new WebError(`hostname "${hostname}" resolved to an invalid IP address`, 'WEB_PROVIDER_ERROR');
      }
      if (isBlockedCampusAddress(entry.address)) {
        throw new WebError(`URL hostname "${hostname}" resolves to a blocked address`, 'WEB_BLOCKED_URL');
      }
      addresses.push({ address: entry.address, family });
    }
    return addresses;
  };
}

/** Create the fetch provider used by the profile (also used by the audit script). */
export function createCampusFetchProvider(limits = {}, trustedHosts = DEFAULT_TRUSTED_HOSTS) {
  const resolvedLimits = { ...DEFAULT_LIMITS, ...limits };
  const strictProvider = new HttpFetchProvider(resolvedLimits);
  const resolver = createCampusResolver({ trustedHosts, strictResolve: strictProvider.resolveAddresses });
  return new HttpFetchProvider(resolvedLimits, resolver);
}

export function apply(ctx, config = {}) {
  const limits = config.limits ?? {};
  const trustedHosts = Array.isArray(config.trustedHosts) && config.trustedHosts.length
    ? config.trustedHosts : DEFAULT_TRUSTED_HOSTS;
  ctx.web.registerFetchProvider(createCampusFetchProvider(limits, trustedHosts));
}
