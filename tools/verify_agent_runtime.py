"""Real Harness -> actual FastAPI bridge -> real synthetic domain DB integration.

Only the model is controlled. Provider responses derive domain facts exclusively
from the actual tool messages. Saves evidence; never uses campus credentials.
Run with apps/api/.venv/Scripts/python.exe tools/verify_agent_runtime.py.
"""
from __future__ import annotations

import json
import csv
import io
import re
import argparse
import os
import socket
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "apps/api"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def verify(*, serve=False, web_port=15174, stop_file=None):
    (ROOT / "var").mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="agent-integration-", dir=ROOT / "var"))
    receipts = []
    slow_started = threading.Event()
    child_b_waiting, release_child_b = threading.Event(), threading.Event()
    browser_submissions = []
    stopped_script_started, stopped_script_late = threading.Event(), threading.Event()

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == '/browser/started':
                stopped_script_started.set()
            self.send_response(200)
            if self.path == '/browser/download':
                self.send_header('Content-Disposition', 'attachment; filename="browser-campus.txt"')
                self.end_headers(); self.wfile.write(b'actual-browser-attachment-918\n'); return
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            page = '<title>浏览器集成材料</title><h1>校园材料-632</h1><a id="download" href="/browser/download">下载资料</a>' \
                '<form action="/browser/submit" method="post"><label>姓名<input id="student" name="student"></label>' \
                '<button id="submit">提交测试材料</button></form>'
            self.wfile.write(page.encode('utf-8'))

        def do_POST(self):
            if self.path == '/browser/late':
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                stopped_script_late.set()
                self.send_response(200); self.end_headers(); self.wfile.write(b'ok'); return
            if self.path == '/browser/submit':
                from urllib.parse import parse_qs
                values = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
                browser_submissions.append(values.get('student', [''])[0])
                self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
                self.wfile.write('<title>测试提交回执</title><p>已收到本地测试材料</p>'.encode('utf-8')); return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == '/release-child-b':
                release_child_b.set()
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
                return
            receipts.append(body)
            # This broad regression explicitly requests every optional family.
            # The discovery-specific test separately asserts the minimal default
            # and one-group-only visibility. Never call a tool absent on the wire.
            names = {t['function']['name'] for t in body.get('tools', [])}
            optional = {'mcp__campus_browser__browser_navigate', 'list_mcp_resources',
                        'subagent', 'job_list', 'exit_plan_mode'}
            if not optional.issubset(names):
                assert 'load_tools' in names, names
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'fixture_load_' + str(len(receipts)), 'type': 'function', 'function': {
                        'name': 'load_tools', 'arguments': json.dumps({'tools': sorted(optional)})}}]},
                    'finish_reason': 'tool_calls'}]}).encode())
                return
            # Keep fixture step numbering independent of the new loader step;
            # the real session and evidence still contain its call and result.
            messages = [m for m in body['messages'] if not (
                m.get('tool_call_id', '').startswith('fixture_load_') or
                any(c.get('id', '').startswith('fixture_load_') for c in m.get('tool_calls', [])))]
            human = next((m for m in reversed(messages) if m['role'] == 'user' and '用户消息：\n' in m['content']), None)
            if human and '[V2持续目标演练]' in human['content']:
                boundary = next((m for m in reversed(messages) if m['role'] == 'user' and
                                 ('<goal_round>' in m['content'] or '用户消息：\n' in m['content'])), None)
                results = [m['content'] for m in messages[messages.index(boundary) + 1:] if m['role'] == 'tool']
                calls, content = [], None
                def goal_call(name, arguments):
                    return {'id': 'goal_' + str(len(receipts)), 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}
                if '<goal_round>' not in boundary['content']:
                    if not results:
                        calls = [goal_call('create_goal', {'objective': 'GOAL_673 整理真实文件并交付结果', 'max_goal_rounds': 4})]
                    else:
                        content = '持续目标已创建，继续推进。'
                elif not results:
                    calls = [goal_call('get_goal', {})]
                elif len(results) == 1:
                    calls = [goal_call('write', {'file_path': 'goal-673.md', 'content': '真实原生 Goal 续行结果-673\n'})]
                elif len(results) == 2:
                    goal = json.loads(results[0])['goal']
                    calls = [goal_call('update_goal', {'goal_id': goal['id'], 'revision': goal['revision'], 'action': 'complete'})]
                else:
                    content = '已完成持续目标，真实文件 goal-673.md 已写入。'
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': content, 'tool_calls': calls},
                    'finish_reason': 'tool_calls' if calls else 'stop'}]}, ensure_ascii=False).encode())
                return
            control_user = next((m for m in reversed(messages) if m['role'] == 'user' and '[V2子任务' in m['content']), None)
            if control_user is not None and control_user is next((m for m in reversed(messages) if m['role'] == 'user' and
                    ('用户消息：\n' in m['content'] or '[V2子任务' in m['content'])), None):
                index = messages.index(control_user)
                results = [m['content'] for m in messages[index + 1:] if m['role'] == 'tool']
                marker, calls, content = control_user['content'], [], None
                def native(name, args):
                    return {'id': 'child_' + str(len(receipts)) + '_' + name, 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}
                if '[V2子任务A]' in marker:
                    if not results:
                        calls = [native('pwsh', {'command': 'Start-Sleep -Seconds 30; Write-Output "SHOULD_NOT_COMPLETE"', 'description': '耗时子任务计算'})]
                    else:
                        content = '\n'.join(results)
                elif '[V2子任务B]' in marker:
                    if not results:
                        calls = [native('read', {'file_path': 'v2-browser-notes.md'})]
                    else:
                        child_b_waiting.set()
                        if serve:
                            # Browser QA can take longer than the automated driver.
                            # Release the controlled response after observing actual
                            # upstream cancellation, rather than racing a UI operator.
                            for _ in range(600):
                                with sqlite3.connect(run_dir / 'agent/journal.sqlite') as journal:
                                    latest = journal.execute('SELECT id FROM turns ORDER BY created_at DESC LIMIT 1').fetchone()[0]
                                    ended = journal.execute("SELECT data FROM events WHERE turn_id=? AND kind='subagent.finished'", (latest,)).fetchall()
                                if any(json.loads(row[0]).get('stopReason') == 'aborted' for row in ended):
                                    release_child_b.set()
                                    break
                                if release_child_b.wait(timeout=0.05):
                                    break
                        if not release_child_b.wait(timeout=30):
                            raise AssertionError('B was not released after actual selective cancellation')
                        content = '\n'.join(results)
                elif not results:
                    release_child_b.clear()
                    child_b_waiting.clear()
                    calls = [native('subagent', {'description': '独立计算 A', 'prompt': '[V2子任务A] 执行耗时计算', 'run_in_background': False}),
                             native('subagent', {'description': '独立阅读 B', 'prompt': '[V2子任务B] 读取实际文件', 'run_in_background': False})]
                    # Model call IDs are unique even when names match in one batch.
                    calls[1]['id'] += '_b'
                else:
                    content = '\n'.join(results)
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': content, 'tool_calls': calls},
                    'finish_reason': 'tool_calls' if calls else 'stop'}]}, ensure_ascii=False).encode())
                return
            # Upstream skills/runtime context may append user-role reminders.
            # Select the actual gateway prompt, rather than the last reminder.
            last_user = max(i for i, m in enumerate(messages) if m["role"] == "user" and "用户消息：\n" in m["content"])
            if "取消演练" in messages[last_user]["content"]:
                slow_started.set()
                time.sleep(2)
            user_text = messages[last_user]["content"].split("用户消息：\n", 1)[1]
            if user_text.startswith('[V2停止演练]'):
                results = [m['content'] for m in messages[last_user + 1:] if m['role'] == 'tool']
                if not results:
                    stopped_script_started.clear()
                    stopped_script_late.clear()
                delay = 90000 if serve else 15000
                code = f'''async (page) => {{
                  await page.goto('http://127.0.0.1:{provider.server_port}/browser/form');
                  await fetch('http://127.0.0.1:{provider.server_port}/browser/started');
                  await new Promise(resolve => setTimeout(resolve, {delay}));
                  await fetch('http://127.0.0.1:{provider.server_port}/browser/late', {{method:'POST',body:'fixture'}});
                  return '脚本执行完成';
                }}'''
                calls = [] if results else [{'id': 'stop_script_' + str(len(receipts)), 'type': 'function', 'function': {
                    'name': 'mcp__campus_browser__browser_run_code_unsafe', 'arguments': json.dumps({'code': code})}}]
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': '\n'.join(results) if results else None,
                    'tool_calls': calls}, 'finish_reason': 'tool_calls' if calls else 'stop'}]}, ensure_ascii=False).encode())
                return
            if user_text.startswith('[V2浏览器回执]'):
                context = json.loads(messages[last_user]['content'].splitlines()[1])
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                content = '操作状态：' + ','.join(a['status'] for a in context.get('browser_actions', []))
                self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]}).encode())
                return
            if user_text.startswith('[V2浏览器演练]'):
                actual_results = [m['content'] for m in messages[last_user + 1:] if m['role'] == 'tool']
                choices = [('browser_navigate', {'url': f'http://127.0.0.1:{provider.server_port}/browser/form'}),
                    ('browser_fill_form', {'fields': [{'target': '#student', 'name': '姓名', 'type': 'textbox', 'value': '浏览器学生-724'}]}),
                    ('browser_click', {'target': '#download'}), ('browser_click', {'target': '#submit'})]
                calls = []
                if len(actual_results) < len(choices):
                    name, args = choices[len(actual_results)]
                    calls = [{'id': 'browser_flow_' + str(len(receipts)), 'type': 'function', 'function': {
                        'name': 'mcp__campus_browser__' + name, 'arguments': json.dumps(args, ensure_ascii=False)}}]
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': None if calls else
                    ('已读取校园材料-632，下载资料并填写姓名。提交前请在浏览器工作区确认（needs_user）。'
                     if '校园材料-632' in '\n'.join(actual_results) and 'needs_user' in actual_results[-1] else '\n'.join(actual_results)),
                    'tool_calls': calls}, 'finish_reason': 'tool_calls' if calls else 'stop'}]}, ensure_ascii=False).encode())
                return
            if user_text.startswith('[V2文档演练]'):
                context = json.loads(messages[last_user]['content'].splitlines()[1])
                attachments = context.get('attachments', [])
                actual_results = [m['content'] for m in messages[last_user + 1:] if m['role'] == 'tool']
                stem = '文档成果-' + attachments[0]['id'][-6:] if attachments else '文档成果'
                spec = {}
                if len(actual_results) >= 3:
                    # Facts come from the actual read tool output, not fixture DB access.
                    lines = re.findall(r'^\s*\d+:\s?(.*)$', actual_results[2], re.MULTILINE)
                    source_rows = list(csv.reader(io.StringIO('\n'.join(lines))))
                    assert len(source_rows) > 1, actual_results[2]
                    columns, rows = source_rows[0], source_rows[1:]
                    rows = [[int(value) if column == 1 and value.isdigit() else value for column, value in enumerate(row)] for row in rows]
                    spec = {'title': '校园资料整理', 'subtitle': '依据本次上传资料',
                        'sections': [{'heading': '资料清单', 'paragraphs': ['保留原始页数和截止时间；来源未提供的时间标为未知。'],
                                      'table': {'columns': columns, 'rows': rows}}],
                        'sources': [attachments[0]['name']],
                        'sheets': [{'name': '资料清单', 'columns': columns, 'rows': rows}]}
                choices = [('skill', {'name': 'document-artifacts'}),
                    ('read', {'file_path': str(ROOT / 'apps/agent-runtime/skills/document-artifacts/references/input-format.md')}),
                    ('read', {'file_path': attachments[0]['path']}),
                    ('write', {'file_path': stem + '.json', 'content': json.dumps(spec, ensure_ascii=False)}),
                    ('pwsh', {'command': '& $env:DSH_PYTHON_EXECUTABLE -X utf8 $env:DSH_DOCUMENT_EXPORTER --input "' + stem +
                        '.json" --output-dir "文档成果" --stem "' + stem + '" --formats pdf,xlsx,md',
                        'description': '生成实际中文 PDF、Excel 和 Markdown'}),
                    *[('publish_file', {'path': '文档成果/' + stem + '.' + fmt}) for fmt in ['pdf', 'xlsx', 'md']]] if attachments else []
                calls = []
                if len(actual_results) < len(choices):
                    name, args = choices[len(actual_results)]
                    calls = [{'id': 'document_flow_' + str(len(receipts)), 'type': 'function', 'function': {
                        'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': None if calls else
                    ('已根据上传资料生成 PDF、Excel 和 Markdown，成果可预览或下载。' if attachments else '请先选择资料清单 CSV 附件。'),
                    'tool_calls': calls}, 'finish_reason': 'tool_calls' if calls else 'stop'}]}, ensure_ascii=False).encode())
                return
            if user_text.startswith('[V2文件演练]'):
                context = json.loads(messages[last_user]['content'].splitlines()[1])
                attachments = context.get('attachments', [])
                actual_results = [m['content'] for m in messages[last_user + 1:] if m['role'] == 'tool']
                choices = [('read', {'file_path': attachments[0]['path']}),
                    ('write', {'file_path': '附件整理成果.md', 'content': '# 附件整理成果\n' + (actual_results[0] if actual_results else '')}),
                    ('publish_file', {'path': '附件整理成果.md'})] if attachments else []
                calls = []
                if len(actual_results) < len(choices):
                    name, args = choices[len(actual_results)]
                    calls = [{'id': 'file_flow_' + str(len(receipts)), 'type': 'function', 'function': {
                        'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': None if calls else
                    ('\n'.join(actual_results) if attachments else '请先上传并选择附件。'), 'tool_calls': calls},
                    'finish_reason': 'tool_calls' if calls else 'stop'}]}, ensure_ascii=False).encode())
                return
            if user_text.startswith('[V2工具演练]'):
                actual_results = [m['content'] for m in messages[last_user + 1:] if m['role'] == 'tool']
                choices = [
                    ('write', {'file_path': 'v2-browser-notes.md', 'content': '# 实际工具产物\n来源标记：v2-browser-731\n'}),
                    ('read', {'file_path': 'v2-browser-notes.md'}),
                    ('pwsh', {'command': '& $env:DSH_PYTHON_EXECUTABLE -c "print(6 * 7)"', 'description': '通过实际 Python 环境计算结果'}),
                ]
                if any(t['function']['name'] == 'mcp__calculator__multiply' for t in body.get('tools', [])):
                    choices.append(('mcp__calculator__multiply', {'a': 36, 'b': 7}))
                calls = []
                if len(actual_results) < len(choices):
                    name, args = choices[len(actual_results)]
                    calls = [{'id': 'v2_native_' + str(len(receipts)), 'type': 'function', 'function': {
                        'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant',
                    'content': None if calls else '\n'.join(actual_results), 'tool_calls': calls},
                    'finish_reason': 'tool_calls' if calls else 'stop'}]}, ensure_ascii=False).encode())
                return
            results = [json.loads(m["content"]) for m in messages[last_user + 1:] if m["role"] == "tool"]
            calls = []
            content = None
            context = json.loads(messages[last_user]["content"].splitlines()[1])
            user_text = messages[last_user]["content"].split("用户消息：\n", 1)[1]
            def call(name, arguments):
                return [{"id": "plan_call_" + str(len(receipts)), "type": "function", "function": {
                    "name": "campus_" + name.replace(".", "_"), "arguments": json.dumps(arguments, ensure_ascii=False)}}]
            if "我完成了准备实验报告材料" in user_text:
                if not results:
                    calls = call("tasks.list", {"course_id": "course_synthetic"})
                elif len(results) == 1:
                    task = next(t for t in results[0]["data"]["items"] if t["title"] == "准备实验报告材料")
                    calls = call("tasks.feedback", {"task_id": task["id"], "expected_revision": task["revision"],
                                                  "outcome": "completed", "actual_minutes": 30})
                else:
                    result = results[-1]
                    content = "本地学习结果：" + str(result["data"].get("task", {}).get("status", result.get("error")))
            elif "生成学习任务" in user_text:
                if not results:
                    calls = call("tasks.build", {"course_id": "course_synthetic"})
                elif len(results) == 1 and results[0]["data"].get("job"):
                    jid = results[0]["data"]["job"]["id"]
                    calls = call("jobs.cancel" if "并停止" in user_text else "jobs.read", {"job_id": jid})
                else:
                    result = results[-1]
                    job = result["data"].get("job", result["data"])
                    content = "后台任务实际状态：" + str(job.get("status", result.get("error")))
            elif context.get("accepted_action"):
                accepted = context["accepted_action"]
                if not results:
                    calls = call(accepted["tool"], {"action_id": accepted["action_id"]})
                else:
                    result = results[-1]
                    content = ("操作回执：" + result["data"]["receipt"]["plan"]["status"] if result.get("status") == "ok"
                               else "操作未完成：" + result["error"]["message"])
            elif "安排" in user_text and "取消演练" not in user_text:
                if not results:
                    calls = call("availability.read", {})
                elif not any("action" in r["data"] for r in results) and len(results) == 1 and results[0]["status"] == "ok":
                    args = {"period": "tomorrow" if "明天" in user_text else "this_week"}
                    weekdays = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7}
                    for label, number in weekdays.items():
                        if f"本周{label}晚上没空" in user_text:
                            args["unavailable_evenings"] = [number]
                    if "19:00–21:00" in user_text:
                        day = (datetime.fromisoformat(context["now"]) + timedelta(days=1)).date()
                        args["blocked"] = [{"starts_at": f"{day}T19:00:00+08:00", "ends_at": f"{day}T21:00:00+08:00"}]
                    calls = call("plans.preview", args)
                else:
                    result = results[-1]
                    if result["status"] == "ok" and "action" in result["data"]:
                        plan = result["data"]["action"]["preview"]["plan"]
                        content = f"已生成预览：{len(plan['blocks'])} 段安排，{len(plan['unassigned'])} 项未分配。请查看方案差异后接受。"
                    else:
                        content = "预览未完成：" + str(result.get("error"))
            elif not results:
                calls = [{"id": "assignment_call_" + str(len(receipts)), "type": "function", "function": {
                            "name": "campus_assignments_list", "arguments": json.dumps({"course_id": "course_synthetic"})}},
                         {"id": "search_call_" + str(len(receipts)), "type": "function", "function": {
                            "name": "campus_materials_search", "arguments": json.dumps({"course_id": "course_synthetic", "query": "实验报告"}, ensure_ascii=False)}}]
            else:
                found = next((r for r in results if r["data"].get("items") and "chunk_id" in r["data"]["items"][0]), None)
                read = next((r for r in results if "resource" in r["data"]), None)
                if found and not read:
                    hit = found["data"]["items"][0]
                    calls = [{"id": "read_call_" + str(len(receipts)), "type": "function", "function": {
                        "name": "campus_materials_read", "arguments": json.dumps({"resource_id": hit["resource_id"], "expected_version": hit["resource_version"], "chunk_ids": [hit["chunk_id"]]})}}]
                elif read:
                    assignments = next(r for r in results if r["data"].get("items") and "due_precision" in r["data"]["items"][0])
                    row = assignments["data"]["items"][0]
                    chunk = read["data"]["items"][0]
                    content = row["title"] + "；截止精度：" + row["due_precision"] + "；原文：" + chunk["text"] + "；来源：" + read["evidence"][0]["source_id"]
                else:
                    content = "没有可读取的正文依据。"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"role": "assistant", "content": content,
                    "tool_calls": calls}, "finish_reason": "tool_calls" if calls else "stop"}]}, ensure_ascii=False).encode())

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    port = free_port()
    env = os.environ.copy()
    env.update(STUDY_DATABASE_URL="sqlite:///" + (run_dir / "domain.sqlite").as_posix(),
               STUDY_FILES_DIR=str(run_dir / "files"), STUDY_SECRETS_DIR=str(run_dir / "secrets"), CAMPUS_AGENT_HOME=str(run_dir / "agent"),
               CAMPUS_BRIDGE_URL=f"http://127.0.0.1:{port}", PYTHONUTF8="1", STUDY_WORKER_INPROC="0")
    env['CAMPUS_WORKSPACE_DIR'] = str(run_dir / 'workspace')
    env["CAMPUS_WEB_ORIGINS"] = f"http://127.0.0.1:{web_port},http://localhost:{web_port}"
    # Seed in a separate process: config.py reads paths only once, before import.
    seed = """
from app import models as m
from app.db import Base, engine, SessionLocal
Base.metadata.create_all(engine)
with SessionLocal() as db:
    db.add(m.Course(id='course_synthetic', title='合成实验课程', term='2026秋'))
    db.add(m.ProviderConfig(id='provider_synthetic', name='受控测试模型', base_url=__PROVIDER_URL__, model='controlled-test'))
    db.flush()
    db.add(m.Assignment(id='assignment_synthetic', course_id='course_synthetic', title='合成实验报告', due_precision='unknown', revision=1))
    db.add(m.LearningTask(id='task_synthetic', course_id='course_synthetic', title='准备实验报告材料', type='reading', estimated_minutes=120, due_precision='unknown', status='pending'))
    db.add(m.Availability(id=1, timezone='Asia/Shanghai', weekly_windows=[{'weekday':d, 'start':'18:00', 'end':'22:00'} for d in range(1,8)], date_exceptions=[], daily_limit_minutes=120))
    db.add(m.Resource(id='resource_synthetic', course_id='course_synthetic', kind='document', title='合成实验要求', version=2, parse_status='ready'))
    db.flush()
    db.add(m.ContentChunk(id='chunk_synthetic', resource_id='resource_synthetic', resource_version=2, ordinal=0, text='实验报告需要方法说明与原始数据附件。', locator={'paragraph':1}, provenance={'kind':'synthetic'}))
    db.commit()
""".replace("__PROVIDER_URL__", repr(f"http://127.0.0.1:{provider.server_port}/v1"))
    subprocess.run([sys.executable, "-c", seed], cwd=API, env=env, check=True)
    log = (run_dir / "api.log").open("w", encoding="utf-8")
    api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                           cwd=API, env=env, stdout=log, stderr=log,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    base = f"http://127.0.0.1:{port}"
    client = requests.Session()
    evidence = {"kind": "synthetic-model-real-harness-real-python", "runtime_version": "0.2.0-rc.2", "turns": []}
    web = None

    def post(path, payload, headers=None):
        response = client.post(base + "/api/v1/agent" + path, json=payload, headers=headers, timeout=15)
        assert response.ok, (response.status_code, response.text)
        return response.json()

    def wait_turn(tid):
        deadline, items, after = time.monotonic() + 100, [], 0
        while time.monotonic() < deadline:
            response = client.get(base + "/api/v1/agent/turns/" + tid, params={"after": after}, timeout=10)
            assert response.ok, response.text
            body = response.json()
            events = body.pop("events")
            items.extend(events["items"])
            after = events["next_cursor"]
            if body["status"] not in {"queued", "running", "stopping"}:
                body["events"] = items
                return body
            time.sleep(0.2)
        raise AssertionError("integration turn timed out")

    try:
        for _ in range(100):
            if api.poll() is not None:
                raise AssertionError("API exited before ready")
            try:
                if client.get(base + "/api/v1/health", timeout=1).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.1)
        if serve:
            env.update(VITE_API_MODE="real", VITE_PROXY_TARGET=base, VITE_API_BASE_URL="", VITE_AGENT_TEST_DATA="1")
            web = subprocess.Popen([shutil.which("node"), str(ROOT / "apps/web/node_modules/vite/bin/vite.js"),
                                    "--port", str(web_port), "--strictPort"], cwd=ROOT / "apps/web", env=env,
                                    stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            metadata = {"url": f"http://127.0.0.1:{web_port}", "api_url": base, "run_dir": str(run_dir),
                        "api_pid": api.pid, "web_pid": web.pid, "synthetic": True}
            (run_dir / "demo.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            print(json.dumps(metadata), flush=True)
            while not (stop_file and stop_file.exists()):
                if api.poll() is not None or web.poll() is not None:
                    raise AssertionError("owned demo service exited")
                time.sleep(0.25)
            return
        cid = post("/conversations", {"provider_id": "provider_synthetic", "send_to_provider": True, "course_id": "course_synthetic"})["id"]
        for ordinal, text in enumerate(["查实验作业截止以及报告所需材料", "继续上一条，再核对原文依据"]):
            result = post(f"/conversations/{cid}/turns", {"text": text}, {"Idempotency-Key": f"integration-{ordinal}"})
            duplicate = post(f"/conversations/{cid}/turns", {"text": text}, {"Idempotency-Key": f"integration-{ordinal}"})
            assert duplicate["id"] == result["id"]
            completed = wait_turn(result["id"])
            evidence["turns"].append(completed)
            assert completed["status"] == "succeeded", completed
            assert "原始数据附件" in completed["response"] and "unknown" in completed["response"]
            assert "chunk_synthetic" in completed["response"]
            tool_events = [e for e in completed["events"] if e["kind"] == "tool_result"]
            assert {e["data"]["name"] for e in tool_events} == {"assignments.list", "materials.search", "materials.read"}
            assert len(tool_events) == 3
        history = client.get(base + "/api/v1/agent/conversations/" + cid, timeout=10).json()
        assert len(history["turns"]) == 2 and history["current_course_id"] == "course_synthetic"
        assert sum(m["role"] == "user" for m in receipts[-1]["messages"]) >= 2, "official persisted session did not resume"
        expected_tools = {"campus_connections_status", "campus_courses_list", "campus_materials_list", "campus_materials_search", "campus_materials_read", "campus_assignments_list", "campus_tasks_list", "campus_plans_read", "campus_jobs_read", "campus_availability_read", "campus_plans_preview", "campus_plans_apply", "campus_plans_revoke", "campus_tasks_build", "campus_jobs_cancel", "campus_tasks_feedback"}
        expected_tools |= {'campus_campus_' + name for name in ('status', 'query', 'sync', 'academics', 'notice_read',
            'imported_read', 'webvpn_entry', 'notice_import')}
        native_tools = {'load_tools', 'create_goal', 'edit', 'exit_plan_mode', 'get_goal', 'glob', 'grep', 'interrupt_agent',
                        'job_kill', 'job_list', 'job_output', 'list_agents', 'pwsh' if os.name == 'nt' else 'bash',
                        'read', 'read_image', 'send_message', 'skill', 'subagent', 'subagent_fork', 'todo_write',
                        'update_goal', 'web_fetch', 'web_search', 'workflow', 'write', 'publish_file'}
        actual_tools = {s['function']['name'] for s in receipts[-1]['tools']}
        browser_names = {'browser_close', 'browser_resize', 'browser_console_messages', 'browser_handle_dialog', 'browser_emulate_media',
            'browser_evaluate', 'browser_file_upload', 'browser_drop', 'browser_find', 'browser_fill_form', 'browser_press_key',
            'browser_type', 'browser_navigate', 'browser_navigate_back', 'browser_network_requests', 'browser_network_request',
            'browser_run_code_unsafe', 'browser_take_screenshot', 'browser_snapshot', 'browser_click', 'browser_drag',
            'browser_hover', 'browser_select_option', 'browser_tabs', 'browser_wait_for', 'browser_prepare_action'}
        browser_tools = {'mcp__campus_browser__' + name for name in browser_names}
        mcp_resources = {'list_mcp_resources', 'list_mcp_resource_templates', 'read_mcp_resource'}
        scheduling_tools = {'schedule_create', 'schedule_list', 'schedule_update', 'schedule_delete', 'schedule_history'}
        assert actual_tools == expected_tools | native_tools | browser_tools | mcp_resources | scheduling_tools, actual_tools
        cancel_turn = post(f"/conversations/{cid}/turns", {"text": "取消演练"}, {"Idempotency-Key": "integration-cancel"})
        assert slow_started.wait(timeout=15), "controlled provider was not reached before cancellation"
        cancelled = post("/turns/" + cancel_turn["id"] + "/cancel", {})
        assert cancelled["status"] in ('stopping', 'cancelled')
        if cancelled['status'] == 'stopping':
            held = client.post(base + f'/api/v1/agent/conversations/{cid}/turns', json={'text': '等待物理停止'},
                headers={'Idempotency-Key': 'while-stopping'}, timeout=10)
            assert held.status_code == 409 and held.json()['error']['code'] == 'TURN_IN_PROGRESS'
        assert wait_turn(cancel_turn['id'])['status'] == 'cancelled'
        # Start after observed teardown, not a guessed sleep interval.
        resumed = post(f"/conversations/{cid}/turns", {"text": "取消后继续核对原文"}, {"Idempotency-Key": "integration-after-cancel"})
        completed = wait_turn(resumed["id"])
        evidence["after_cancel_turn"] = completed
        assert completed["status"] == "succeeded" and "原始数据附件" in completed["response"], completed
        cancelled = wait_turn(cancel_turn["id"])
        assert cancelled["status"] == "cancelled"
        assert not [e for e in cancelled["events"] if e["kind"] == "tool_result"], "cancelled model response caused new tool execution"
        evidence["cancelled_turn"] = cancelled
        unsafe = post(f'/conversations/{cid}/turns', {'text': '[V2停止演练] 执行浏览器脚本并允许停止'}, {'Idempotency-Key': 'stop-browser-script'})
        assert stopped_script_started.wait(15), 'Actual browser code did not start'
        began_stop = time.monotonic()
        stopping = post('/turns/' + unsafe['id'] + '/cancel', {})
        assert stopping['status'] in ('stopping', 'cancelled')
        stopped = wait_turn(unsafe['id'])
        assert stopped['status'] == 'cancelled'
        assert time.monotonic() - began_stop < 10, 'Root cancellation waited for the delayed browser operation'
        assert not stopped_script_late.is_set()
        evidence['browser_script_cancelled_turn'] = stopped
        evidence['browser_script_cancel_checks'] = {'actual_script_started': True, 'stopping_until_teardown': True,
            'actual_browser_and_sdk_owned_processes_stopped': True, 'delayed_operation_did_not_execute': True}
        # Restart the actual API, keep the client cookie and both journals, then
        # resume again. No progress file or simulated memory substitutes for this.
        api.terminate()
        api.wait(timeout=20)
        api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                               cwd=API, env=env, stdout=log, stderr=log,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        for _ in range(100):
            try:
                if client.get(base + "/api/v1/health", timeout=1).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.1)
        recovered = client.get(base + "/api/v1/agent/conversations/" + cid, timeout=10).json()
        assert len(recovered["turns"]) == 5 and recovered["current_course_id"] == "course_synthetic"
        restarted_turn = post(f"/conversations/{cid}/turns", {"text": "重启后继续核对材料"}, {"Idempotency-Key": "integration-after-restart"})
        completed = wait_turn(restarted_turn["id"])
        evidence["after_api_restart_turn"] = completed
        assert completed["status"] == "succeeded" and "原始数据附件" in completed["response"], completed
        assert sum(m["role"] == "user" for m in receipts[-1]["messages"]) >= 5
        trusted_context = json.loads(next(m["content"] for m in receipts[0]["messages"] if m["role"] == "user").splitlines()[1])
        assert trusted_context["timezone"] == "Asia/Shanghai" and trusted_context["now"].endswith("+08:00")
        # M2 uses the same conversation and the real Harness for every write.
        def plan_turn(text, key, action_id=None):
            payload = {"text": text}
            if action_id:
                payload["action_id"] = action_id
            queued = post(f"/conversations/{cid}/turns", payload, {"Idempotency-Key": key})
            assert post(f"/conversations/{cid}/turns", payload, {"Idempotency-Key": key})["id"] == queued["id"]
            done = wait_turn(queued["id"])
            evidence.setdefault("plan_turns", []).append(done)
            assert done["status"] == "succeeded", done
            results = [e["data"] for e in done["events"] if e["kind"] == "tool_result"]
            assert results and all(e["result"]["status"] == "ok" for e in results), results
            return results

        original_availability = client.get(base + "/api/v1/preferences/availability", timeout=10).json()
        def preview_and_apply(text, key):
            results = plan_turn(text, key)
            assert [e["name"] for e in results] == ["availability.read", "plans.preview"]
            action = results[-1]["result"]["data"]["action"]
            assert action["preview"]["plan"]["blocks"]
            assert action["preview"]["task_titles"]["task_synthetic"] == "准备实验报告材料"
            accepted = post(f"/conversations/{cid}/actions/{action['id']}/accept", {"operation": "apply"}, {"Idempotency-Key": key + "-accept"})
            applied = plan_turn("按这个来", key + "-apply", accepted["id"])
            assert [e["name"] for e in applied] == ["plans.apply"]
            assert applied[0]["result"]["data"]["receipt"]["plan"]["status"] == "active"
            current = client.get(base + "/api/v1/plans/current", timeout=10).json()["plan"]
            assert current["id"] == action["plan_id"]
            return action

        # Relative weekday comes from a semantic tool argument; Python resolves
        # the actual date and exclusion interval, which remains in the preview.
        current_weekday = datetime.fromisoformat(trusted_context["now"]).isoweekday()
        evening_label = {1:"一", 2:"二", 3:"三", 4:"四", 5:"五", 6:"六", 7:"日"}[min(current_weekday + 1, 7)]
        weekly = preview_and_apply(f"安排这周，本周{evening_label}晚上没空", "plan-weekly")
        assert weekly["preview"]["constraints"]["blocked"]
        first = preview_and_apply("把剩余任务安排到明天", "plan-first")
        second = preview_and_apply("把剩余任务安排到明天，19:00–21:00没空", "plan-second")
        for block in second["preview"]["plan"]["blocks"]:
            start = datetime.fromisoformat(block["starts_at"].replace("Z", "+00:00")).astimezone(datetime.fromisoformat(trusted_context["now"]).tzinfo)
            end = datetime.fromisoformat(block["ends_at"].replace("Z", "+00:00")).astimezone(start.tzinfo)
            assert end.hour <= 19 or start.hour >= 21
        assert client.get(base + "/api/v1/preferences/availability", timeout=10).json() == original_availability
        undo = post(f"/conversations/{cid}/actions/{second['id']}/accept", {"operation": "revoke"}, {"Idempotency-Key": "undo-second"})
        undone = plan_turn("撤销刚才安排", "plan-undo", undo["id"])
        assert [e["name"] for e in undone] == ["plans.revoke"]
        assert undone[0]["result"]["data"]["receipt"]["restored_plan_id"] == first["plan_id"]
        assert client.get(base + "/api/v1/plans/current", timeout=10).json()["plan"]["id"] == first["plan_id"]
        replay = plan_turn("重复请求只读取刚才的回执", "plan-undo-replay", undo["id"])
        assert replay[0]["result"]["data"]["receipt"] == undone[0]["result"]["data"]["receipt"]
        evidence["plan_checks"] = {"actual_harness_apply_revoke": True, "same_conversation": True,
            "temporary_constraints_preserve_preferences": True, "previous_plan_restored": True, "durable_replay": True}
        def job_turn(text, key):
            queued = post(f"/conversations/{cid}/turns", {"text": text}, {"Idempotency-Key": key})
            done = wait_turn(queued["id"])
            evidence.setdefault("job_turns", []).append(done)
            assert done["status"] == "succeeded", done
            results = [e["data"] for e in done["events"] if e["kind"] == "tool_result"]
            assert all(e["result"]["error"] is None for e in results), results
            return results
        results = job_turn("为合成实验课程生成学习任务", "job-build")
        assert [e["name"] for e in results] == ["tasks.build", "jobs.read"]
        jid = results[0]["result"]["job_id"]
        assert results[-1]["result"]["data"]["status"] == "queued"
        # Execute the actual registered worker once in the isolated business DB.
        subprocess.run([sys.executable, "-c", "from app.worker import process_next_job; assert process_next_job()"],
                       cwd=API, env=env, check=True, timeout=30, stdout=log, stderr=log)
        completed_job = client.get(base + "/api/v1/jobs/" + jid, timeout=10).json()
        assert completed_job["status"] == "succeeded" and completed_job["result_refs"], completed_job
        results = job_turn("为合成实验课程生成学习任务并停止", "job-cancel")
        assert [e["name"] for e in results] == ["tasks.build", "jobs.cancel"]
        assert results[-1]["result"]["data"]["job"]["status"] == "cancelled"
        evidence["job_checks"] = {"real_harness_build_read_cancel": True, "actual_worker_completed": True,
                                  "queued_not_claimed_complete": True}
        before_assignment = client.get(base + "/api/v1/assignments/assignment_synthetic", timeout=10).json()
        results = job_turn("我完成了准备实验报告材料，用了30分钟", "feedback-completed")
        assert [e["name"] for e in results] == ["tasks.list", "tasks.feedback"]
        assert results[-1]["result"]["data"]["task"]["status"] == "completed"
        assert results[-1]["result"]["data"]["feedback"]["actual_minutes"] == 30
        after_assignment = client.get(base + "/api/v1/assignments/assignment_synthetic", timeout=10).json()
        assert before_assignment["submission_status"] == after_assignment["submission_status"]
        evidence["feedback_checks"] = {"real_harness_read_then_feedback": True, "school_submission_unchanged": True}
        configured = client.put(base + '/api/v1/agent/settings', json={'mcp_servers': [{
            'serverName': 'calculator', 'command': sys.executable,
            'args': ['-u', str(ROOT / 'tools/fixtures/mcp_calculator.py'), str(run_dir / 'mcp-service.jsonl')],
        }]}, timeout=10)
        assert configured.ok, configured.text
        diagnostic = post('/settings/mcp/calculator/test', {})
        assert diagnostic['ok'] and diagnostic['tool_names'] == ['multiply'], diagnostic
        generic = post(f'/conversations/{cid}/turns', {'text': '[V2工具演练] 文件、Python与已配置MCP实际执行'}, {'Idempotency-Key': 'v2-generic'})
        done = wait_turn(generic['id'])
        assert done['status'] == 'succeeded', done
        native_results = [e['data']['event']['data']['message'] for e in done['events']
            if e['kind'] == 'harness_event' and e['data']['event']['type'] == 'tool/result']
        assert len(native_results) == 5 and all(not r['isError'] for r in native_results), native_results
        assert '42' in done['response'] and 'MCP computed: 252' in done['response']
        assert (run_dir / 'workspace/v2-browser-notes.md').read_text(encoding='utf-8') == '# 实际工具产物\n来源标记：v2-browser-731\n'
        evidence['generic_tools_turn'] = done
        evidence['generic_checks'] = {'settings_api_to_actual_runtime': True, 'actual_file_and_python': True,
            'actual_local_mcp': True, 'all_four_tool_results_succeeded': True}
        uploaded = client.post(base + f'/api/v1/agent/conversations/{cid}/files',
            files={'file': ('上传资料.md', '# 实际上传附件\nuploaded-source-marker-619\n'.encode())},
            headers={'Idempotency-Key': 'actual-upload-one'}, timeout=10)
        assert uploaded.status_code == 201, uploaded.text
        attached = post(f'/conversations/{cid}/turns', {'text': '[V2文件演练] 读取附件并生成可下载成果',
            'attachment_ids': [uploaded.json()['id']]}, {'Idempotency-Key': 'actual-attached-flow'})
        file_done = wait_turn(attached['id'])
        assert file_done['status'] == 'succeeded', file_done
        file_results = [e['data']['event']['data']['message'] for e in file_done['events']
            if e['kind'] == 'harness_event' and e['data']['event']['type'] == 'tool/result']
        assert len(file_results) == 4 and all(not r['isError'] for r in file_results), file_results
        published = [e['data']['file'] for e in file_done['events'] if e['kind'] == 'file.published']
        assert len(published) == 1 and published[0]['kind'] == 'output', file_done
        downloaded = client.get(base + f"/api/v1/agent/conversations/{cid}/files/{published[0]['id']}/content", timeout=10)
        assert downloaded.ok and 'uploaded-source-marker-619' in downloaded.content.decode()
        evidence['file_workspace_turn'] = file_done
        evidence['file_checks'] = {'actual_multipart_upload': True, 'selected_attachment_in_sdk_context': True,
            'actual_read_write_publish': True, 'downloadable_output_contains_source': True}
        csv_source = '资料,页数,截止时间\n原始记录-619,0,未知\n实验讲义,12,2026-10-20\n'
        uploaded_doc = client.post(base + f'/api/v1/agent/conversations/{cid}/files',
            files={'file': ('资料清单.csv', csv_source.encode())}, headers={'Idempotency-Key': 'document-source'}, timeout=10)
        assert uploaded_doc.status_code == 201, uploaded_doc.text
        document_turn = post(f'/conversations/{cid}/turns', {'text': '[V2文档演练] 根据资料清单生成PDF和Excel及Markdown',
            'attachment_ids': [uploaded_doc.json()['id']]}, {'Idempotency-Key': 'actual-documents-flow'})
        document_done = wait_turn(document_turn['id'])
        assert document_done['status'] == 'succeeded', document_done
        document_results = [e['data']['event']['data']['message'] for e in document_done['events']
            if e['kind'] == 'harness_event' and e['data']['event']['type'] == 'tool/result']
        assert len(document_results) == 9 and all(not result['isError'] for result in document_results), document_results
        document_files = [e['data']['file'] for e in document_done['events'] if e['kind'] == 'file.published']
        assert len(document_files) == 3 and {Path(f['name']).suffix for f in document_files} == {'.pdf', '.xlsx', '.md'}
        from pypdf import PdfReader
        from openpyxl import load_workbook
        for item in document_files:
            content = client.get(base + f"/api/v1/agent/conversations/{cid}/files/{item['id']}/content", timeout=10)
            assert content.ok, content.text
            saved = run_dir / item['name']
            saved.write_bytes(content.content)
            if saved.suffix == '.pdf':
                assert '原始记录-619' in ''.join(page.extract_text() for page in PdfReader(saved).pages)
            elif saved.suffix == '.xlsx':
                book = load_workbook(saved)
                assert book['资料清单']['A5'].value == '原始记录-619' and book['资料清单']['B5'].value == 0
                assert book['资料清单']['B6'].value == 12
                book.close()
                preview = client.get(base + f"/api/v1/agent/conversations/{cid}/files/{item['id']}/preview", timeout=10).json()
                assert preview['kind'] == 'spreadsheet' and preview['sheets'][0]['rows'][4][1] == 0
            else:
                assert '原始记录-619' in saved.read_text(encoding='utf-8')
        evidence['document_turn'] = document_done
        evidence['document_files'] = document_files
        evidence['document_checks'] = {'actual_skill_and_reference_read': True, 'actual_uploaded_csv_read': True,
            'actual_python_pdf_xlsx_md_generation': True, 'all_eight_tools_succeeded': True,
            'three_published_downloads': True, 'pdf_text_and_xlsx_numeric_values': True}
        parallel = post(f'/conversations/{cid}/turns', {'text': '[V2子任务演练] 同时计算A与阅读B，允许单独停止A'}, {'Idempotency-Key': 'v2-parallel-controls'})
        assert child_b_waiting.wait(timeout=20), 'Both actual children did not reach their independent work'
        view = client.get(base + '/api/v1/agent/turns/' + parallel['id'], timeout=10).json()
        a_id = next(e['data']['session_id'] for e in view['events']['items'] if e['kind'] == 'harness_event' and
                    e['data']['event']['type'] == 'tool/call' and e['data']['event']['data']['name'] == 'pwsh')
        stopped = post(f"/turns/{parallel['id']}/subagents/{a_id}/interrupt", {})
        assert stopped['accepted']
        for _ in range(100):
            view = client.get(base + '/api/v1/agent/turns/' + parallel['id'], timeout=10).json()
            ended = [e['data'] for e in view['events']['items'] if e['kind'] == 'subagent.finished']
            if any(e['childSessionId'] == a_id and e['stopReason'] == 'aborted' for e in ended):
                break
            time.sleep(0.05)
        else:
            raise AssertionError('Actual child A did not stop')
        assert view['status'] == 'running' and len(ended) == 1, 'Stopping A incorrectly stopped B or the parent'
        replay_stop = post(f"/turns/{parallel['id']}/subagents/{a_id}/interrupt", {})
        assert replay_stop['accepted'] and replay_stop['already_finished']
        release_child_b.set()
        done = wait_turn(parallel['id'])
        assert done['status'] == 'succeeded' and 'v2-browser-731' in done['response'], done
        assert 'SHOULD_NOT_COMPLETE' not in done['response']
        finished = [e['data'] for e in done['events'] if e['kind'] == 'subagent.finished']
        assert len(finished) == 2 and any(e['status'] == 'ok' for e in finished)
        evidence['parallel_children_turn'] = done
        evidence['parallel_checks'] = {'actual_api_selective_child_cancel': True, 'sibling_and_parent_continue': True,
            'stop_replay_is_idempotent': True, 'actual_sibling_file_result': True}
        persistent = post(f'/conversations/{cid}/turns', {'text': '[V2持续目标演练] 持续整理资料，完成后汇报'}, {'Idempotency-Key': 'v2-native-goal'})
        persistent_done = wait_turn(persistent['id'])
        assert persistent_done['status'] == 'succeeded' and '已完成持续目标' in persistent_done['response'], persistent_done
        assert '真实原生 Goal 续行结果-673' in (run_dir / 'workspace/goal-673.md').read_text(encoding='utf-8')
        goal_states = [e['data'] for e in persistent_done['events'] if e['kind'] == 'goal.state']
        assert any(s['goal']['phase'] == 'active' for s in goal_states)
        assert goal_states[-1]['goal']['phase'] == 'complete' and goal_states[-1]['goal']['roundsStarted'] == 1
        assert goal_states[-1]['agent_status'] == 'idle'
        goal_messages = [e['data']['event'] for e in persistent_done['events'] if e['kind'] == 'harness_event' and
                         e['data']['event']['type'] == 'assistant/message']
        assert len(goal_messages) >= 2
        evidence['persistent_goal_turn'] = persistent_done
        evidence['persistent_goal_checks'] = {'actual_native_round_driver': True, 'global_subscription_after_initial_idle': True,
            'actual_file_written_in_goal_round': True, 'latest_final_response_not_initial_progress': True}
        browser_turn = post(f'/conversations/{cid}/turns', {'text': '[V2浏览器演练] 读取页面、下载资料、填写测试材料，提交前给我确认'}, {'Idempotency-Key': 'v2-browser-flow'})
        browser_done = wait_turn(browser_turn['id'])
        assert browser_done['status'] == 'succeeded', browser_done
        browser_results = [e['data']['event'] for e in browser_done['events'] if e['kind'] == 'harness_event' and
                           e['data']['event']['type'] == 'tool/result']
        assert len(browser_results) == 5 and all(not e['data']['message'].get('isError') for e in browser_results), browser_results
        assert '校园材料-632' in browser_done['response'] and 'needs_user' in browser_done['response']
        status = client.get(base + f'/api/v1/agent/conversations/{cid}/browser', timeout=15).json()
        pending = [a for a in status['reviews'] if a['status'] == 'pending']
        assert len(pending) == 1 and browser_submissions == []
        assert pending[0]['preview']['action_url'] == f'http://127.0.0.1:{provider.server_port}/browser/submit'
        action_url = f'/conversations/{cid}/browser/actions/{pending[0]["id"]}/confirm'
        assert post(action_url, {})['action']['status'] == 'executed'
        assert post(action_url, {})['action']['status'] == 'executed'
        assert browser_submissions == ['浏览器学生-724']
        downloads = [f for f in client.get(base + f'/api/v1/agent/conversations/{cid}/files', timeout=10).json()['items'] if f['name'] == 'browser-campus.txt']
        assert len(downloads) == 1
        assert client.get(base + f'/api/v1/agent/conversations/{cid}/files/{downloads[0]["id"]}/content', timeout=10).content == b'actual-browser-attachment-918\n'
        assert any(e['kind'] == 'file.published' and e['data']['file']['id'] == downloads[0]['id'] for e in browser_done['events'])
        followup = post(f'/conversations/{cid}/turns', {'text': '[V2浏览器回执] 我已在工作区确认，继续核对执行状态'}, {'Idempotency-Key': 'v2-browser-receipt-context'})
        followup_done = wait_turn(followup['id'])
        assert followup_done['status'] == 'succeeded' and 'executed' in followup_done['response'], followup_done
        latest_context = json.loads(next(m['content'] for m in reversed(receipts[-1]['messages']) if m['role'] == 'user' and '用户消息：\n' in m['content']).splitlines()[1])
        assert any(a['id'] == pending[0]['id'] and a['status'] == 'executed' for a in latest_context['browser_actions'])
        evidence['browser_receipt_followup_turn'] = followup_done
        evidence['browser_turn'] = browser_done
        evidence['browser_checks'] = {'actual_mcp_discovery_and_four_successful_tools': True,
            'actual_attachment_published_and_downloaded': True, 'form_filled_before_one_confirmation': True,
            'duplicate_confirmation_did_not_resubmit': True, 'next_turn_knows_actual_external_action_receipt': True}
        actual_tools = {s['function']['name'] for s in receipts[-1]['tools']}
        assert actual_tools == expected_tools | native_tools | browser_tools | mcp_resources | scheduling_tools | {'mcp__calculator__multiply'}
        evidence.update(model_call_count=len(receipts), model_visible_tools=sorted(actual_tools), second_turn_preserved_history=True)
        print(json.dumps({"ok": True, "evidence": str(run_dir / "evidence.json"), "model_calls": len(receipts)}, ensure_ascii=False))
    finally:
        release_child_b.set()
        evidence['stop_fixture'] = {'started': stopped_script_started.is_set(), 'late': stopped_script_late.is_set()}
        (run_dir / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        (run_dir / "model-receipts.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2), encoding="utf-8")
        client.close()
        if web:
            web.terminate()
            web.wait(timeout=10)
        api.terminate()
        try:
            api.wait(timeout=20)
        except subprocess.TimeoutExpired:
            api.kill()
            api.wait(timeout=5)
        log.close()
        provider.shutdown()
        provider.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help="Keep synthetic API/model/web services for browser QA")
    parser.add_argument("--web-port", type=int, default=15174)
    parser.add_argument("--stop-file", type=Path)
    args = parser.parse_args()
    verify(serve=args.serve, web_port=args.web_port, stop_file=args.stop_file)
