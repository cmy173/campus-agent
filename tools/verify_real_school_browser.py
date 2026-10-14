"""Read-only trial against the real school site in the already logged-in browser.

Condition 5 needs a *logged-in* browser to read pages and download files. The login lives
in the app's persistent profile (`var/agent/browser/profile`), which can only be used by
the app instance that already has it open — so this driver talks to a **running** app
instance instead of starting its own, and never submits anything.

    python tools/verify_real_school_browser.py --base-url http://127.0.0.1:18100 --task login-read
    python tools/verify_real_school_browser.py --base-url http://127.0.0.1:18100 --task download

The turn is driven by the real official provider (paid). Every prompt is read-only; any
browser interaction that would submit a form is forbidden by the prompt and would surface
as a pending confirmation instead of an executed action.

Both tasks keep each turn inside a small tool-call budget: unbounded exploration on a real
snapshot-heavy page is what made earlier attempts hang for 10 minutes without a final
answer (see var/real-school-browser-20261014-211018). The `download` task also uses two
consecutive turns in one conversation: list the notice board first, then download one
attachment, so neither turn has to hold the whole page model at once.

Evidence survives a killed client: every poll appends `progress.jsonl` and the final
`evidence.json` is written on settle *and* on timeout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_ID = 'prv_c68d2878'
TARGET = 'https://zdbk.zju.edu.cn/'

PROMPTS = {
    # Turn 1: prove the logged-in 教务 page is readable, with identity evidence.
    'login-read': [
        '只做两步，最多 3 次工具调用，然后立刻用中文回答：'
        '1) 用 campus_webvpn_entry 取「本科教务 zdbk」的 WebVPN 地址，并用浏览器打开它；'
        '2) 用 browser_snapshot 读取当前页面。'
        '然后**立刻**回答：页面标题、当前 URL、页面上是否出现本人身份信息（学号/姓名/身份）并给出原文片段、'
        '以及页面主要栏目名称。'
        '不要下载文件、不要打开新标签、不要用 browser_evaluate、不要继续探索；信息不足就说明不足并结束。'
    ],
    # Turn 1 of two: land on 通知公告 and read the list.
    'download': [
        '第一步（后面还有一步，不要一次做完）。用最多 6 次工具调用完成：'
        '1) 用 campus_webvpn_entry 取「本科教务 zdbk」的 WebVPN 地址，用 browser_navigate 打开；'
        '2) 在页面里进入「通知公告」栏目，读取通知列表。'
        '然后**立刻**用中文回答：通知公告列表页的 URL、以及列表中最靠前的 3 条通知的标题。'
        '不要点开任何通知详情、不要下载、不要提交表单、不要用 browser_evaluate、不要反复重试；'
        '如果找不到通知公告入口，就如实说明并结束。',
    ],
    # Single turn with a wider budget: hunt for a notice that really carries an attachment.
    'download-search': [
        '在已登录的本科教务（浙大 WebVPN 下的 zdbk）里找到**一个真实可下载的文件**并下载它。'
        '最多 12 次工具调用，然后立刻用中文回答。'
        '建议路径（可自选）：'
        '1) 用 campus_webvpn_entry 取 zdbk 的 WebVPN 地址并用 browser_navigate 打开，进入「通知公告」；'
        '2) 用列表页的「类别」筛选（优先教务处/教务通知类）或搜索框，找标题像通知/申报/名单/表格/附件的条目；'
        '3) 打开 1-3 条详情，找正文中的「附件」字样或回形针/下载图标，点击附件下载链接完成下载。'
        '4) 如果通知公告里确实没有可下载附件，就到「信息查询」或全部服务列表里找名称含「下载/导出/表格」的栏目并尝试一次。'
        '读取页面链接时可以用 browser_evaluate 做只读 DOM 查询，但禁止提交表单、禁止修改任何数据、禁止反复重试。'
        '回答必须包含：下载到的文件名（工具返回的原文）、保存路径（工具返回的原文）、来源页面 URL；'
        '以及你查过但没有附件的位置。确实找不到就明确说找不到。',
    ],
    # Turn 1 of an adaptive two-turn flow. The instance caps model calls per turn (observed: 8),
    # so a hunt that both searches and downloads in one turn runs out of budget before answering.
    'download-urls': [
        '第一步：只做侦察，绝对不要下载，也不要提交任何表单。'
        '在已登录的本科教务（浙大 WebVPN 下的 zdbk）里找出**最可能带附件**的通知候选。'
        '**最多 6 次工具调用**（含 campus_webvpn_entry 和 browser_navigate）；到 6 次就必须立刻回答。'
        '建议：1) campus_webvpn_entry 取 zdbk 地址并 browser_navigate 打开；2) 打开「通知公告」列表；'
        '3) 用 browser_evaluate 只读读取列表中的链接与文字，优先教务处类、或标题含「表格/名单/申报/附件/通知」的条目。'
        '然后**立刻**用中文回答：'
        'A) 你挑出的 1-3 条候选通知的**完整 URL 原文**（逐条一行，必须是可直接打开的 href）；'
        'B) 每条候选的标题与判断理由；'
        'C) 如果你已经在列表页看到任何「附件」下载链接，把它们也按 URL 原文列出。'
        '不要打开详情以外的页面、不要用 browser_click 提交任何东西。',
    ],
    # Three small turns instead of one big one: the operator instance caps model calls per turn
    # (observed: 8), so a single hunt+download turn dies of BUDGET_EXCEEDED before it can answer.
    'download-flow': [
        '第 1/3 步，只做侦察，不要下载、不要提交任何表单。严格按顺序做，**最多 4 次工具调用**，用完必须立刻回答。'
        '1) 用 browser_navigate 打开通知公告列表页 '
        '`https://webvpn.zju.edu.cn/https/77726476706e69737468656265737421eaf34397692a6245300d8db9d6562d/jwglxt/xtgl/xwgl_cxXwListGyxtzgg.html?gnmkdm=index`'
        '（浏览器已登录，直接打开即可）；'
        '2) 在页面上的「通知标题」输入框里填「附件」，按 Enter 查询（这一步必须做）；'
        '3) 读取结果：条数、以及列表里前 3 条通知标题原文。'
        '然后**立刻**用中文回答：搜索词「附件」的结果条数与前 3 条标题原文；'
        '若结果为 0 条，就再用同一个输入框查「申报」并回答同样的内容（不要额外探索）。',
    ],
    # GET-only hunt for a real downloadable file. A form submit needs the operator's one
    # confirmation (correct, but unusable from an unattended run), so this task never submits:
    # it opens the 教务 home page and follows plain navigation links to a file.
    'download-home': [
        '在已登录的本科教务（浙大 WebVPN 下的 zdbk）里**下载一个真实存在的文件**。'
        '只做 GET 导航，**最多 6 次工具调用**，用完必须立刻回答。'
        '1) 用 browser_navigate 打开「本科教学管理信息服务平台」首页：'
        '`https://webvpn.zju.edu.cn/https/77726476706e69737468656265737421eaf34397692a6245300d8db9d6562d/jwglxt/xtgl/index_initMenu.html?jsdm=06`；'
        '2) 在页面上找名称含「下载 / 表格 / 模板 / 校历 / 资料 / 文件」的入口或链接'
        '（左侧功能列表、顶部快捷入口、首页公告区都要看）；'
        '3) 用 browser_navigate 打开最像"文件下载"的那一个；'
        '4) 若打开的是文件直链就让它下载；若是列表/页面，就打开其中第一个文件链接完成下载。'
        '然后**立刻**回答：你打开的入口名称与 URL 原文、下载到的文件名（工具返回原文）、'
        '保存路径（工具返回原文）、来源页面 URL。'
        '**禁止提交任何表单、禁止改动下拉框/勾选框、禁止用 browser_evaluate、禁止反复重试**；'
        '如果确实找不到可下载文件，明确说「没有找到可下载文件」，并列出你看过的全部候选入口名称与 URL 原文。',
    ],
    # The 教务 notice board carries no attachments (verified), so this uses a real 本科生院
    # notice that does: GET-only, exact URLs, three tool calls at most.
    'download-direct': [
        '只做一次真实下载，**最多 3 次工具调用**，用完必须立刻回答。全部是 GET 导航，不提交任何表单。'
        '1) 用 browser_navigate 打开这个真实校方附件的直链：'
        '`https://bksy.zju.edu.cn/_upload/article/files/84/af/64f4eee64e839fe1ad5ec0965279/1b6e11bc-3992-4111-b2e5-f42be5ad5889.docx`'
        '2) 如果浏览器触发了下载，就读取工具返回的文件名与保存路径；'
        '如果这一步没有触发下载，就再用 browser_navigate 打开来源通知页 '
        '`https://bksy.zju.edu.cn/2026/1013/c28418a3214295/page.htm`，'
        '然后用 browser_click 点击正文里的第一个附件链接（只点附件，不要点其他链接、不要提交表单）。'
        '然后**立刻**用中文回答：'
        'A) 下载到的文件名（工具返回的原文，照抄）；B) 保存路径（工具返回的原文，照抄）；'
        'C) 来源通知标题与来源页面 URL 原文。'
        '如果确实没有触发下载，明确说「未触发下载」，并给出你实际看到的页面标题与 URL。'
    ],
}

# Turn 2 of the `download` task is built from turn 1's answer at runtime.
DOWNLOAD_TURN2 = (
    '第二步。用最多 6 次工具调用：打开通知「{title}」（或列表中最靠前的那条带附件的通知），'
    '点击它的附件下载链接，把附件保存到本地。'
    '然后**立刻**用中文回答：通知标题、下载到的文件名、保存路径（下载工具返回的路径原文）、当前页面 URL。'
    '禁止提交表单、禁止修改任何数据、禁止用 browser_evaluate、禁止反复重试；'
    '如果确实没有可下载的附件，就明确说「没有可下载的附件」并结束。'
)

# Turn 2 of the adaptive `download-urls` task: turn 1 only reconnoitred, so this turn spends its
# whole budget on opening one candidate and downloading its attachment.
DOWNLOAD_URLS_TURN2 = (
    '第二步：上一步你读到的候选链接如下（原文）：\n{urls}\n'
    '现在只做下载，**最多 5 次工具调用**（含 browser_navigate），到 5 次必须立刻回答。'
    '1) 用 browser_navigate 打开其中最可能含附件的那一条；2) 读取页面正文，找「附件」「下载」链接或回形针图标；'
    '3) 点击附件链接完成下载（如果附件链接是可直接打开的 URL，也可以直接 browser_navigate 打开它触发下载）。'
    '然后**立刻**用中文回答：通知标题、附件文件名（工具返回的原文）、保存路径（工具返回的原文）、当前页面 URL。'
    '禁止提交表单、禁止修改任何数据、禁止反复重试；若这些链接里确实没有可下载的附件，请明确说明，'
    '并给出你看到的正文里的链接原文。'
)

# Turn 2 of `download-flow`: open one candidate and report its attachment links.
FLOW_TURN2 = (
    '第 2/3 步：只打开详情、找附件链接，**不要下载**、不要提交任何表单。**最多 5 次工具调用**，用完必须立刻回答。\n'
    '上一步你的结论：\n{candidates}\n'
    '请按可能性从高到低打开这些通知的详情页（通知公告列表里点标题就会进入详情，'
    '详情页 URL 形如 `.../jwglxt/xtgl/xwgl_ckXw.html?xwbh=<ID>&doType=save`），'
    '在正文里找「附件」「下载」字样或回形针/文件图标。\n'
    '然后**立刻**用中文回答：'
    'A) 你打开的每条通知的标题与详情页 URL 原文；'
    'B) 其中任何附件的**链接 URL 原文**与附件文件名（一个字都不要改）；'
    'C) 如果都没有附件，明确说「没有附件」，并给出正文里出现的全部链接原文。'
)

# Turn 3 of `download-flow`: spend the whole budget on the download itself.
FLOW_TURN3 = (
    '第 3/3 步：完成下载。**最多 5 次工具调用**，用完必须立刻回答。\n'
    '上一步你报告的候选链接原文：\n{urls}\n'
    '请打开/点击其中真正的附件链接完成下载（如果它就是文件直链，可以直接用 browser_navigate 打开触发下载；'
    '如果是详情页里的链接，就回到该详情页点击它）。\n'
    '然后**立刻**用中文回答：附件文件名（工具返回的原文）、保存路径（工具返回的原文）、来源页面 URL。'
    '禁止提交表单、禁止修改任何数据；如果确实没有可下载的附件，就明确说明并给出你尝试过的链接原文。'
)


def poll_turn(client, base, turn_id, deadline, progress_path, settle_on_result=False):
    """Poll one turn to settlement, appending progress so a killed client still leaves a trace.

    `settle_on_result` covers an instance whose turn row never leaves `running` even after the
    runtime produced its final answer (observed on the pre-prewarm instance the operator was
    already running on port 18100 — its journal holds the `result` event while `turns.status`
    stays `running`). The runtime result is then the settlement authority; the turn is still
    cancelled afterwards so no turn is left running.
    """
    cursor, events = 0, []
    answered_at = None
    while time.monotonic() < deadline:
        view = client.get(base + f'/api/v1/agent/turns/{turn_id}', params={'after': cursor}, timeout=30).json()
        events.extend(view['events']['items'])
        cursor = view['events']['next_cursor']
        if answered_at is None and any(e['kind'] == 'result' for e in events):
            answered_at = time.monotonic()
        settled_by = 'turn_status'
        if answered_at is not None and settle_on_result and time.monotonic() - answered_at > 15:
            settled_by = 'result_event'
        with progress_path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'at': time.strftime('%H:%M:%S'), 'turn': turn_id, 'status': view['status'],
                                     'events': len(events), 'answered': answered_at is not None,
                                     'settled_by': settled_by if settled_by == 'result_event' else None}) + '\n')
        if settled_by == 'result_event':
            try:
                client.post(base + f'/api/v1/agent/turns/{turn_id}/cancel', timeout=20)
                time.sleep(2)
                view = client.get(base + f'/api/v1/agent/turns/{turn_id}', params={'after': cursor}, timeout=30).json()
                events.extend(view['events']['items'])
            except requests.RequestException:
                pass
            view['runtime_result'] = next((e['data'] for e in events if e['kind'] == 'result'), None)
            return view, events, True, settled_by
        if view['status'] not in ('queued', 'running', 'stopping'):
            return view, events, True, settled_by
        time.sleep(1)
    # Never leave a turn running on the operator's instance; the owner cookie this session
    # already holds is enough to cancel it.
    try:
        client.post(base + f'/api/v1/agent/turns/{turn_id}/cancel', timeout=20)
    except requests.RequestException:
        pass
    view = client.get(base + f'/api/v1/agent/turns/{turn_id}', params={'after': cursor}, timeout=30).json()
    return view, events, False, 'timeout'


def collect(events):
    calls, results, downloads, timeline = [], [], [], []
    for event in events:
        if event['kind'] != 'harness_event':
            timeline.append({'kind': event['kind'], 'data': str(event.get('data'))[:120],
                             'at': event['created_at']})
            continue
        payload = event['data']['event']
        timeline.append({'kind': payload['type'], 'data': str(payload['data'].get('name', ''))[:60],
                         'at': event['created_at']})
        if payload['type'] == 'tool/call':
            calls.append({'name': payload['data']['name'], 'arguments': str(payload['data'].get('arguments'))[:300]})
        elif payload['type'] == 'tool/result':
            text = ' '.join(block.get('text', '') for block in payload['data']['message'].get('content', [])
                            if isinstance(block, dict))
            results.append(text[:1500])
            downloads.extend(re.findall(r'Downloaded file (\S+) to "([^"]+)"', text))
    return calls, results, downloads, timeline


def post_turn(client, base, cid, text, key, wait_seconds=90):
    """Post a turn, waiting out the previous turn's teardown.

    The API refuses a second turn while the first is still stopping (`TURN_IN_PROGRESS`);
    a cancelled turn needs a few seconds to finish tearing its runner down.
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        posted = client.post(base + f'/api/v1/agent/conversations/{cid}/turns', json={'text': text},
                             headers={'Idempotency-Key': key}, timeout=30)
        if posted.ok:
            return posted.json()['id']
        code = ''
        try:
            code = posted.json().get('error', {}).get('code', '')
        except ValueError:
            pass
        if code != 'TURN_IN_PROGRESS' or time.monotonic() > deadline:
            raise AssertionError(posted.text)
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True, help='base URL of a running app instance that owns the browser')
    parser.add_argument('--task', choices=sorted(PROMPTS), default='login-read')
    parser.add_argument('--timeout', type=int, default=600, help='per-turn timeout in seconds')
    parser.add_argument('--settle-on-result', action='store_true',
                        help='treat the runtime result event as settlement when the instance never finalizes the turn')
    parser.add_argument('--browser-home', default=str(ROOT / 'var' / 'agent' / 'browser'),
                        help='browser home of the target instance; its action ledger proves downloads')
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    directory = ROOT / 'var' / ('real-school-browser-' + time.strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True, exist_ok=True)
    progress_path = directory / 'progress.jsonl'
    client = requests.Session()
    evidence = {'kind': 'real_school_browser_read_only_trial', 'task': args.task, 'base_url': base,
                'target': TARGET, 'prompts': list(PROMPTS[args.task])}

    health = client.get(base + '/api/v1/health', timeout=10)
    assert health.ok, health.text
    created = client.post(base + '/api/v1/agent/conversations',
                          json={'provider_id': PROVIDER_ID, 'send_to_provider': True}, timeout=30)
    assert created.ok, created.text
    cid = created.json()['id']
    evidence['conversation_id'] = cid

    all_calls, all_results, all_downloads, all_timeline, turns = [], [], [], [], []
    timed_out = False
    prompts = list(PROMPTS[args.task])
    index = 0
    while index < len(prompts):
        text = prompts[index]
        if index == 1:
            evidence['prompts'].append(text)
        turn_id = post_turn(client, base, cid, text, f'real-school-browser-{index + 1}')
        view, events, settled, settled_by = poll_turn(client, base, turn_id, time.monotonic() + args.timeout,
                                                     progress_path, args.settle_on_result)
        calls, results, downloads, timeline = collect(events)
        all_calls += calls
        all_results += results
        all_downloads += downloads
        all_timeline += timeline
        answer = view.get('response') or (view.get('runtime_result') or {}).get('response', '')
        turns.append({'id': turn_id, 'status': view['status'], 'settled_by': settled_by, 'response': answer,
                      'error': view.get('error'), 'events': len(events), 'settled': settled})
        timed_out = timed_out or not settled
        if not settled or index >= 2:
            break
        urls = [u.rstrip('.,;') for u in re.findall(r'https?://[^\s`<>"\'）)｜|]+', answer)]
        urls = list(dict.fromkeys(urls))[:3]
        if args.task == 'download':
            if index >= 1:
                break
            # Build turn 2 of the download task from turn 1's answer.
            listed = re.findall(r'[「《"\']([^「」《》"\']{4,60})[」》"\']', answer) or \
                     re.findall(r'^\s*\d+[.、)]\s*(\S.{3,59})$', answer, re.M)
            title = listed[0] if listed else '列表中最靠前的带附件的通知'
            prompts.append(DOWNLOAD_TURN2.format(title=title))
        elif args.task == 'download-urls':
            if index >= 1 or not urls:
                break
            # Hand the candidate links turn 1 reported back to the model verbatim.
            prompts.append(DOWNLOAD_URLS_TURN2.format(urls='\n'.join(urls)))
            evidence['turn2_urls'] = urls
        elif args.task == 'download-flow':
            if index == 0:
                prompts.append(FLOW_TURN2.format(candidates=answer[:800]))
            elif urls:
                prompts.append(FLOW_TURN3.format(urls='\n'.join(urls)))
                evidence['turn3_urls'] = urls
            else:
                break
        else:
            break
        index += 1

    # The browser host's own ledger is independent of the model's answer: a file on disk plus a
    # ledger row is proof a real download happened in the logged-in profile.
    ledger_downloads, ledger_path = [], Path(args.browser_home) / 'actions.json'
    if ledger_path.is_file():
        for item in json.loads(ledger_path.read_text(encoding='utf-8')).get('downloads', []):
            if item.get('conversation_id') != cid:
                continue
            file = Path(item.get('path', ''))
            row = dict(item)
            row['exists'] = file.is_file()
            row['bytes'] = file.stat().st_size if row['exists'] else None
            row['sha256'] = hashlib.sha256(file.read_bytes()).hexdigest() if row['exists'] else None
            ledger_downloads.append(row)
    actions = client.get(base + f'/api/v1/agent/conversations/{cid}/actions', timeout=20).json().get('items', [])
    final = turns[-1]
    response = final['response']
    evidence['timed_out'] = timed_out
    evidence['turns'] = turns
    evidence['ledger_downloads'] = ledger_downloads
    evidence['turn'] = {'id': final['id'], 'status': final['status'], 'settled_by': final['settled_by'],
                        'response': response, 'error': final['error'], 'events': final['events']}
    evidence['checks'] = {
        'turns_attempted': len(turns),
        'turn_succeeded': all(turn['status'] == 'succeeded' or turn['settled_by'] == 'result_event'
                              for turn in turns),
        'browser_tool_calls': [c['name'] for c in all_calls if 'browser' in c['name']],
        'downloads': all_downloads,
        'ledger_downloads': len(ledger_downloads),
        'pending_actions': len(actions),
        'mentions_logged_in': bool(re.search(r'已登录|登录状态|学号|姓名|身份', response)),
        'answer_chars': len(response),
    }
    evidence['timeline'] = all_timeline
    evidence['ok'] = bool(evidence['checks']['turn_succeeded'] and evidence['checks']['browser_tool_calls']
                          and evidence['checks']['answer_chars'] > 50)
    if args.task in ('download', 'download-search', 'download-urls', 'download-flow', 'download-home',
                     'download-direct'):
        evidence['checks']['downloaded_file'] = bool(ledger_downloads) or bool(all_downloads)
        evidence['ok'] = bool(evidence['ok'] and evidence['checks']['downloaded_file'])
    (directory / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    (directory / 'transcript.json').write_text(json.dumps({'calls': all_calls, 'results': all_results},
                                                          ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'ok': evidence['ok'], 'task': args.task, 'evidence': str(directory),
                      'checks': evidence['checks'], 'responses': [turn['response'][:400] for turn in turns]},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
