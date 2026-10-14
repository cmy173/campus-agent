"""Read-only trial against the real school site in the already logged-in browser.

Condition 5 needs a *logged-in* browser to read pages and download files. The login lives
in the app's persistent profile (`var/agent/browser/profile`), which can only be used by
the app instance that already has it open — so this driver talks to a **running** app
instance instead of starting its own, and never submits anything.

    python tools/verify_real_school_browser.py --base-url http://127.0.0.1:18100

The turn is driven by the real official provider (paid). Every prompt is read-only; any
browser interaction that would submit a form is forbidden by the prompt and would surface
as a pending confirmation instead of an executed action.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_ID = 'prv_c68d2878'
TARGET = 'https://zdbk.zju.edu.cn/'

PROMPT = (
    '只做一件事，最多 6 次工具调用，然后立刻用中文回答，不要继续探索。'
    '禁止使用 browser_evaluate，只用 browser_navigate / browser_snapshot / browser_click 这类工具。'
    '任务：用 campus_webvpn_entry 取「本科教务 zdbk」的 WebVPN 入口，用浏览器打开，进入「通知公告」，'
    '找到一条带附件的通知，把附件下载下来。'
    '然后用一句话回答：1) 页面标题；2) 是否已登录（依据是页面上出现的本人身份信息）；'
    '3) 下载到的文件名（如果确实没有可下载的附件，就明确说没有）。'
    '不要反复重试；任何一步失败就如实说明并结束。'
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True, help='base URL of a running app instance that owns the browser')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    directory = ROOT / 'var' / ('real-school-browser-' + time.strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True, exist_ok=True)
    client = requests.Session()
    evidence = {'kind': 'real_school_browser_read_only_trial', 'base_url': base, 'target': TARGET, 'prompt': PROMPT}

    health = client.get(base + '/api/v1/health', timeout=10)
    assert health.ok, health.text
    created = client.post(base + '/api/v1/agent/conversations',
                          json={'provider_id': PROVIDER_ID, 'send_to_provider': True}, timeout=30)
    assert created.ok, created.text
    cid = created.json()['id']
    evidence['conversation_id'] = cid

    posted = client.post(base + f'/api/v1/agent/conversations/{cid}/turns', json={'text': PROMPT},
                         headers={'Idempotency-Key': 'real-school-browser-1'}, timeout=30)
    assert posted.ok, posted.text
    turn_id = posted.json()['id']
    cursor, events = 0, []
    deadline = time.monotonic() + args.timeout
    settled = False
    while time.monotonic() < deadline:
        view = client.get(base + f'/api/v1/agent/turns/{turn_id}', params={'after': cursor}, timeout=30).json()
        events.extend(view['events']['items'])
        cursor = view['events']['next_cursor']
        if view['status'] not in ('queued', 'running', 'stopping'):
            settled = True
            break
        time.sleep(1)
    if not settled:
        # Never leave a turn running on the operator's instance; the owner cookie this
        # session already holds is enough to cancel it.
        try:
            client.post(base + f'/api/v1/agent/turns/{turn_id}/cancel', timeout=20)
        except requests.RequestException:
            pass
        view = client.get(base + f'/api/v1/agent/turns/{turn_id}', params={'after': cursor}, timeout=30).json()
    evidence['timed_out'] = not settled
    evidence['turn'] = {'id': turn_id, 'status': view['status'], 'response': view.get('response', ''),
                        'error': view.get('error'), 'events': len(events)}
    calls, results, downloads = [], [], []
    for event in events:
        if event['kind'] != 'harness_event':
            continue
        payload = event['data']['event']
        if payload['type'] == 'tool/call':
            calls.append({'name': payload['data']['name'], 'arguments': str(payload['data'].get('arguments'))[:300]})
        elif payload['type'] == 'tool/result':
            text = ' '.join(block.get('text', '') for block in payload['data']['message'].get('content', [])
                            if isinstance(block, dict))
            results.append(text[:1500])
            downloads.extend(re.findall(r'Downloaded file (\S+) to "([^"]+)"', text))
    actions = client.get(base + f'/api/v1/agent/conversations/{cid}/actions', timeout=20).json().get('items', [])
    response = evidence['turn']['response']
    evidence['checks'] = {
        'turn_succeeded': view['status'] == 'succeeded',
        'browser_tool_calls': [c['name'] for c in calls if 'browser' in c['name']],
        'downloads': downloads,
        'pending_actions': len(actions),
        'mentions_logged_in': bool(re.search(r'已登录|登录状态|学号|姓名', response)),
        'answer_chars': len(response),
    }
    evidence['ok'] = bool(evidence['checks']['turn_succeeded'] and evidence['checks']['browser_tool_calls']
                          and evidence['checks']['answer_chars'] > 50)
    (directory / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    (directory / 'transcript.json').write_text(json.dumps({'calls': calls, 'results': results}, ensure_ascii=False,
                                                          indent=2), encoding='utf-8')
    print(json.dumps({'ok': evidence['ok'], 'evidence': str(directory), 'checks': evidence['checks'],
                      'response_head': response[:600]}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
