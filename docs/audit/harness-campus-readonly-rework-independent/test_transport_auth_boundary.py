"""Probe the real nested transport: a read-only recovery must not log in to WebVPN."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'harness-campus-readonly-independent'))
from test_additional_boundaries import Probe, NullSession, pending
from app import campus_workspace as workspace_module
from app.campus_workspace import CampusWorkspace
from app.agent_bridge import runtime as runtime_module
from connectors.campus.campus import CampusClient
from connectors.campus import webvpn_tunnel
from connectors.campus.campus_common import CampusError


def test_recovery_does_not_authenticate_an_expired_webvpn_channel(tmp_path, monkeypatch):
    attempts = []

    class Portal:
        def login(self, username, password):
            attempts.append('portal-password-submit')
            raise CampusError('INTERACTION_REQUIRED', 'synthetic portal needs a captcha')
        def close(self):
            pass

    tunnel = webvpn_tunnel.WebVPNTunnel(adapter_factory=Portal)
    monkeypatch.setattr(webvpn_tunnel, 'credential_provider', lambda: lambda: {
        'username': 'synthetic-vpn-user', 'password': 'synthetic-vpn-secret', 'auto_relogin': True})
    monkeypatch.setattr(webvpn_tunnel, '_tunnel', tunnel)
    monkeypatch.setenv('CAMPUS_WEBVPN_MODE', 'webvpn')
    value = CampusWorkspace(tmp_path / 'campus.sqlite')
    client = CampusClient()
    client.passport_authenticated = True
    value.manager.states['local'] = {'client': client, 'connected': True,
        'expires': time.monotonic() + 3600, 'identity': 'synthetic-independent'}
    monkeypatch.setattr(workspace_module, 'get_campus_workspace', lambda: value)
    monkeypatch.setattr(runtime_module, 'SessionLocal', NullSession)
    monkeypatch.setattr(value, 'auto_relogin_locked', lambda: pytest.fail('CAS login must not start'))
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('no real network allowed'))
    try:
        result = Probe().probe_authenticated_read(pending())
        assert result['error'] is not None
        assert attempts == [], 'read-only recovery initiated WebVPN password authentication'
    finally:
        value.close()
        tunnel.close()
