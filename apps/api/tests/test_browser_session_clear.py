"""Synthetic shared-profile clearing: no browser process, no user Edge profile."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent_bridge.browser import BrowserHost
from app.errors import ApiError


def seed(home: Path) -> None:
    for name in ('profile', 'downloads', 'outputs', 'tmp'):
        (home / name).mkdir(parents=True, exist_ok=True)
    (home / 'profile' / 'Cookies').write_bytes(b'synthetic-cookie-db')
    (home / 'downloads' / 'conv_a').mkdir(parents=True)
    (home / 'downloads' / 'conv_a' / 'f.bin').write_bytes(b'synthetic')
    (home / 'outputs' / 'page.yml').write_text('synthetic snapshot', encoding='utf-8')
    (home / 'actions.json').write_text(json.dumps({'reviews': [{'conversation_id': 'conv_a'}], 'downloads': []}), encoding='utf-8')


def test_clear_session_removes_owned_components_without_a_daemon(tmp_path):
    home = tmp_path / 'browser'
    seed(home)
    host = BrowserHost(home)
    receipt = host.clear_session_private()
    assert receipt['cleared'] is True
    assert set(receipt['components']) == {'profile', 'downloads', 'outputs', 'tmp', 'actions.json'}
    assert not (home / 'profile').exists() and not (home / 'actions.json').exists()
    assert (home / 'profile' / 'Cookies').exists() is False
    # Reusable empty scratch directories remain, but no session material does.
    for name in ('downloads', 'outputs', 'tmp'):
        assert (home / name).is_dir()
    assert 'Cookies' not in json.dumps(receipt)


def test_clear_session_is_idempotent_and_reports_absent_parts(tmp_path):
    home = tmp_path / 'browser'
    host = BrowserHost(home)
    first = host.clear_session_private()
    assert first['cleared'] is True and set(first['absent']) == {'profile', 'downloads', 'outputs', 'tmp', 'actions.json'}
    assert host.clear_session_private()['cleared'] is True


def test_clear_session_refuses_links_and_outside_targets(tmp_path, monkeypatch):
    home = tmp_path / 'browser'
    seed(home)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep.txt').write_text('must survive', encoding='utf-8')
    host = BrowserHost(home)
    original = Path.lstat
    def lstat(candidate, *args, **kwargs):
        if Path(candidate) == home / 'profile':
            import stat as stat_module
            return type('M', (), {'st_mode': stat_module.S_IFLNK, 'st_file_attributes': 0})()
        return original(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(ApiError) as error:
        host.clear_session_private()
    assert error.value.code == 'BROWSER_PATH_UNSAFE'
    assert (home / 'profile').exists() and (outside / 'keep.txt').exists()


def test_clear_session_refuses_while_an_operation_is_live(tmp_path):
    home = tmp_path / 'browser'
    seed(home)
    host = BrowserHost(home)
    host.active_operation = {'turn_id': 'turn_a', 'name': 'browser_click'}
    with pytest.raises(ApiError) as error:
        host.clear_session_private()
    assert error.value.code == 'BROWSER_BUSY'
    assert (home / 'actions.json').exists()
