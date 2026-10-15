"""Isolated settings boundary; no app client, DB, browser or real credential store."""
import json

import pytest
from pydantic import ValidationError

from app.agent_bridge.settings import MASK, SEARCH_KEY, McpServer, RuntimeSettings, SettingsStore, harness_server
from app.errors import ApiError


@pytest.fixture
def settings_store(monkeypatch):
    # Isolate filesystem IO as well as credentials. This runs the real save /
    # public / resolved logic without writing host state or sharing pytest temp.
    class MemoryPath:
        files = {}

        def __init__(self, name='settings.json'):
            self.name = name
            self.parent = self

        def exists(self):
            return self.name in self.files

        def read_text(self, **_kwargs):
            return self.files[self.name]

        def write_text(self, value, **_kwargs):
            self.files[self.name] = value

        def mkdir(self, **_kwargs):
            pass

        def with_suffix(self, suffix):
            return MemoryPath('settings' + suffix)

    def replace(source, target):
        target.files[target.name] = source.files.pop(source.name)

    monkeypatch.setattr('app.agent_bridge.settings.os.replace', replace)
    credentials = {}
    cleared = []
    monkeypatch.setattr('app.agent_bridge.settings.secrets_store.set_key', lambda ref, value: credentials.__setitem__(ref, value))
    monkeypatch.setattr('app.agent_bridge.settings.secrets_store.get_key', lambda ref: credentials.get(ref))
    monkeypatch.setattr('app.agent_bridge.settings.secrets_store.has_key', lambda ref: ref in credentials)

    def clear(ref):
        cleared.append(ref)
        return credentials.pop(ref, None) is not None

    monkeypatch.setattr('app.agent_bridge.settings.secrets_store.clear_key', clear)
    return SettingsStore(MemoryPath()), credentials, cleared


def server(name='fixture', **kwargs):
    return {'serverName': name, 'transport': 'stdio', 'command': 'unused', **kwargs}


def test_enabled_is_not_full_authority_and_legacy_harness_default_is_empty():
    value = McpServer.model_validate(server())
    assert value.enabled and value.allowedTools == []
    configured = McpServer.model_validate(server(allowedTools=['read_course', 'submit_assignment']))
    assert configured.allowedTools == ['read_course', 'submit_assignment']
    raw = configured.model_dump()
    assert harness_server(raw)['allowedTools'] == configured.allowedTools
    raw.pop('allowedTools')
    assert harness_server(raw)['allowedTools'] == []


@pytest.mark.parametrize('names', [['*'], ['read*'], ['read', 'read'], ['namespace.read'], [''], ['x' * 64], [1]])
def test_mcp_scope_requires_unique_exact_untruncated_names(names):
    with pytest.raises(ValidationError):
        McpServer.model_validate(server(allowedTools=names))


def test_hints_and_reserved_browser_cannot_grant_user_mcp_authority():
    with pytest.raises(ValidationError):
        McpServer.model_validate(server(readOnlyHint=True))
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({'mcp_servers': [server('campus_browser')]})
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({'mcp_servers': [server(), server()]})


def test_save_and_private_harness_permissions_do_not_expose_env_or_header_values(settings_store):
    store, credentials, _ = settings_store
    value = RuntimeSettings.model_validate({'mcp_servers': [server(allowedTools=['read'], env={'PRIVATE': 'fixture-private-value'},
        headers={'Authorization': 'fixture-private-header'})]})
    public = store.save(value)
    assert public['mcp_servers'][0]['allowedTools'] == ['read']
    assert public['mcp_servers'][0]['env'] == {'PRIVATE': MASK}
    assert public['mcp_servers'][0]['headers'] == {'Authorization': MASK}
    persisted = store.path.read_text(encoding='utf-8')
    assert 'fixture-private-value' not in persisted and 'fixture-private-header' not in persisted
    assert 'fixture-private-value' not in json.dumps(public)
    resolved = store.resolved()['mcp_servers'][0]
    assert resolved['env']['PRIVATE'] == 'fixture-private-value'
    assert resolved['headers']['Authorization'] == 'fixture-private-header'
    harness = harness_server(resolved)
    assert harness['allowedTools'] == ['read'] and harness['env']['PRIVATE'] == 'fixture-private-value'
    assert 'headers' not in harness  # only selected transport's private credentials
    assert len(credentials) == 2


def test_removed_env_header_and_server_references_are_cleaned_after_save(settings_store):
    store, credentials, cleared = settings_store
    store.save(RuntimeSettings.model_validate({'search': {'api_key': 'search-private'}, 'mcp_servers': [
        server(env={'KEEP': 'keep-secret', 'DROP': 'drop-secret'}, headers={'Authorization': 'header-secret'}),
        server('removed', env={'KEY': 'removed-server-secret'})]}))
    before = json.loads(store.path.read_text(encoding='utf-8'))
    keep_ref = before['mcp_servers'][0]['env']['KEEP']
    orphan_refs = {before['mcp_servers'][0]['env']['DROP'], before['mcp_servers'][0]['headers']['Authorization'],
                   before['mcp_servers'][1]['env']['KEY']}
    store.save(RuntimeSettings.model_validate({'mcp_servers': [server(allowedTools=['read'], env={'KEEP': MASK})]}))
    assert credentials[keep_ref] == 'keep-secret'
    assert credentials[SEARCH_KEY] == 'search-private'
    assert set(cleared) == orphan_refs
    assert not any(ref in credentials for ref in orphan_refs)
    assert store.public()['mcp_servers'][0]['allowedTools'] == ['read']


def test_credential_rotation_keeps_reference_and_invalid_mask_has_no_side_effect(settings_store):
    store, credentials, cleared = settings_store
    store.save(RuntimeSettings.model_validate({'mcp_servers': [server(env={'KEY': 'old-secret'})]}))
    prior = store.path.read_text(encoding='utf-8')
    ref = json.loads(prior)['mcp_servers'][0]['env']['KEY']
    with pytest.raises(ApiError):
        store.save(RuntimeSettings.model_validate({'mcp_servers': [server(env={'MISSING': MASK})]}))
    assert store.path.read_text(encoding='utf-8') == prior and credentials[ref] == 'old-secret' and not cleared
    store.save(RuntimeSettings.model_validate({'mcp_servers': [server(env={'KEY': 'new-secret'})]}))
    assert credentials[ref] == 'new-secret' and not cleared


def test_removing_only_allowlist_never_revokes_credentials(settings_store):
    store, credentials, cleared = settings_store
    store.save(RuntimeSettings.model_validate({'mcp_servers': [server(allowedTools=['read'], env={'KEY': 'private'})]}))
    store.save(RuntimeSettings.model_validate({'mcp_servers': [server(allowedTools=[], env={'KEY': MASK})]}))
    assert list(credentials.values()) == ['private'] and not cleared
    assert store.resolved()['mcp_servers'][0]['allowedTools'] == []
