/** Policy tests for the project-owned fetch resolver (no network access). */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_TRUSTED_HOSTS,
  createCampusResolver,
  isBlockedCampusAddress,
  isTrustedCampusHost,
} from '../campus-web-fetch.mjs';

const TRUSTED = new Set(DEFAULT_TRUSTED_HOSTS);

test('the allowlist matches exact hostnames only', () => {
  assert.equal(isTrustedCampusHost('www.zju.edu.cn'), true);
  assert.equal(isTrustedCampusHost('RD.ZJU.EDU.CN'), true);
  assert.equal(isTrustedCampusHost('www.zju.edu.cn.'), true);
  // Lookalikes and suffix tricks must stay outside the allowlist.
  assert.equal(isTrustedCampusHost('www.zju.edu.cn.evil.example'), false);
  assert.equal(isTrustedCampusHost('evil-zju.edu.cn'), false);
  assert.equal(isTrustedCampusHost('notzju.edu.cn'), false);
  assert.equal(isTrustedCampusHost('unknown.zju.edu.cn'), false);
  assert.equal(isTrustedCampusHost('stusom.zju.edu.cn'), false);
  assert.equal(isTrustedCampusHost(''), false);
});

test('dangerous addresses are refused even for a trusted host', () => {
  for (const address of ['127.0.0.1', '0.0.0.0', '169.254.169.254', '224.0.0.1',
    '::1', '::', 'fe80::1', 'ff02::1', '::ffff:127.0.0.1']) {
    assert.equal(isBlockedCampusAddress(address), true, address + ' must be blocked');
  }
  for (const address of ['10.203.4.70', '10.10.0.12', '192.168.1.2', '172.16.0.5',
    '203.0.113.5', '39.173.157.74', '2001:db8::1']) {
    assert.equal(isBlockedCampusAddress(address), false, address + ' must be allowed');
  }
});

test('a trusted campus host may resolve to campus-private addresses', async () => {
  const resolver = createCampusResolver({
    trustedHosts: [...TRUSTED],
    strictResolve: async () => { throw new Error('strict resolver must not run for trusted hosts'); },
    lookupImpl: async () => [{ address: '10.203.4.70', family: 4 }, { address: '10.10.0.12', family: 4 }],
  });
  assert.deepEqual(await resolver('www.zju.edu.cn'), [
    { address: '10.203.4.70', family: 4 }, { address: '10.10.0.12', family: 4 },
  ]);
});

test('a trusted host resolving to loopback/link-local is still refused', async () => {
  for (const address of ['127.0.0.1', '169.254.169.254', '::1']) {
    const resolver = createCampusResolver({
      trustedHosts: [...TRUSTED], strictResolve: async () => [],
      lookupImpl: async () => [{ address, family: address.includes(':') ? 6 : 4 }],
    });
    await assert.rejects(resolver('www.zju.edu.cn'), (error) => error?.code === 'WEB_BLOCKED_URL');
  }
});

test('untrusted hostnames keep the upstream strict resolver', async () => {
  const seen = [];
  const resolver = createCampusResolver({
    trustedHosts: [...TRUSTED],
    strictResolve: async (hostname, signal) => { seen.push([hostname, signal]); return [{ address: '203.0.113.5', family: 4 }]; },
    lookupImpl: async () => { throw new Error('campus lookup must not run for untrusted hosts'); },
  });
  const signal = AbortSignal.timeout(1000);
  assert.deepEqual(await resolver('example.org', signal), [{ address: '203.0.113.5', family: 4 }]);
  assert.deepEqual(seen, [['example.org', signal]]);
});

test('an empty answer set for a trusted host is a provider error', async () => {
  const resolver = createCampusResolver({
    trustedHosts: [...TRUSTED], strictResolve: async () => [], lookupImpl: async () => [],
  });
  await assert.rejects(resolver('www.zju.edu.cn'), (error) => error?.code === 'WEB_PROVIDER_ERROR');
});
