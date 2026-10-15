import test from 'node:test';
import assert from 'node:assert/strict';
import { assertModelBrowserCall, isDangerousURL } from '../browser-action-policy.mjs';

const notice = 'https://zdbk.zju.edu.cn/jwglxt/xtgl/xwck_ckLoginNews.html?xwbh=0123456789abcdef0123456789ABCDEF&doType=save';
test('registered read-only ZDBK notice route is not a submission just because its legacy parameter says save', () => {
  assert.equal(isDangerousURL(notice), false);
  assert.doesNotThrow(() => assertModelBrowserCall('browser_navigate', { url: notice }));
});
test('read-only notice exception never grants arbitrary save routes or origins', () => {
  for (const url of [notice.replace('zdbk.zju.edu.cn', 'evil.example'), notice.replace('https:', 'http:'),
    notice.replace('xwck_ckLoginNews', 'submit'), notice + '&action=delete', notice + '&doType=delete',
    notice.replace('0123456789abcdef0123456789ABCDEF', 'bad-id'), notice.replace('https://', 'https://user:pass@'),
    notice.replace('zdbk.zju.edu.cn/', 'zdbk.zju.edu.cn:444/'), 'https://example.org/save']) {
    assert.equal(isDangerousURL(url), true, url);
  }
});
