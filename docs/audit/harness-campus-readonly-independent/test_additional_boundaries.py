"""Independent probes: real workspace/service/cache, synthetic school session only."""
import time
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import campus_workspace as workspace_module
from app.campus_workspace import CampusWorkspace
from app.agent_bridge import runtime as runtime_module
from app.agent_bridge.runtime import Runtime
from app.agent_bridge.tools import ToolContext


class Client:
    passport_authenticated = True
    eta_authenticated = False
    def __init__(self):
        self.requests = 0
    def close(self):
        pass
    def start_operation(self):
        pass
    def grades(self):
        self.requests += 1
        return [], True, 0


class NullSession:
    def __enter__(self):
        return None
    def __exit__(self, *args):
        return False


class Probe:
    RECOVERY_REFRESH_TOOL = Runtime.RECOVERY_REFRESH_TOOL
    probe_authenticated_read = Runtime.probe_authenticated_read
    def _auth_recovery_scope(self, db, checkpoint):
        return ToolContext('local', 'independent-conversation', frozenset(), frozenset(), frozenset())


@pytest.fixture
def site(tmp_path, monkeypatch):
    value = CampusWorkspace(tmp_path / 'campus.sqlite', Client)
    monkeypatch.setattr(workspace_module, 'get_campus_workspace', lambda: value)
    monkeypatch.setattr(runtime_module, 'SessionLocal', NullSession)
    yield value
    value.close()


def pending():
    return {'payload': {'pending_tool': {'name': 'campus.query',
            'arguments': {'capability': 'grades', 'query': {}, 'platform': 'zdbk'}}}}


def test_real_cache_read_does_not_cross_any_network_or_auth_boundary(site, monkeypatch):
    def forbidden(*a, **k):
        pytest.fail('cache read crossed an external/auth boundary')
    monkeypatch.setattr(site, 'auto_relogin_locked', forbidden)
    monkeypatch.setattr(site, '_browser_cookie_provider', forbidden)
    monkeypatch.setattr(site.manager.service, 'sync', forbidden)
    import socket
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    result = site.query('grades', {}, 'zdbk')
    assert result['records'] == []
    assert result['error']['code'] == 'AUTH_REQUIRED'
    assert site.status()['connected'] is False
    site.manager.academic_view('local', '2026-10-16')


def test_trusted_probe_can_refresh_stored_auth_error_with_real_service(site, monkeypatch):
    # Persist a real AUTH failure through the service, then simulate successful
    # authentication by installing only the synthetic session at its real boundary.
    def forbidden(*a, **k):
        pytest.fail('already authenticated probe tried to log in again')
    monkeypatch.setattr(site, 'auto_relogin_locked', forbidden)
    assert site.manager.service.sync('grades', {}, workspace_module.UserContext('local')).error
    client = Client()
    site.manager.states['local'] = {'client': client, 'connected': True,
        'expires': time.monotonic() + 3600, 'identity': 'synthetic-independent'}
    result = Probe().probe_authenticated_read(pending())
    assert result['error'] is None
    assert result['data']['error'] is None
    assert client.requests == 1
    assert site.query('grades', {}, 'zdbk')['error'] is None


def test_trusted_probe_without_target_session_does_not_start_authentication(site, monkeypatch):
    # A successful check of some other connection is not proof of a live ZDBK
    # session. The recovery callback advertises "no login/browser of its own".
    attempts = []
    monkeypatch.setattr(site, 'auto_relogin_locked',
                        lambda: attempts.append('password-path') or {'ok': False, 'code': 'NO_CREDENTIAL'})
    result = Probe().probe_authenticated_read(pending())
    assert result['error']['code'] == 'AUTH_REQUIRED'
    assert attempts == [], 'trusted read probe invoked password authentication path'


def test_notice_judge_accepts_a_clearly_negated_whole_school_claim():
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'tools'))
    import verify_campus_readonly_live as judge
    assert judge.affirmative_whole_school_claim(
        '本机缓存里**没有**能读到正文的「全校性本科生选课确认」通知。') == []


def test_job_judge_accepts_terminal_result_from_explicit_sync_wait():
    import json
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'tools'))
    import verify_campus_readonly_live as judge
    source = Path(__file__).resolve().parents[3] / 'var/campus-readonly-independent/model-t2.json'
    record = json.loads(source.read_text(encoding='utf-8'))['sessions'][0]
    sync = next(c for c in record['turns'][0]['tool_calls'] if c['name'] == 'connections.sync')
    job = sync['evidence']['jobs'][0]
    assert sync['arguments']['wait_seconds'] > 0 and job['status'] == 'succeeded'
    assert any(j['id'] == job['id'] and j['status'] == 'succeeded'
               for j in record['domain_evidence']['jobs'])
    assert judge.judge_t2(record)['job_observed']['passed'], (
        'model consumed a real terminal sync result; a redundant jobs.read is unnecessary')
