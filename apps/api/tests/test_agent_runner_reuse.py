"""The owned runtime process is reused across a conversation's turns.

Rebuilding the whole profile (and every MCP child) before each turn made every turn
after the first pay a multi-second startup; see docs/agent-v2-performance.md. These
tests pin the reuse decision itself: same conversation + same profile = same process,
and every case that must replace the process really does.
"""
from __future__ import annotations

import io
import json
import time

import pytest

from app.agent_bridge import runtime as runtime_module
from app.agent_bridge.response_grounding import assignment_deadline_summary, assignment_top_n_summary
from app.agent_bridge.store import Store


class FakeStore:
    def __init__(self):
        self.bound = []
        self.grants = {}
        self.turns = {}
        self.failures = {}
        self.finished = []
        self.recorded = []
        self.limits = type('Limits', (), {'max_output_tokens': 16384, 'turn_seconds': 1800})()

    def bind_token(self, tid, token):
        self.bound.append((tid, token))
        return True

    def turn(self, tid):
        return self.turns.get(tid, {'id': tid, 'conversation_id': 'conv_reuse', 'status': 'running'})

    def running(self, tid):
        return self.turn(tid)['status'] == 'running'

    def finish(self, tid, status, *, response='', error=None):
        self.finished.append((tid, status, response, error))
        if tid in self.turns:
            self.turns[tid]['status'] = status

    def event(self, tid, kind, data):
        self.recorded.append((tid, kind, data))

    def last_failure(self, tid):
        return self.failures.get(tid)

    def conversation(self, cid, owner=None):
        return {'id': cid, 'settings': {'provider': {'id': 'provider_a'}, 'course_ids': []}}

    def grant(self, cid):
        token = 'grant-' + cid
        self.grants[cid] = token
        return token

    def granted(self, token):
        for cid, value in self.grants.items():
            if value == token:
                return self.conversation(cid)
        return None

    def revoke_grant(self, cid):
        self.grants.pop(cid, None)

    def events(self, tid, after=0):
        items = [{'seq': seq, 'kind': kind, 'data': data} for seq, (event_tid, kind, data)
                 in enumerate(self.recorded, 1) if event_tid == tid and seq > after]
        return {'items': items, 'next_cursor': items[-1]['seq'] if items else after}


class FakeProcess:
    def __init__(self, pid=4242, stdout=''):
        self.pid = pid
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(stdout)
        self.campus_owner = None
        self.alive = True

    def poll(self):
        return None if self.alive else 1

    def wait(self, timeout=None):
        self.alive = False
        return 0

    def terminate(self):
        self.alive = False


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    instance = runtime_module.Runtime(Store(tmp_path / 'journal.sqlite'), tmp_path / 'harness',
                                      'http://127.0.0.1:8931')
    instance.store = FakeStore()
    spawned = []

    def spawn(*args, **kwargs):
        spawned.append(FakeProcess(pid=5000 + len(spawned)))
        return spawned[-1]

    monkeypatch.setattr(runtime_module, 'spawn_owned', spawn)
    instance.spawned = spawned
    return instance


def acquire(instance, tid, cid='conv_reuse', signature='sig-a', token='tok-one', resume=None):
    return instance._acquire_runner(cid, tid, {'CAMPUS_BRIDGE_TOKEN': token}, signature, resume)


def test_second_turn_reuses_the_same_process_and_rebinds_its_token(runtime):
    first, spawned_first = acquire(runtime, 'turn_1')
    assert spawned_first and first['token'] == 'tok-one'

    second, spawned_again = acquire(runtime, 'turn_2')
    assert spawned_again is False, 'the second turn must not start another runner'
    assert second is first
    assert len(runtime.spawned) == 1
    # The warm process answers to the token it was started with, so this turn is bound to it.
    assert runtime.store.bound == [('turn_2', 'tok-one')]
    assert first['turns'] == 0

    runtime._retain_runner('conv_reuse', first['proc'])
    assert first['turns'] == 1


def test_changed_profile_replaces_the_process(runtime):
    first, _ = acquire(runtime, 'turn_1')
    second, spawned = acquire(runtime, 'turn_2', signature='sig-b')
    assert spawned is True and second['proc'] is not first['proc']
    assert len(runtime.spawned) == 2
    assert runtime.store.bound == [], 'a fresh process keeps its own token; nothing is rebound'


def test_benchmark_tool_exposure_profile_is_allowlisted_and_part_of_runner_signature(runtime, monkeypatch):
    configuration = {'provider': 'p', 'model': 'm', 'key': None, 'provider_id': 'provider_a'}
    runtime_settings = {'search': {'key': None}, 'skill_dirs': [], 'mcp_servers': []}
    monkeypatch.setenv('CAMPUS_BENCH_ALLOW_TOOL_GROUPS', '1')
    monkeypatch.setattr(runtime.store, 'conversation', lambda _cid: {'settings': {
        'benchmark_tool_groups': ['campus_reviews', 'not-allowlisted']}})
    env = runtime._runner_env('conv_reuse', 'token', configuration, runtime_settings)
    assert env['CAMPUS_BENCH_DEFER_TOOL_GROUPS'] == 'campus_reviews'
    groups = runtime._benchmark_tool_groups({'benchmark_tool_groups': ['campus_reviews', 'not-allowlisted']})
    treatment = runtime._runner_signature(configuration, runtime_settings, runtime.home,
                                          benchmark_tool_groups=groups)

    control_groups = runtime._benchmark_tool_groups({'benchmark_tool_groups': []})
    control = runtime._runner_signature(configuration, runtime_settings, runtime.home,
                                        benchmark_tool_groups=control_groups)
    assert treatment != control, 'runner reuse must not cross benchmark tool-exposure conditions'
    monkeypatch.setenv('CAMPUS_BENCH_ALLOW_TOOL_GROUPS', '0')
    assert runtime._benchmark_tool_groups({'benchmark_tool_groups': ['campus_reviews']}) == ()


def test_dead_process_is_replaced(runtime):
    first, _ = acquire(runtime, 'turn_1')
    first['proc'].alive = False
    second, spawned = acquire(runtime, 'turn_2')
    assert spawned is True and second['proc'] is not first['proc']


def test_goal_resume_always_starts_a_clean_process(runtime):
    first, _ = acquire(runtime, 'turn_1')
    second, spawned = acquire(runtime, 'turn_2', resume={'goal': {'id': 'goal-1'}})
    assert spawned is True and second['proc'] is not first['proc']


def test_an_idle_conversation_loses_its_runner(runtime, monkeypatch):
    stale, _ = acquire(runtime, 'turn_1', cid='conv_old')
    monkeypatch.setattr(runtime_module, 'RUNNER_IDLE_SECONDS', 60.0)
    stale['last_used'] = time.monotonic() - 120
    fresh, spawned = acquire(runtime, 'turn_2', cid='conv_new')
    assert spawned is True
    assert 'conv_old' not in runtime.runners, 'a conversation idle past the limit is dropped'
    assert 'conv_new' in runtime.runners
    assert stale['proc'].alive is False


def test_the_oldest_conversation_is_evicted_past_the_live_cap(runtime, monkeypatch):
    monkeypatch.setattr(runtime_module, 'RUNNER_MAX_LIVE', 1)
    first, _ = acquire(runtime, 'turn_1', cid='conv_a')
    first['last_used'] = time.monotonic() - 5
    second, _ = acquire(runtime, 'turn_2', cid='conv_b')
    assert list(runtime.runners) == ['conv_b']
    assert first['proc'].alive is False and second['proc'].alive is True


def test_close_stops_warm_runners(runtime):
    first, _ = acquire(runtime, 'turn_1')
    runtime.close()
    assert runtime.runners == {}
    assert first['proc'].alive is False


def prewarm(runtime, cid='conv_reuse'):
    return runtime.prewarm(cid)


def test_prewarm_boots_the_runtime_and_the_first_turn_reuses_it(runtime, monkeypatch):
    runtime.home = runtime.store and runtime.home  # keep the fixture path
    monkeypatch.setattr(runtime, '_runtime_inputs',
                        lambda settings: ({'provider': 'p', 'model': 'm', 'key': None},
                                          {'search': {'key': None, 'base_url': '', 'model': ''}, 'skill_dirs': [],
                                           'mcp_servers': [], 'workspace': ''}, runtime.home))
    monkeypatch.setattr(runtime, '_runner_env', lambda cid, token, *rest: {'CAMPUS_BRIDGE_TOKEN': token})
    monkeypatch.setattr(runtime, '_runner_signature', lambda *rest: 'sig')
    spawned = []

    def spawn(*args, **kwargs):
        process = FakeProcess(pid=6000 + len(spawned),
                              stdout=json.dumps({'kind': 'prewarm', 'data': {'ms': 1}, 'turn': 1}) + '\n')
        spawned.append(process)
        return process

    monkeypatch.setattr(runtime_module, 'spawn_owned', spawn)
    prewarm(runtime)
    handle = runtime.runners['conv_reuse']
    assert handle['proc'] is spawned[0]
    assert handle['turns'] == 1, 'the prewarm consumed the first frame slot'
    assert '"prewarm": true' in spawned[0].stdin.getvalue()
    assert runtime.store.grants['conv_reuse'] == handle['token']

    # The next turn must reuse the booted process and be addressable by its grant token.
    reused, newly = acquire(runtime, 'turn_1', signature='sig', token='turn-token')
    assert newly is False and reused is handle
    assert runtime.store.bound == [('turn_1', handle['token'])]


def test_prewarm_failure_discards_the_process(runtime, monkeypatch):
    monkeypatch.setattr(runtime, '_runtime_inputs',
                        lambda settings: ({'provider': 'p', 'model': 'm', 'key': None},
                                          {'search': {'key': None, 'base_url': '', 'model': ''}, 'skill_dirs': [],
                                           'mcp_servers': [], 'workspace': ''}, runtime.home))
    monkeypatch.setattr(runtime, '_runner_env', lambda cid, token, *rest: {'CAMPUS_BRIDGE_TOKEN': token})
    monkeypatch.setattr(runtime, '_runner_signature', lambda *rest: 'sig')
    monkeypatch.setattr(runtime_module, 'spawn_owned',
                        lambda *a, **k: FakeProcess(pid=7001, stdout=''))  # EOF: boot never reported
    prewarm(runtime, 'conv_broken')
    assert runtime.runners == {}
    assert 'conv_broken' not in runtime.store.grants


def fail_turn(instance, monkeypatch, stdout, tid='turn_fail', recorded=None, initial_events=(), user_text='你好'):
    """Drive one turn through `_run_owned` with a runner that emitted `stdout` and then ended."""
    turn = {'id': tid, 'conversation_id': 'conv_reuse', 'status': 'running', 'text': user_text, 'attachment_ids': []}
    instance.store.turns[tid] = turn
    instance.store.recorded.extend((tid, kind, data) for kind, data in initial_events)
    if recorded is not None:
        instance.store.failures[tid] = recorded
    monkeypatch.setattr(instance, '_runtime_inputs',
                        lambda settings: ({'provider': 'p', 'model': 'm', 'key': None},
                                          {'search': {'key': None, 'base_url': '', 'model': ''}, 'skill_dirs': [],
                                           'mcp_servers': [], 'workspace': ''}, instance.home))
    monkeypatch.setattr(instance, '_runner_env', lambda cid, token, *rest: {'CAMPUS_BRIDGE_TOKEN': token})
    monkeypatch.setattr(instance, '_runner_signature', lambda *rest: 'sig')
    process = FakeProcess(pid=8100, stdout=stdout)
    process.alive = False
    handle = {'proc': process, 'signature': 'sig', 'token': 'tok', 'capture': None, 'controls': None,
              'turns': 0, 'last_used': time.monotonic()}
    monkeypatch.setattr(instance, '_acquire_runner', lambda *args, **kwargs: (handle, True))
    monkeypatch.setattr(instance, '_discard_runner', lambda *args, **kwargs: None)
    instance._run_owned(turn, 'tok')
    return instance.store.finished[-1]


def test_a_recorded_budget_failure_becomes_the_turn_error(runtime, monkeypatch):
    """The harness frame only says HARNESS_FAILED; the precise cause is already journaled."""
    recorded = {'code': 'BUDGET_EXCEEDED', 'message': '本轮模型调用达到配置预算。', 'retryable': False}
    stdout = json.dumps({'kind': 'error', 'turn': 1,
                         'data': {'code': 'HARNESS_FAILED', 'message': 'Harness 本轮未完成', 'retryable': True}}) + '\n'
    tid, status, response, error = fail_turn(runtime, monkeypatch, stdout, recorded=recorded)
    assert status == 'failed' and response == ''
    assert error == recorded, 'BUDGET_EXCEEDED must stay actionable instead of the generic harness failure'


def test_a_specific_runner_error_is_kept_as_is(runtime, monkeypatch):
    specific = {'code': 'PREWARM_FAILED', 'message': '运行时预热未完成', 'retryable': True}
    stdout = json.dumps({'kind': 'error', 'data': specific, 'turn': 1}) + '\n'
    _, status, _, error = fail_turn(runtime, monkeypatch, stdout,
                                    recorded={'code': 'BUDGET_EXCEEDED', 'message': '不该被采用', 'retryable': False})
    assert status == 'failed' and error == specific


def test_a_turn_that_ends_without_an_answer_reports_the_recorded_cause(runtime, monkeypatch):
    recorded = {'code': 'PROVIDER_TIMEOUT', 'message': 'DeepSeek 模型请求超时', 'retryable': True}
    _, status, _, error = fail_turn(runtime, monkeypatch, '', recorded=recorded)
    assert status == 'failed' and error == recorded


def test_a_turn_without_any_recorded_cause_stays_generic(runtime, monkeypatch):
    _, status, _, error = fail_turn(runtime, monkeypatch, '')
    assert status == 'failed' and error['code'] == 'RUNTIME_FAILED'


def test_per_course_deadline_tool_result_is_rendered_from_trusted_rows(runtime, monkeypatch):
    result = {"status": "partial", "data": {"items": [{
        "id": "private_assignment", "course_title": "PRIVATE COURSE", "assignment_title": "PRIVATE TASK",
        "due_at": "2026-11-02T16:00:00Z", "due_at_local": "2026-11-03T00:00:00+08:00",
        "submission_status": "unknown", "source_ref": {"platform": "xzzd"},
    }]}}
    initial_events = [("tool_result", {"name": "assignments.next_by_course", "result": result})]
    frame = {"kind": "result", "turn": 1, "data": {"conversation_id": "conv_reuse",
        "response": "作业截止日期可能是 2026-11-04。"}}
    finished = fail_turn(runtime, monkeypatch, json.dumps(frame) + "\n", initial_events=initial_events)
    response = finished[2]
    assert "PRIVATE COURSE" in response and "PRIVATE TASK" in response
    assert "2026-11-03T00:00:00+08:00" in response
    assert "2026-11-04" not in response
    assert "本机缓存" in response and "不能证明学校平台全量" in response
    assert any(kind == "response_grounding" and data["rendered_from_tool_data"]
               for _, kind, data in runtime.store.recorded)
    saved_result = next(data for _, kind, data in runtime.store.recorded if kind == "result")
    assert saved_result["response"] == response


def _assignment_list_events(items, *, order_by='due_soon', limit=2, extra_tool=None):
    events = [
        {'kind': 'harness_event', 'data': {'event': {'type': 'tool/call', 'data': {
            'name': 'campus_assignments_list', 'callId': 'assignments-call',
            'arguments': {'order_by': order_by, 'limit': limit}}}}},
        {'kind': 'tool_result', 'data': {'name': 'assignments.list', 'call_id': 'assignments-call',
            'arguments': {'order_by': order_by, 'limit': limit},
            'result': {'status': 'partial', 'data': {'items': items, 'next_cursor': 'opaque'}}}},
    ]
    if extra_tool:
        events.append({'kind': 'tool_started', 'data': {'name': extra_tool}})
    return events


def test_assignment_top_n_renderer_uses_cached_rows_and_china_timezone_without_persisting_private_metadata():
    events = _assignment_list_events([
        {'id': 'private-a', 'course_id': 'private-course', 'course_title': 'PRIVATE COURSE',
         'course_term': '2026秋', 'title': 'PRIVATE ASSIGNMENT',
         'due_at': '2026-11-02T16:00:00Z', 'submission_status': 'not_submitted'},
        {'id': 'private-b', 'course_id': 'private-course', 'course_title': 'PRIVATE COURSE',
         'course_term': '2026秋', 'title': '第二项', 'due_at': None,
         'submission_status': 'unknown'},
    ])
    grounded = assignment_top_n_summary(events, '请列出截止时间最近的2项作业')
    assert grounded is not None
    answer, meta = grounded
    assert '2026-11-03T00:00:00+08:00' in answer
    assert answer.index('2026-11-03') < answer.index('截止时间未知')
    assert 'PRIVATE COURSE' in answer and 'PRIVATE ASSIGNMENT' in answer
    assert '本机缓存' in answer and '不能证明学校平台全量' in answer
    assert meta['rows'] == 2 and meta['unsupported_dates'] == 0
    assert 'private-course' not in json.dumps(meta) and '2026-11-03' not in json.dumps(meta)
    assert 'PRIVATE ASSIGNMENT' not in json.dumps(meta)


def test_assignment_top_n_renderer_deduplicates_overlapping_tool_pages_by_private_id():
    items = [
        {'id': 'private-a', 'course_id': 'private-course', 'course_title': 'Course',
         'title': 'Task A', 'due_at': '2026-11-02T16:00:00Z'},
        {'id': 'private-b', 'course_id': 'private-course', 'course_title': 'Course',
         'title': 'Task B', 'due_at': '2026-11-03T16:00:00Z'},
    ]
    first = _assignment_list_events(items, limit=3)
    second = _assignment_list_events(items, limit=3)
    grounded = assignment_top_n_summary(first + second, '最近的3项作业')
    assert grounded is not None
    answer, meta = grounded
    assert answer.count('Task A') == 1 and answer.count('Task B') == 1
    assert meta['rows'] == 2 and meta['duplicate_rows_collapsed'] == 2
    assert 'private-a' not in json.dumps(meta) and 'private-b' not in json.dumps(meta)


def test_assignment_top_n_renderer_requires_exact_query_and_trusted_course_labels():
    rows = [{'id': 'a', 'course_id': 'c', 'course_title': 'Course', 'title': 'Task',
             'due_at': '2026-11-02T16:00:00Z'}]
    assert assignment_top_n_summary(_assignment_list_events(rows, order_by='id'), '最近的2项作业') is None
    assert assignment_top_n_summary(_assignment_list_events(rows, extra_tool='campus_sync'), '最近的2项作业') is None
    missing_label = [{k: v for k, v in rows[0].items() if k != 'course_title'}]
    assert assignment_top_n_summary(_assignment_list_events(missing_label), '最近的2项作业') is None
    assert assignment_top_n_summary(_assignment_list_events(rows), '每门课程的下一项截止作业') is None


def test_assignment_top_n_answer_is_rendered_after_agent_rewrites_a_supported_date(runtime, monkeypatch):
    result = {'status': 'partial', 'data': {'items': [{
        'id': 'private-assignment', 'course_id': 'private-course', 'course_title': 'PRIVATE COURSE',
        'course_term': '2026秋', 'title': 'PRIVATE ASSIGNMENT', 'due_at': '2026-11-02T16:00:00Z',
        'submission_status': 'not_submitted'}], 'next_cursor': None}}
    events = [('tool_result', {'name': 'assignments.list', 'call_id': 'c1',
        'arguments': {'order_by': 'due_soon', 'limit': 1}, 'result': result})]
    frame = {'kind': 'result', 'turn': 1, 'data': {'conversation_id': 'conv_reuse',
        'response': '作业的截止日期可能是 2026-11-04。'}}
    response = fail_turn(runtime, monkeypatch, json.dumps(frame) + '\n', initial_events=events,
        user_text='请列出截止时间最近的1项作业。')[2]
    assert 'PRIVATE COURSE' in response and 'PRIVATE ASSIGNMENT' in response
    assert '2026-11-03T00:00:00+08:00' in response and '2026-11-04' not in response
    assert '本机缓存' in response and '不能证明学校平台全量' in response
    assert any(kind == 'response_grounding' and data['source'] == 'assignments.list'
               for _, kind, data in runtime.store.recorded)


def _deadline_page(call_id, cursor, next_cursor, course, due):
    return [
        {'kind': 'harness_event', 'data': {'event': {'type': 'tool/call', 'data': {
            'name': 'campus_assignments_next_by_course', 'callId': call_id,
            'arguments': {'limit': 1, **({'cursor': cursor} if cursor else {})}}}}},
        {'kind': 'tool_result', 'data': {'name': 'assignments.next_by_course', 'call_id': call_id,
            'result': {'status': 'ok', 'data': {'items': [{
                'course_title': course, 'assignment_title': 'Task', 'due_at_local': due}],
                'next_cursor': next_cursor}}}},
    ]


def test_deadline_renderer_marks_only_unconsumed_pagination_cursors_incomplete():
    first_page = _deadline_page('page-1', None, 'opaque-cursor', 'Course A', '2026-11-03T00:00:00+08:00')
    second_page = _deadline_page('page-2', 'opaque-cursor', None, 'Course B', '2026-11-04T00:00:00+08:00')
    complete = assignment_deadline_summary(first_page + second_page)
    assert complete is not None
    assert '结果仍有未读取的后续课程页' not in complete[0]
    assert 'Course A' in complete[0] and 'Course B' in complete[0]

    incomplete = assignment_deadline_summary(first_page)
    assert incomplete is not None
    assert '结果仍有未读取的后续课程页' in incomplete[0]


def test_deadline_renderer_explains_courses_without_known_due_dates():
    result = {'status': 'partial', 'coverage': {'complete': False}, 'data': {'items': [
        {'course_id': 'a', 'course_title': 'Course A', 'course_status': 'next_due',
         'assignment_title': 'Task A', 'due_at_local': '2026-11-03T00:00:00+08:00',
         'cached_assignment_count': 2, 'unknown_deadline_count': 1},
        {'course_id': 'b', 'course_title': 'Course B', 'course_status': 'unknown_deadline_only',
         'assignment_title': None, 'due_at_local': None,
         'cached_assignment_count': 1, 'unknown_deadline_count': 1},
        {'course_id': 'c', 'course_title': 'Course C', 'course_status': 'no_cached_assignments',
         'assignment_title': None, 'due_at_local': None,
         'cached_assignment_count': 0, 'unknown_deadline_count': 0},
    ], 'next_cursor': None}}
    rendered = assignment_deadline_summary([{'kind': 'tool_result', 'data': {
        'name': 'assignments.next_by_course', 'call_id': 'statuses-1', 'result': result}}])
    assert rendered is not None
    assert 'Course A' in rendered[0] and '2026-11-03T00:00:00+08:00' in rendered[0]
    assert 'Course B' in rendered[0] and '截止时间未知' in rendered[0]
    assert 'Course C' in rendered[0] and '本机缓存没有作业记录' in rendered[0]
    assert rendered[1]['courses'] == 3 and rendered[1]['rows'] == 1
