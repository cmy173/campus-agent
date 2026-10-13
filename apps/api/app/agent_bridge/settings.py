"""Personal runtime settings. Credentials use the existing local secret store."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import secrets_store
from ..errors import ApiError

MASK = '__SAVED_CREDENTIAL__'
SEARCH_KEY = 'agent-search'


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


def endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('地址必须为 HTTP/HTTPS URL，凭据请填写在密钥或请求头中')
    return value.rstrip('/')


class SearchSettings(Input):
    base_url: str = Field(default='', max_length=2048)
    model: str = Field(default='', max_length=128)
    key_mode: Literal['shared', 'separate'] = 'shared'
    api_key: str | None = Field(default=None, max_length=8192)
    clear_key: bool = False

    @model_validator(mode='after')
    def validate_url(self):
        if self.base_url:
            self.base_url = endpoint(self.base_url)
        if self.api_key and self.clear_key:
            raise ValueError('不能同时保存与清除密钥')
        return self


class McpServer(Input):
    serverName: str = Field(pattern=r'^[A-Za-z0-9_-]{1,32}$')
    enabled: bool = True
    transport: Literal['stdio', 'streamable-http'] = 'stdio'
    command: str = Field(default='', max_length=2048)
    args: list[str] = Field(default_factory=list, max_length=128)
    cwd: str = Field(default='', max_length=2048)
    env: dict[str, str] = Field(default_factory=dict, max_length=64)
    url: str = Field(default='', max_length=2048)
    headers: dict[str, str] = Field(default_factory=dict, max_length=64)
    toolCallTimeoutMs: int = Field(default=60000, ge=1000, le=1800000)

    @model_validator(mode='after')
    def validate_transport(self):
        if self.transport == 'stdio' and not self.command.strip():
            raise ValueError('本地 MCP 服务需要启动命令')
        if self.transport == 'streamable-http':
            self.url = endpoint(self.url)
        for mapping in (self.env, self.headers):
            if any(not k or len(k) > 256 or len(v) > 16384 for k, v in mapping.items()):
                raise ValueError('MCP 环境变量或请求头长度无效')
        return self


class RuntimeSettings(Input):
    workspace: str = Field(default='', max_length=2048)
    skill_dirs: list[str] = Field(default_factory=list, max_length=32)
    search: SearchSettings = Field(default_factory=SearchSettings)
    mcp_servers: list[McpServer] = Field(default_factory=list, max_length=32)

    @model_validator(mode='after')
    def unique_servers(self):
        names = [s.serverName for s in self.mcp_servers]
        if len(names) != len(set(names)):
            raise ValueError('MCP 服务名称不能重复')
        return self


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    def _load(self):
        if not self.path.exists():
            return {'workspace': '', 'skill_dirs': [], 'search': {'base_url': '', 'model': '', 'key_mode': 'shared'}, 'mcp_servers': []}
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ApiError(500, 'SETTINGS_UNREADABLE', '助手设置无法读取，请检查本地设置文件') from None

    def public(self):
        with self.lock:
            value = self._load()
            value['search']['has_api_key'] = secrets_store.has_key(SEARCH_KEY)
            for server in value['mcp_servers']:
                for kind in ('env', 'headers'):
                    server[kind] = {key: MASK for key in server.get(kind, {})}
            return value

    def save(self, settings: RuntimeSettings):
        with self.lock:
            prior = {s['serverName']: s for s in self._load()['mcp_servers']}
            value = settings.model_dump()
            search = value['search']
            key, clear = search.pop('api_key'), search.pop('clear_key')
            pending = []
            for server in value['mcp_servers']:
                for kind in ('env', 'headers'):
                    old = prior.get(server['serverName'], {}).get(kind, {})
                    for name, credential in server[kind].items():
                        if credential == MASK:
                            if name not in old:
                                raise ApiError(400, 'INVALID_INPUT', '没有可保留的 MCP 凭据，请填写实际值')
                            server[kind][name] = old[name]
                        else:
                            identity = f"{server['serverName']}:{kind}:{name}"
                            reference = 'agent-mcp-' + hashlib.sha256(identity.encode()).hexdigest()[:32]
                            server[kind][name] = reference
                            pending.append((reference, credential))
            for reference, credential in pending:
                secrets_store.set_key(reference, credential)
            if key:
                secrets_store.set_key(SEARCH_KEY, key)
            if clear:
                secrets_store.clear_key(SEARCH_KEY)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temporary, self.path)
            return self.public()

    def resolved(self, shared_key: str | None = None):
        with self.lock:
            value = self._load()
            search = value['search']
            search['key'] = shared_key if search['key_mode'] == 'shared' else secrets_store.get_key(SEARCH_KEY)
            for server in value['mcp_servers']:
                for kind in ('env', 'headers'):
                    server[kind] = {name: secrets_store.get_key(ref) or '' for name, ref in server.get(kind, {}).items()}
            return value


def harness_server(server: dict) -> dict:
    common = {k: server[k] for k in ('serverName', 'transport', 'toolCallTimeoutMs')}
    if server['transport'] == 'stdio':
        return common | {k: server[k] for k in ('command', 'args', 'cwd', 'env')}
    return common | {k: server[k] for k in ('url', 'headers')}
