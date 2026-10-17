"""Drive the four real task levels through the product's ordinary Agent API.

This is the takeover-phase driver: it does not touch the fixed benchmark catalog and it
never fabricates a tool result. It submits a natural-language task to a running API,
records the complete event journal (tool calls, tool results, final answer, timings),
and applies a small source-grounding check where an objective oracle exists.

    python tools/verify_takeover_tasks.py --task L1 --api http://127.0.0.1:8017
    python tools/verify_takeover_tasks.py --task all --api http://127.0.0.1:8017

The API must be a source-matched service started outside the DSH sandbox (the official
SDK spawns its runner over piped stdio, which the sandbox denies with `spawn EPERM`).
Evidence is written under `var/takeover-tasks/<task>-<stamp>/`.
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

TASKS = {
    'L1': {
        'title': '自主联网：浙江大学最近的公开学术活动',
        'prompt': '帮我搜索浙江大学最近的公开学术活动，并告诉我来源。',
        'expects': '真实联网工具调用（web_search/web_fetch/浏览器）；回答含可核实来源 URL；'
                   '不得凭模型记忆编造',
        'kind': 'source_grounded',
        'web_tools': ('web_search', 'web_fetch', 'browser_navigate', 'browser_snapshot'),
    },
    'L2': {
        'title': '校园工具调用：明天的课',
        'prompt': '查询我明天有哪些课。',
        'expects': '真实课表/课程工具调用；若本机缓存或学校会话不足必须如实说明；不得编造课表',
        'kind': 'campus_grounded',
        'campus_tools': ('courses.list', 'campus.query', 'campus.sync', 'campus.academics',
                         'connections.courses', 'campus.status'),
    },
    'L3': {
        'title': '多工具协同：结合明天课表安排两小时自习',
        'prompt': '结合我明天的课表，帮我安排两个小时自习。',
        'expects': '课表/可用时间工具 + 计划预览；不得假装查到实时空教室/座位',
        'kind': 'campus_grounded',
        'campus_tools': ('courses.list', 'campus.query', 'campus.academics', 'plans.preview',
                         'plans.read', 'availability.read', 'campus.status'),
    },
    'L4': {
        'title': '长任务：整理课程学习资料并安排复习计划',
        'prompt': ('帮我整理这门课最近的学习资料，并给我安排一个复习计划。'
                   '如果我还需要说明是哪门课，就先根据我能看到的数据判断；缺少数据时如实说明。'),
        'expects': '多步工具链（资料检索/读取 + 计划）；结果有实际资料依据；失败可继续',
        'kind': 'campus_grounded',
        'campus_tools': ('materials.search', 'materials.read', 'materials.list', 'courses.list',
                         'assignments.list', 'plans.preview', 'plans.read', 'jobs.read',
                         'campus.query', 'campus.sync'),
    },
}


def stamp() -> str:
    return time.strftime('%Y%m%d-%H%M%S')


class Agent:
    def __init__(self, base: str, timeout_s: int = 300):
        self.base = base.rstrip('/')
        self.client = requests.Session()
        self.timeout_s = timeout_s

    def conversation(self, provider_id: str = PROVIDER_ID) -> str:
        created = self.client.post(self.base + '/api/v1/agent/conversations',
                                   json={'provider_id': provider_id, 'send_to_provider': True}, timeout=30)
        created.raise_for_status()
        return created.json()['id']

    def run_turn(self, conversation_id: str, text: str, label: str) -> dict:
        started = time.monotonic()
        posted = self.client.post(self.base + f'/api/v1/agent/conversations/{conversation_id}/turns',
                                  json={'text': text}, headers={'Idempotency-Key': f'takeover-{label}'},
                                  timeout=30)
        posted.raise_for_status()
        turn_id = posted.json()['id']
        cursor, events = 0, []
        view = None
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            view = self.client.get(self.base + f'/api/v1/agent/turns/{turn_id}',
                                   params={'after': cursor}, timeout=30).json()
            events.extend(view['events']['items'])
            cursor = view['events']['next_cursor']
            if view['status'] not in ('queued', 'running', 'stopping'):
                break
            time.sleep(0.4)
        else:
            raise AssertionError('turn timed out: ' + turn_id)
        return {'turn_id': turn_id, 'status': view['status'], 'response': view.get('response', ''),
                'error': view.get('error'), 'events': events,
                'submission_to_completion_ms': round((time.monotonic() - started) * 1000, 1)}


def classify(events: list[dict]) -> tuple[list[dict], list[dict], str]:
    tools, model_calls = [], []
    first_message = None
    for event in events:
        if event.get('kind') != 'harness_event':
            continue
        payload = event['data']['event']
        data = payload.get('data') or {}
        if payload.get('type') == 'tool/call':
            tools.append({'name': data.get('name'), 'arguments': data.get('arguments')})
        elif payload.get('type') == 'tool/result':
            text = json.dumps(data, ensure_ascii=False)
            tools.append({'name': None, 'result_text': text[:20000]})
        elif payload.get('type') == 'model/call':
            model_calls.append(data)
        elif payload.get('type') == 'assistant/message' and first_message is None:
            first_message = time.time()
    return tools, model_calls, first_message


def normalize_tool_name(name: str | None) -> str:
    """Map a Harness model-facing name back to the domain tool name.

    Domain tools are registered as `campus_campus_academics` / `campus_courses_list`
    (fixed `campus_` prefix + dots replaced by underscores), so trace-based checks must
    normalize before matching a catalog name such as `campus.academics` / `courses.list`.
    """
    if not name:
        return ''
    if name.startswith('mcp__campus_browser__'):
        return name[len('mcp__campus_browser__'):]
    if name.startswith('campus_campus_'):
        return 'campus.' + name[len('campus_campus_'):]
    if name.startswith('campus_') and name.count('_') >= 1:
        return name[len('campus_'):].replace('_', '.', 1)
    return name


def tool_names(events: list[dict]) -> list[str]:
    names = []
    for event in events:
        if event.get('kind') == 'harness_event' and event['data']['event'].get('type') == 'tool/call':
            names.append(normalize_tool_name(event['data']['event']['data'].get('name')))
    return names


def tool_result_text(events: list[dict]) -> str:
    chunks = []
    for event in events:
        if event.get('kind') == 'harness_event' and event['data']['event'].get('type') == 'tool/result':
            chunks.append(json.dumps(event['data']['event'].get('data'), ensure_ascii=False))
    return '\n'.join(chunks)


def citations(answer: str) -> list[str]:
    return re.findall(r'https?://[^\s)>\]}）】」，,、；]+', answer)


def evaluate(task: str, run: dict) -> dict:
    spec = TASKS[task]
    names = tool_names(run['events'])
    results = tool_result_text(run['events'])
    answer = run.get('response') or ''
    urls = citations(answer)
    urls_grounded = [url for url in urls if url.rstrip('.,;') in results]
    checks: dict = {'tool_calls': names, 'turn_status': run['status'],
                    'cited_urls': urls, 'cited_urls_found_in_tool_results': urls_grounded}
    if spec['kind'] == 'source_grounded':
        web_calls = [n for n in names if n in spec['web_tools']]
        checks['web_tool_calls'] = web_calls
        checks['grounded'] = bool(web_calls) and bool(urls_grounded)
        checks['verdict'] = ('真实联网并引用可核实来源' if checks['grounded']
                             else ('有联网调用但未给出工具结果中的 URL' if web_calls
                                   else '没有观察到联网工具调用'))
    else:
        campus_calls = [n for n in names if n in spec['campus_tools']]
        checks['campus_tool_calls'] = campus_calls
        honest_gap = bool(re.search(r'(无法确认|无法取得|没有可用依据|尚未确认|不打算猜测|无法判断|'
                                    r'请你在浏览器|尚未支持|没有.*时间)', answer))
        checks['honest_gap_disclosed'] = honest_gap
        if campus_calls and honest_gap:
            checks['verdict'] = '观察到真实校园数据工具调用；数据/会话缺口如实说明（未编造）'
        elif campus_calls:
            checks['verdict'] = '观察到真实校园数据工具调用'
        else:
            checks['verdict'] = '没有观察到校园数据工具调用（可能是模型凭记忆回答，不可接受）'
        # A campus answer must not claim school data when no campus tool ran.
        checks['fabrication_risk'] = bool(re.search(r'(课表|课程安排|座位|教室)', answer)) and not campus_calls
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', default='L1', choices=[*TASKS, 'all'])
    parser.add_argument('--api', default='http://127.0.0.1:8017')
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--provider-id', default=PROVIDER_ID)
    args = parser.parse_args()
    tasks = list(TASKS) if args.task == 'all' else [args.task]
    agent = Agent(args.api, timeout_s=args.timeout)
    overall = []
    for task in tasks:
        spec = TASKS[task]
        batch = f'{task}-{stamp()}'
        directory = ROOT / 'var' / 'takeover-tasks' / batch
        directory.mkdir(parents=True, exist_ok=True)
        summary: dict = {'task': task, 'title': spec['title'], 'prompt': spec['prompt'],
                         'expects': spec['expects'], 'api': args.api, 'provider_id': args.provider_id,
                         'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
        try:
            conversation_id = agent.conversation(args.provider_id)
            summary['conversation_id'] = conversation_id
            run = agent.run_turn(conversation_id, spec['prompt'], batch)
            summary['run'] = {key: run[key] for key in
                              ('turn_id', 'status', 'response', 'error', 'submission_to_completion_ms')}
            summary['events'] = run['events']
            summary['checks'] = evaluate(task, run)
            summary['ok'] = summary['checks'].get('verdict')
        except Exception as error:  # keep the evidence even when the turn fails
            summary['exception'] = f'{type(error).__name__}: {error}'
        (directory / 'transcript.json').write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        (directory / 'evidence.json').write_text(
            json.dumps({key: value for key, value in summary.items() if key != 'events'},
                       ensure_ascii=False, indent=2), encoding='utf-8')
        print('=' * 70)
        print(task, spec['title'])
        print('status:', summary.get('run', {}).get('status'))
        print('elapsed_ms:', summary.get('run', {}).get('submission_to_completion_ms'))
        names = summary.get('checks', {}).get('tool_calls', [])
        print('tool_calls:', names)
        print('verdict:', summary.get('checks', {}).get('verdict'))
        print('answer:', (summary.get('run', {}).get('response') or '')[:1200].replace('\n', ' | '))
        print('evidence:', directory)
        overall.append({'task': task, 'status': summary.get('run', {}).get('status'),
                        'verdict': summary.get('checks', {}).get('verdict'),
                        'evidence': str(directory)})
    print('SUMMARY', json.dumps(overall, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
