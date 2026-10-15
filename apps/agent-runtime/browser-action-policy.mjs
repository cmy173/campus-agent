/** Owned browser entry policy. Server annotations and model descriptions are not grants. */
import { createHash } from 'node:crypto';
export const effectFingerprint = request => createHash('sha256').update(requestSignature(request)).digest('hex');
export const BLOCKED_BROWSER_TOOLS = new Set([
  'browser_evaluate', 'browser_run_code_unsafe', 'browser_file_upload', 'browser_drop', 'browser_drag',
]);
const MODEL_TOOLS = new Set([
  'browser_close', 'browser_resize', 'browser_console_messages', 'browser_handle_dialog',
  'browser_emulate_media', 'browser_find', 'browser_fill_form', 'browser_press_key', 'browser_type',
  'browser_navigate', 'browser_navigate_back', 'browser_network_requests', 'browser_network_request',
  'browser_take_screenshot', 'browser_snapshot', 'browser_click', 'browser_hover', 'browser_select_option',
  'browser_tabs', 'browser_wait_for', 'browser_prepare_action',
]);
export const isModelBrowserTool = name => MODEL_TOOLS.has(name) && !BLOCKED_BROWSER_TOOLS.has(name);
const denial = () => Object.assign(new Error(
  '该浏览器操作可能绕过提交确认或直接传出文件；请准备具体目标、请求方法和内容供用户确认，或由用户在浏览器中完成。',
), { code: 'BROWSER_APPROVAL_REQUIRED' });

// Apply to both direct navigation and background requests. Decode repeatedly so
// percent-encoded dangerous routes cannot become an unchecked raw navigation.
export function isDangerousURL(value) {
  let url;
  try { url = new URL(value); } catch { return true; }
  // Exact read-only source contract already used by the fixed ZDBK notices
  // adapter (connectors/campus/connectors/notices.py). `doType=save` is the
  // school's legacy *display* query here, not permission for generic save URLs.
  const params = url.searchParams;
  if (url.protocol === 'https:' && url.hostname === 'zdbk.zju.edu.cn' && !url.port
      && !url.username && !url.password && !url.hash
      && url.pathname === '/jwglxt/xtgl/xwck_ckLoginNews.html'
      && [...params.keys()].length === 2 && params.getAll('xwbh').length === 1
      && /^[0-9a-f]{32}$/i.test(params.get('xwbh') ?? '')
      && params.getAll('doType').length === 1 && params.get('doType') === 'save') return false;
  let text = url.pathname + url.search;
  for (let i = 0; i < 3; i++) { try { const next = decodeURIComponent(text); if (next === text) break; text = next; } catch { break; } }
  return /(?:delete|remove|destroy|erase|logout|log[-_]?out|sign[-_]?out|pay(?:ment)?|purchase|checkout|unsubscribe|cancel|submit|send|register|transfer|withdraw|revoke|reset|update|save|setcookie|action=(?!search|query|list|view)|删除|支付|注销|退出|提交|发送)/i.test(text);
}

export function assertModelBrowserCall(name, args = {}) {
  if (!MODEL_TOOLS.has(name) || BLOCKED_BROWSER_TOOLS.has(name)) throw denial();
  // Playwright output writes are outside the native filesystem sandbox.
  if (args.filename !== undefined && (typeof args.filename !== 'string' ||
      !args.filename || args.filename.length > 128 || /[\\/:<>"|?*\x00-\x1f]/.test(args.filename) ||
      args.filename === '.' || args.filename === '..')) throw denial();
  if (name === 'browser_network_request' && args.part !== 'response-body') throw denial();
  if (name === 'browser_handle_dialog' && args.accept !== false) throw denial();
  if (name === 'browser_navigate' || (name === 'browser_tabs' && args.action === 'new' && args.url)) {
    try { const url = new URL(args.url);
      if (!['http:', 'https:', 'about:'].includes(url.protocol) ||
          (url.protocol === 'about:' && args.url !== 'about:blank') || isDangerousURL(args.url)) throw denial();
    } catch { throw denial(); }
  }
  if (name === 'browser_press_key' && !['Enter', 'Space', ' ', 'Tab', 'Shift+Tab', 'Escape',
    'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown',
    'Backspace', 'Delete'].includes(args.key)) throw denial();
}

const EXTERNAL_ACTION = /提交|发送|报名|购买|支付|删除|取消订单|退出|注销|确认|submit|send|register|purchase|pay|delete|remove|logout|confirm/i;
const NAV_LABEL = /^(上一页|下一页|首页|末页|返回|上一步|下一步|next( page)?|previous( page)?|prev|back|first|last|[0-9]{1,4})$/i;
export function isReadOnlyInteraction(target, key) {
  if (!target || target.sensitive || EXTERNAL_ACTION.test(target.text || '') || target.method && target.method.toLowerCase() !== 'get') return false;
  if (key === 'Space' || key === ' ') return target.editable === true;
  if (target.tag === 'a' && target.href && !target.scripted) {
    try { return ['http:', 'https:'].includes(new URL(target.href).protocol) && !isDangerousURL(target.href); } catch { return false; }
  }
  return (target.tag === 'button' || target.role === 'button') && NAV_LABEL.test((target.text || '').trim())
    && !target.scripted && !target.is_submit;
}

// One classifier is serialized into all owned DOM inspections and screenshot masks.
// Include text/number OTP inputs and token fields, not only password input types.
export const SENSITIVE_SELECTOR = 'input[type="password"], [autocomplete*="password" i], [autocomplete="one-time-code" i], input[name*="otp" i], input[id*="otp" i], input[name*="token" i], input[id*="token" i], input[name*="secret" i], input[id*="secret" i], input[name*="password" i], input[id*="password" i], input[name*="passwd" i], input[name*="verification" i], input[name*="verifycode" i], input[name*="captcha" i], input[name*="验证码"], input[name*="authcode" i], textarea[name*="token" i]';
export function isSensitiveElement(element) {
  if (!element) return false;
  const get = key => element.getAttribute?.(key) || element[key] || '';
  return String(get('type')).toLowerCase() === 'password' ||
    /password|one-time-code/i.test(get('autocomplete')) ||
    /otp|token|secret|password|passwd|verification|verifycode|captcha|authcode|验证码/i.test(get('name') + ' ' + get('id'));
}
export function isAuthenticationURL(value) {
  try { const url = new URL(value); return /(?:^|\.)(?:zjuam\.zju\.edu\.cn)$/.test(url.hostname) || /\/(?:login|signin|sso|oauth|mfa|auth)(?:[/.?]|$)/i.test(url.pathname); } catch { return false; }
}

// Registry is owned code, never website/MCP annotations. No broad '*.zju.edu.cn'
// POST grant: reviewed campus adapters execute their reads outside the browser.
export const CAMPUS_READONLY_REQUESTS = Object.freeze([
  Object.freeze({ method: 'GET', origin: 'https://zdbk.zju.edu.cn', pathname: '/jwglxt/xtgl/index_cxYhxxIndex.html' }),
]);
export function isRegisteredReadOnlyRequest(request) {
  let url; try { url = new URL(request.url); } catch { return false; }
  return !isDangerousURL(url.href) && CAMPUS_READONLY_REQUESTS.some(rule =>
    rule.method === request.method.toUpperCase() && rule.origin === url.origin && rule.pathname === url.pathname && !url.search);
}
const bodyEntries = body => [...new URLSearchParams(body || '')].sort(([ak, av], [bk, bv]) => ak.localeCompare(bk) || av.localeCompare(bv));
export function requestSignature(request) {
  const method = request.method.toUpperCase(), url = new URL(request.url);
  url.hash = '';
  if (request.encoding === 'form' && method === 'GET') {
    return JSON.stringify({ method, url: url.origin + url.pathname, entries: bodyEntries(url.search) });
  }
  return JSON.stringify({ method, url: url.href, ...(request.encoding === 'form' ? { entries: bodyEntries(request.body) } : { body: request.body || '' }) });
}

/** Context-wide fail-closed gate; stays armed after model IO for delayed autosave.
 * Only trusted UI 'open' restores manual mode. Confirm grants exactly ONE request,
 * never a whole page/action interval; redirects and secondary writes need a grant.
 */
export function createNetworkGate() {
  let guarded = false, depth = 0, permit = null;
  const blocked = [];
  return {
    begin() { guarded = true; depth++; blocked.length = 0; },
    end() { depth = Math.max(0, depth - 1); permit = null; },
    human() { if (depth) throw denial(); guarded = false; permit = null; blocked.length = 0; },
    get guarded() { return guarded; },
    get blocked() { return blocked; },
    grant(effect) { permit = effect ? { ...effect, used: false } : null; },
    decide(request) {
      if (!guarded) return true; // Trusted UI mode: human official SSO/MFA is unmodified.
      const method = request.method.toUpperCase();
      if (permit && !permit.used) {
        const signature = requestSignature({ ...request, encoding: permit.encoding });
        if (createHash('sha256').update(signature).digest('hex') === permit.fingerprint) { permit.used = true; return true; }
      }
      if (isRegisteredReadOnlyRequest(request)) return true;
      const safeGet = method === 'GET' && !isDangerousURL(request.url);
      // Ordinary document/resource reads are automatic. Arbitrary background GET
      // APIs/exfiltration are not inferred read-only from their URL or annotations.
      if (safeGet && (request.navigation || ['document', 'stylesheet', 'script', 'image', 'font', 'media', 'manifest'].includes(request.resourceType))) return true;
      if (safeGet && request.explicitSearch) return true;
      blocked.push({ method, url: request.url, resourceType: request.resourceType });
      return false;
    },
  };
}
