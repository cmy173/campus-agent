"""A successful campus authentication in the normal account UI must wake pending tasks."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'apps/api/tests'))
from test_agent_gateway import gateway
from app import campus_workspace


@pytest.mark.parametrize('success', [True, False])
def test_successful_passport_test_login_notifies_auth_recovery(gateway, monkeypatch, success):
    client, runtime, _, _ = gateway
    notifications = []
    monkeypatch.setattr(runtime, 'resume_authenticated_tasks', lambda: notifications.append('passport-ready'))

    monkeypatch.setattr(campus_workspace.CampusWorkspace, 'auto_relogin_locked',
                        lambda self: {'ok': success, 'code': 'AUTO_LOGIN_OK' if success else 'CREDENTIAL_REJECTED',
                                      'needs_user': not success})
    monkeypatch.setattr(campus_workspace.CampusWorkspace, 'status',
                        lambda self: {'connected': success, 'passport_connected': success})
    response = client.post('/api/v1/agent/campus/credentials/test-login', json={'scope': 'passport'})
    assert response.status_code == 200 and response.json()['ok'] is success, response.text
    assert notifications == (['passport-ready'] if success else []), (
        'only a successful trusted campus login should wake pending recovery')
