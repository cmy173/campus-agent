"""Architecture regression: real bridge + synthetic DB, never a school/model request."""
import pytest
import threading
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy.orm import Session

from app import models as m
from app.agent_bridge import tools
from app.errors import ApiError
from test_agent_gateway import gateway, conversation, turn, internal  # noqa: F401
from test_agent_connection_sync_tool import db, ctx, XZZD, ZYKT  # noqa: F401


def page(course_id, cursor=None):
    return {"items": [{"external_id": course_id, "title": course_id, "term": "2026秋"}],
            "next_cursor": cursor}


def test_discovery_keeps_cursor_and_scope_and_does_not_claim_full_coverage(db, monkeypatch):
    monkeypatch.setattr(tools.connections, "discover_platform_courses", lambda *a, **kw: page("first", "page-2"))
    result = tools.execute("connections.courses", {"platform": "xzzd"}, db, ctx())
    assert result["next_cursor"], "platform cursor was discarded by the tool wrapper"
    assert result["coverage"]["complete"] is False
    assert result["status"] == "partial"
    assert result["data"]["courses"][0]["external_id"] == "first"


def test_course_discovery_one_platform_failure_keeps_the_other(db, monkeypatch):
    def discover(cid, *a, **kw):
        if cid == XZZD:
            raise ApiError(502, "TIMEOUT", "synthetic timeout", retryable=True)
        return page("surviving")
    db.get(m.Connection, ZYKT).status = "ok"
    db.commit()
    monkeypatch.setattr(tools.connections, "discover_platform_courses", discover)
    result = tools.execute("connections.courses", {}, db, ctx())
    assert result["status"] == "partial"
    assert [c["external_id"] for c in result["data"]["courses"]] == ["surviving"]
    assert result["data"]["errors"][0]["error"]["code"] == "TIMEOUT"


def test_import_uses_requested_page_instead_of_silently_importing_first_page(db, monkeypatch):
    seen = []
    def discover(cid, session, **kw):
        seen.append(kw)
        return page("second" if kw.get("cursor") == "page-2" else "first")
    monkeypatch.setattr(tools.connections, "discover_platform_courses", discover)
    result = tools.execute("connections.courses.import", {"platform": "xzzd", "all": True,
                           "cursor": "page-2"}, db, ctx())
    assert seen == [{"cursor": "page-2"}]
    assert result["data"]["courses"][0]["title"] == "second"


def test_import_then_read_is_possible_in_same_conversation_and_durable(gateway, monkeypatch):
    client, runtime, tokens, engine = gateway
    with Session(engine) as session:
        session.add(m.Connection(id=XZZD, platform="xzzd", display_name="合成平台", status="ok"))
        session.commit()
    cid = conversation(client)
    other_cid = conversation(client)
    tid = turn(client, cid)
    monkeypatch.setattr(tools.connections, "discover_platform_courses", lambda *a, **kw: page("new-course"))
    def call(name, args, call_id):
        response = internal(client, tokens[tid], "tool", {"conversation_id": cid,
                            "name": name, "arguments": args, "call_id": call_id})
        assert response.status_code == 200, response.text
        return response.json()
    imported = call("connections.courses.import", {"platform": "xzzd", "all": True}, "import")
    new_id = imported["data"]["courses"][0]["id"]
    result = call("materials.list", {"course_id": new_id}, "read")
    assert result["error"] is None, result
    assert new_id in runtime.store.conversation(cid)["settings"]["course_ids"]
    assert new_id not in runtime.store.conversation(other_cid)["settings"]["course_ids"]
    started = call('connections.sync', {'platform': 'xzzd'}, 'sync-imported')
    assert started['status'] == 'queued'
    # Simulate ONLY the school/worker output; subsequent requests use the actual
    # bridge, durable job, scope grants and material services, not stubbed tools.
    with Session(engine) as worker:
        job = worker.get(m.Job, started['job_id'])
        assert job is not None and job.payload['course_ids'] == [new_id]
        worker.add(m.Resource(id='imported_material', course_id=new_id, kind='document',
                              title='合成平台课件', version=1, parse_status='ready'))
        worker.flush()
        worker.add(m.ContentChunk(id='imported_chunk', resource_id='imported_material', resource_version=1,
                                  ordinal=0, text='合成课件原文', locator={'paragraph': 1}, provenance={'kind': 'synthetic'}))
        job.status = 'succeeded'
        worker.commit()
    polled = call('jobs.read', {'job_id': started['job_id']}, 'poll-imported')
    assert polled['data']['status'] == 'succeeded'
    materials = call('materials.list', {'course_id': new_id}, 'list-imported')
    assert materials['data']['items'][0]['id'] == 'imported_material'
    full = call('materials.read', {'resource_id': 'imported_material', 'expected_version': 1}, 'read-imported')
    assert full['data']['items'][0]['text'] == '合成课件原文'
    replay = call("connections.courses.import", {"platform": "xzzd", "all": True}, "import")
    assert replay == imported
    assert runtime.store.conversation(cid)["settings"]["course_ids"].count(new_id) == 1


def test_sync_never_expands_a_narrow_conversation_to_every_bound_course(db):
    db.add(m.Course(id="not_granted", title="未授权课程"))
    db.add(m.SourceRef(owner_type="course", owner_id="not_granted", platform="xzzd",
                       external_id="ungranted-remote", observed_at=m.utc_now()))
    db.commit()
    result = tools.execute("connections.sync", {"platform": "xzzd"}, db, ctx())
    job = db.get(m.Job, result["job_id"])
    assert job.payload["course_ids"] == ["course_math"]


def test_reusing_a_wider_sync_job_does_not_grant_its_results_to_a_narrow_scope(db):
    db.add(m.Course(id="not_granted", title="未授权课程"))
    db.add(m.Job(id="wide_job", type="sync", status="queued", errors=[], result_refs=[],
                 payload={"connection_id": XZZD, "course_ids": ["course_math", "not_granted"], "full": False}))
    db.commit()
    with pytest.raises(ApiError) as exc:
        tools.execute("connections.sync", {"platform": "xzzd"}, db, ctx())
    assert exc.value.code == "SYNC_SCOPE_CONFLICT"


def test_a_slow_browser_operation_does_not_block_an_independent_native_lease(gateway, monkeypatch):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    entered, release = threading.Event(), threading.Event()
    def browser_rpc(*a, **kw):
        if a[1] == 'call':
            entered.set()
            assert release.wait(5)
        return {'content': [], 'downloads': []}
    monkeypatch.setattr(runtime.browser, 'rpc', browser_rpc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        browser = pool.submit(internal, client, tokens[tid], 'browser', {
            'conversation_id': cid, 'method': 'call', 'name': 'browser_navigate',
            'arguments': {'url': 'https://example.org/'}})
        assert entered.wait(2)
        lease = pool.submit(internal, client, tokens[tid], 'tool-lease', {
            'conversation_id': cid, 'call_id': 'other-child-read', 'name': 'read'})
        try:
            assert lease.result(timeout=1).status_code == 200
        finally:
            release.set()
        assert browser.result(timeout=3).status_code == 200


@pytest.mark.parametrize('status, expected', [('failed', 'error'), ('partial_failed', 'partial')])
def test_background_failure_is_not_a_successful_business_outcome(db, status, expected):
    db.add(m.Job(id='broken_sync', type='sync', status=status, result_refs=[],
                 errors=[{'code': 'TIMEOUT', 'message': 'synthetic transport failed', 'retryable': True}],
                 payload={'connection_id': XZZD, 'course_ids': ['course_math'], 'full': False}))
    db.commit()
    result = tools.execute('jobs.read', {'job_id': 'broken_sync'}, db, ctx())
    assert result['status'] == expected
    assert not result['coverage']['complete']
    assert result['data']['errors'][0]['code'] == 'TIMEOUT'


def test_sync_receipt_is_a_committed_job_visible_to_the_next_request_and_worker(gateway):
    client, runtime, tokens, engine = gateway
    with Session(engine) as session:
        session.add(m.Connection(id=XZZD, platform='xzzd', display_name='合成平台', status='ok'))
        session.add(m.SourceRef(owner_type='course', owner_id='course_a', platform='xzzd',
                                external_id='remote-a', observed_at=m.utc_now()))
        session.commit()
    cid = conversation(client)
    tid = turn(client, cid)
    started = internal(client, tokens[tid], 'tool', {'conversation_id': cid, 'call_id': 'sync',
                       'name': 'connections.sync', 'arguments': {'platform': 'xzzd'}}).json()
    assert started['status'] == 'queued'
    with Session(engine) as worker_session:
        assert worker_session.get(m.Job, started['job_id']) is not None, 'receipt points at a rolled-back job'
    polled = internal(client, tokens[tid], 'tool', {'conversation_id': cid, 'call_id': 'poll',
                       'name': 'jobs.read', 'arguments': {'job_id': started['job_id']}}).json()
    assert polled['error'] is None


@pytest.mark.parametrize('operation', ['jobs.read', 'jobs.cancel'])
def test_knowing_a_job_id_never_grants_access_to_other_courses(db, operation):
    db.add(m.Job(id='wide_job', type='sync', status='queued', result_refs=[], errors=[],
                 payload={'connection_id': XZZD, 'course_ids': ['course_math', 'not_granted'], 'full': False}))
    db.commit()
    with pytest.raises(ApiError) as error:
        tools.execute(operation, {'job_id': 'wide_job'}, db, ctx())
    assert error.value.code == 'SCOPE_DENIED'


def test_multi_platform_sync_keeps_a_committed_job_when_other_platform_fails(db, monkeypatch):
    db.get(m.Connection, ZYKT).status = 'ok'
    db.commit()
    enqueue = tools.connections.enqueue_connection_sync
    def partial(session, cid, **kw):
        if cid == ZYKT:
            raise ApiError(502, 'NETWORK_UNREACHABLE', 'synthetic unavailable', retryable=True)
        return enqueue(session, cid, **kw)
    monkeypatch.setattr(tools.connections, 'enqueue_connection_sync', partial)
    result = tools.execute('connections.sync', {}, db, ctx())
    assert result['job_id'] and result['status'] == 'queued'
    assert not result['coverage']['complete']
    assert result['data']['errors'][0]['error']['code'] == 'NETWORK_UNREACHABLE'
    db.rollback()
    assert db.get(m.Job, result['job_id']) is not None
