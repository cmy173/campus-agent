"""LIVE anonymous backend transport check. Fixed URLs, no school or model keys.

Run with the API virtualenv. Never writes app settings, databases, proxy or DNS.
Uses the same requests library as backend providers; each hop has a fresh session.
"""
import concurrent.futures
import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urljoin, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
URLS = ('https://example.org/', 'https://www.zju.edu.cn/',
        'https://zdbk.zju.edu.cn/jwglxt/xtgl/login_slogin.html', 'https://webvpn.zju.edu.cn/')


def public_url(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))


def probe(url, follow_system):
    start = time.monotonic()
    item = {'url': url, 'policy': 'system' if follow_system else 'direct', 'hops': []}
    current = url
    try:
        item['dns_families'] = sorted(set(row[0] for row in socket.getaddrinfo(urlsplit(url).hostname, 443)))
        for _ in range(4):
            # No ambient cookies/authorization, no reuse of response cookies.
            with requests.Session() as session:
                session.trust_env = follow_system
                with session.get(current, timeout=(5, 7), allow_redirects=False, stream=True,
                                 headers={'User-Agent': 'CampusAgent-ReadonlyAudit/1.0'}) as response:
                    hop = {'url': public_url(current), 'status': response.status_code,
                           'content_type': response.headers.get('Content-Type', '').split(';')[0]}
                    item['hops'].append(hop)
                    if 300 <= response.status_code < 400 and response.headers.get('Location'):
                        target = urljoin(current, response.headers['Location'])
                        host = urlsplit(target).hostname or ''
                        if urlsplit(target).scheme != 'https' or not (host == 'zju.edu.cn' or host.endswith('.zju.edu.cn')):
                            item['redirect_stopped'] = True
                            break
                        current = target
                        continue
                    body = next(response.iter_content(32000), b'')
                    hop['sample_bytes'] = len(body)
                    item['ok'] = response.status_code == 200 and bool(body)
                    break
        else:
            item['redirect_limit'] = True
    except (requests.RequestException, OSError) as exc:
        # Exception text can include proxy userinfo; emit only class, never repr.
        item.update(ok=False, error=type(exc).__name__)
    item['elapsed_ms'] = round((time.monotonic() - start) * 1000)
    return item


if __name__ == '__main__':
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(probe, url, policy) for url in URLS for policy in (True, False)]
        items = [future.result() for future in futures]
    report = {'observed_at': datetime.now(timezone.utc).isoformat(), 'mode': 'live-anonymous-backend',
              'credentials': False, 'tls_verified': True,
              'proxy_environment_present': any(os.environ.get(k) for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                                                          'http_proxy', 'https_proxy', 'all_proxy')),
              'items': items}
    (ROOT / 'docs/audit/transition-network-python.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
