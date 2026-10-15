"""Run the three required real scenarios with the official provider (completion condition 4).

This drives the product path end to end: real uvicorn API, real `apps/agent-runtime`
Harness, real official DeepSeek provider and key, real campus data imported from the
legacy database. Nothing here is a controlled or synthetic model.

    python tools/verify_real_scenarios.py --scenario s1
    python tools/verify_real_scenarios.py --scenario all

Cost is real. Every model call is counted from the API log; the script never prints
credentials and writes evidence under `var/real-scenarios-<id>/`.

Scenarios:
    s1  public web research with a cited URL
    s2  imported campus information -> plan preview -> accepted change -> revoke
    s3  attachment read -> openable deliverable
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / 'apps' / 'api'
SOURCE_DB = ROOT / 'var' / 'personal.db'
LEGACY_DB = ROOT / 'data' / 'campus.sqlite3'
REAL_SECRETS = ROOT / 'var' / 'secrets'
PROVIDER_ID = 'prv_c68d2878'
LEGACY_OWNER = '4e7ff483e5be4013b59e15be39d62a37'

sys.path.insert(0, str(API))
from app.process_owner import spawn_owned  # noqa: E402


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def stamp():
    return time.strftime('%Y%m%d-%H%M%S')


class Session:
    """One real API process plus the HTTP surface used by the scenarios."""

    def __init__(self, home: Path, name: str):
        self.home = home
        self.name = name
        self.port = free_port()
        self.base = f'http://127.0.0.1:{self.port}'
        self.client = requests.Session()
        self.process = None
        self.log = None
        self.conversation_id = None
        self.turns: list[dict] = []

    # ---------------------------------------------------------------- lifecycle
    def prepare(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / 'files').mkdir(exist_ok=True)
        (self.home / 'workspace').mkdir(exist_ok=True)
        (self.home / 'agent').mkdir(exist_ok=True)
        # Consistent copy of the real database (the app may be running in WAL mode).
        source = sqlite3.connect('file:' + str(SOURCE_DB) + '?mode=ro', uri=True)
        target = sqlite3.connect(str(self.home / 'domain.sqlite'))
        with target:
            source.backup(target)
        source.close()
        target.close()
        return {
            'PYTHONUTF8': '1',
            'STUDY_DATABASE_URL': 'sqlite:///' + (self.home / 'domain.sqlite').as_posix(),
            'STUDY_FILES_DIR': str(self.home / 'files'),
            'STUDY_SECRETS_DIR': str(REAL_SECRETS),
            'CAMPUS_AGENT_HOME': str(self.home / 'agent'),
            'CAMPUS_WORKSPACE_DIR': str(self.home / 'workspace'),
            # The runtime falls back to 127.0.0.1:8000 when this is absent; the launcher
            # always sets it, so a driver script must set it too.
            'CAMPUS_BRIDGE_URL': self.base,
            'STUDY_WORKER_INPROC': '0',
        }

    def import_legacy(self):
        target = self.home / 'agent' / 'campus' / 'workspace.sqlite3'
        target.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [sys.executable, str(ROOT / 'tools' / 'migrate_campus_data.py'), 'import',
             '--source', str(LEGACY_DB), '--target', str(target), '--owner', LEGACY_OWNER,
             '--batch-id', 'real-scenarios-' + self.name, '--school-cache'],
            cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
        if completed.returncode != 0:
            raise SystemExit('legacy import failed: ' + completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def start(self, env):
        self.log = (self.home / 'api.log').open('a', encoding='utf-8')
        started = time.monotonic()
        self.process = spawn_owned([sys.executable, '-m', 'uvicorn', 'app.main:app',
                                    '--host', '127.0.0.1', '--port', str(self.port)],
                                   cwd=API, env=dict(os.environ, **env), stdout=self.log, stderr=self.log,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            assert self.process.poll() is None, (self.home / 'api.log').read_text(encoding='utf-8')
            try:
                if self.client.get(self.base + '/api/v1/health', timeout=1).ok:
                    return round((time.monotonic() - started) * 1000, 1)
            except requests.RequestException:
                pass
            time.sleep(.05)
        raise AssertionError('API startup timed out')

    def stop(self):
        if self.process is not None:
            owner = getattr(self.process, 'campus_owner', None)
            if owner is not None:
                owner.close()
            else:
                self.process.terminate()
            self.process.wait(timeout=20)
        self.client.close()
        if self.log is not None:
            self.log.close()

    # ------------------------------------------------------------------- product
    def conversation(self):
        created = self.client.post(self.base + '/api/v1/agent/conversations',
                                   json={'provider_id': PROVIDER_ID, 'send_to_provider': True}, timeout=30)
        assert created.ok, created.text
        self.conversation_id = created.json()['id']
        return self.conversation_id

    def model_calls(self):
        return len(re.findall(r'"POST /api/v1/agent/internal/model', (self.home / 'api.log').read_text(
            encoding='utf-8', errors='replace')))

    def run_turn(self, text, attachments=None, action_id=None, timeout=420):
        calls_before = self.model_calls()
        body = {'text': text}
        if attachments:
            body['attachment_ids'] = attachments
        if action_id:
            body['action_id'] = action_id
        posted = self.client.post(self.base + f'/api/v1/agent/conversations/{self.conversation_id}/turns',
                                  json=body, headers={'Idempotency-Key': 'real-%s-%d' % (self.name, len(self.turns))},
                                  timeout=30)
        assert posted.ok, posted.text
        turn_id = posted.json()['id']
        cursor, events = 0, []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = self.client.get(self.base + f'/api/v1/agent/turns/{turn_id}', params={'after': cursor},
                                   timeout=30).json()
            events.extend(view['events']['items'])
            cursor = view['events']['next_cursor']
            if view['status'] not in ('queued', 'running', 'stopping'):
                break
            time.sleep(.5)
        else:
            raise AssertionError('turn timed out: ' + turn_id)
        assert view['status'] == 'succeeded', json.dumps(view, ensure_ascii=False)[:1200]
        tool_calls, tool_results = [], []
        for event in events:
            if event['kind'] != 'harness_event':
                continue
            payload = event['data']['event']
            if payload['type'] == 'tool/call':
                tool_calls.append(payload['data']['name'])
            elif payload['type'] == 'tool/result':
                tool_results.append(str(payload['data'].get('message', {}).get('content'))[:4000])
        turn = {'text': text, 'turn_id': turn_id, 'status': view['status'], 'response': view.get('response', ''),
                'tool_calls': tool_calls, 'tool_results': tool_results,
                'model_requests': self.model_calls() - calls_before,
                'events': len(events), 'attachment_ids': attachments or []}
        self.turns.append(turn)
        return turn

    def pending_actions(self):
        return self.client.get(self.base + f'/api/v1/agent/conversations/{self.conversation_id}/actions',
                               timeout=30).json().get('items', [])

    def accept(self, action_id, operation):
        done = self.client.post(self.base + f'/api/v1/agent/conversations/{self.conversation_id}'
                                           f'/actions/{action_id}/accept',
                                json={'operation': operation},
                                headers={'Idempotency-Key': f'real-{self.name}-{operation}-{action_id[:12]}'},
                                timeout=120)
        return done.status_code, done.text[:2000]


def write_evidence(name, payload, transcript):
    directory = ROOT / 'var' / ('real-scenarios-%s' % name)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'transcript.json').write_text(json.dumps(transcript, ensure_ascii=False, indent=2),
                                               encoding='utf-8')
    (directory / 'evidence.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return directory


# --------------------------------------------------------------------- scenarios
def scenario_s1():
    return {
        'id': 's1',
        'goal': '公开网页检索并引用',
        'prompt': ('请用公开网页检索确认 DeepSeek-V3 的发布/开源时间。要求：真正访问公开网页（不要凭记忆），'
                   '给出结论，并在回答里附上你实际访问过的网页完整 URL 作为引用出处；同时说明你访问的页面标题。'),
        'expects': '至少一次网页工具调用；回答文本包含 http(s) URL',
    }


def scenario_s2():
    return {
        'id': 's2',
        'goal': '校园信息结合计划修改并撤销',
        'prompt': ('先读取我已导入的校园信息（通知/资料/课程缓存），简要说明你现在能看到的校园事实与来源。'
                   '然后基于这些信息，为「明天 19:00-21:00」安排一次复习计划，只给我预览（不要生效），'
                   '并列出你打算创建的时间块。如果还没有可安排的学习任务，先说明缺少什么来源。'),
        'apply': '我已在界面上接受了那份预览，请执行这次安排，并说明现在生效的计划版本号与时间块。',
        'revoke': '我已在界面上撤销了刚才那次安排，请重新读取计划状态，说明现在生效的是哪一版、'
                  '这次改动是否已经被撤销。',
        'expects': '出现校园读取工具调用；产生待确认的计划动作并能被接受；接受后计划生效；随后被撤销',
    }


def scenario_s3():
    return {
        'id': 's3',
        'goal': '附件读取并生成可打开产物',
        'prompt': ('请读取本轮附件的内容，然后生成一份可以直接打开的 Markdown 交付物文件，'
                   '文件名 todo-report.md，内容包括：附件条目的数量、按优先级分组的清单，以及一条学习建议。'
                   '生成后用交付工具把它发布给我。'),
        'expects': '出现附件读取与写文件工具调用；工作区或已发布文件里存在非空且可读的 todo-report.md',
    }


SCENARIOS = {'s1': scenario_s1, 's2': scenario_s2, 's3': scenario_s3}


def run(name, keep):
    spec = SCENARIOS[name]()
    home = ROOT / 'var' / ('real-scenarios-home-' + name + '-' + stamp())
    session = Session(home, name)
    transcript = {'scenario': spec, 'home': str(home), 'provider': PROVIDER_ID, 'model': 'deepseek-flash'}
    summary = {'id': name, 'goal': spec['goal'], 'home': str(home), 'ok': False, 'checks': {}}
    try:
        env = session.prepare()
        transcript['legacy_import'] = session.import_legacy()
        transcript['startup_ms'] = session.start(env)
        session.conversation()
        if name == 's1':
            turn = session.run_turn(spec['prompt'])
            urls = re.findall(r'https?://[^\s)>\]}）】」，,]+', turn['response'])
            web_tools = [call for call in turn['tool_calls'] if 'web' in call or 'fetch' in call or 'search' in call]
            summary['checks'] = {'turn_succeeded': True, 'web_tool_calls': web_tools,
                                 'cited_urls_in_answer': urls,
                                 'url_also_in_tool_output': any(url in result for url in urls
                                                                for result in turn['tool_results'])}
            summary['ok'] = bool(web_tools) and bool(urls)
        elif name == 's2':
            # Real product flow, as the UI drives it: the model reads the imported campus
            # facts and previews a plan change; the user accepts it; a turn carrying that
            # approved action executes it; the user then revokes through the same action.
            first = session.run_turn(spec['prompt'])
            campus_calls = [call for call in first['tool_calls'] if 'campus' in call or 'notice' in call]
            actions = session.pending_actions()
            previews = [action for action in actions if 'plan' in json.dumps(action, ensure_ascii=False)]
            preview_id = previews[0]['id'] if previews else None
            apply_status = session.accept(preview_id, 'apply')[0] if preview_id else None
            apply_turn = session.run_turn(spec['apply'], action_id=preview_id) if preview_id else None
            applied_state = plan_rows(session.home / 'domain.sqlite')
            revoke_status, revoke_body = session.accept(preview_id, 'revoke') if preview_id else (None, '')
            revoke_action_id = json.loads(revoke_body)['id'] if revoke_status and revoke_status < 400 else None
            revoke_turn = session.run_turn(spec['revoke'], action_id=revoke_action_id) if revoke_action_id else None
            final_state = plan_rows(session.home / 'domain.sqlite')
            summary['checks'] = {'campus_tool_calls': campus_calls,
                                 'tool_calls_first': first['tool_calls'],
                                 'tool_calls_apply': apply_turn['tool_calls'] if apply_turn else None,
                                 'tool_calls_revoke': revoke_turn['tool_calls'] if revoke_turn else None,
                                 'preview_action_id': preview_id,
                                 'apply_status': apply_status,
                                 'plans_after_apply': applied_state['versions'],
                                 'applied_plans': applied_state['applied'],
                                 'revoke_status': revoke_status,
                                 'revoke_action_id': revoke_action_id,
                                 'final_plans': final_state['versions'],
                                 'revoked_plans': final_state['revoked'],
                                 'first_answer': first['response'][:800],
                                 'apply_answer': (apply_turn['response'][:800] if apply_turn else None),
                                 'final_answer': (revoke_turn['response'][:800] if revoke_turn else None)}
            summary['ok'] = bool(campus_calls) and bool(preview_id) and bool(apply_status and apply_status < 400) \
                and bool(applied_state['applied']) and bool(revoke_status and revoke_status < 400) \
                and bool(final_state['revoked'])
        else:
            attachment = make_attachment(session)
            uploaded = session.client.post(
                session.base + f'/api/v1/agent/conversations/{session.conversation_id}/files',
                files={'file': (attachment.name, attachment.read_bytes(), 'text/markdown')},
                headers={'Idempotency-Key': 'real-%s-attachment' % name}, timeout=60)
            assert uploaded.ok, uploaded.text
            file_id = uploaded.json()['id']
            turn = session.run_turn(spec['prompt'], attachments=[file_id])
            workspace = session.home / 'workspace'
            produced = [path for path in workspace.rglob('todo-report.md')]
            published = session.client.get(
                session.base + f'/api/v1/agent/conversations/{session.conversation_id}/files',
                timeout=30).json().get('items', [])
            body = ''
            if produced:
                body = produced[0].read_text(encoding='utf-8', errors='replace')
            summary['checks'] = {'turn_succeeded': True, 'uploaded_file_id': file_id,
                                 'tool_calls': turn['tool_calls'],
                                 'workspace_artifacts': [str(path) for path in produced],
                                 'artifact_chars': len(body),
                                 'published_files': published[:3]}
            summary['ok'] = bool(produced) and len(body) > 40
        transcript['turns'] = session.turns
    finally:
        summary['model_requests'] = session.model_calls()
        session.stop()
        transcript['summary'] = summary
        directory = write_evidence(name, {'kind': 'real_official_model_scenario', **summary,
                                          'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}, transcript)
        summary['evidence'] = str(directory)
        if not keep:
            shutil.rmtree(home, ignore_errors=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def plan_rows(db: Path):
    connection = sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True)
    applied, revoked, versions = [], [], []
    for row in connection.execute('select id, version, status, applied_at, revoked_at from plans order by version'):
        versions.append({'id': row[0], 'version': row[1], 'status': row[2]})
        if row[3]:
            applied.append(row[0])
        if row[4]:
            revoked.append(row[0])
    connection.close()
    return {'versions': versions, 'applied': applied, 'revoked': revoked}


def make_attachment(session: Session):
    path = session.home / 'todo-input.md'
    path.write_text(
        '# 本周待办\n\n1. [高] 复习线性代数第 4 章\n2. [高] 提交数据结构实验报告\n'
        '3. [中] 阅读《计算机网络》第 3 章\n4. [低] 整理课程笔记\n5. [中] 预习概率论\n',
        encoding='utf-8')
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scenario', default='all', choices=['all', 's1', 's2', 's3'])
    parser.add_argument('--keep', action='store_true', help='keep the scratch home for inspection')
    args = parser.parse_args()
    names = list(SCENARIOS) if args.scenario == 'all' else [args.scenario]
    results = [run(name, args.keep) for name in names]
    print('SUMMARY', json.dumps([{'id': r['id'], 'ok': r['ok'], 'model_requests': r['model_requests']}
                                 for r in results], ensure_ascii=False))


if __name__ == '__main__':
    main()
