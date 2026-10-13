"""Conversation inputs and published output snapshots, independent of UI layout."""
from __future__ import annotations

import hashlib
import mimetypes
import re
import secrets
from pathlib import Path
from typing import BinaryIO

from ..config import MAX_UPLOAD_BYTES
from ..errors import ApiError
from .store import Store, now


class ConversationFiles:
    def __init__(self, store: Store):
        self.store = store
        self.directory = store.path.parent / 'conversation-files'

    @staticmethod
    def public(row) -> dict:
        return {key: row[key] for key in ('id', 'conversation_id', 'kind', 'name', 'size', 'sha256', 'mime_type', 'created_at')}

    def list(self, cid: str) -> list[dict]:
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM conversation_files WHERE conversation_id=? ORDER BY created_at,id', (cid,)).fetchall()
        return [self.public(row) for row in rows]

    def get(self, cid: str, file_id: str) -> tuple[dict, Path]:
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM conversation_files WHERE id=? AND conversation_id=?', (file_id, cid)).fetchone()
        if row is None:
            raise ApiError(404, 'NOT_FOUND', '文件不属于当前会话或已不可用')
        path = self.directory / row['storage_name']
        if path.is_symlink() or not path.is_file() or path.resolve().parent != self.directory.resolve():
            raise ApiError(404, 'NOT_FOUND', '文件内容已不可用')
        return self.public(row), path

    def save(self, cid: str, stream: BinaryIO, name: str, kind: str, request_key: str, source_path: str = '') -> dict:
        name = re.split(r'[/\\]', name)[-1].strip()
        if not name or len(name) > 240 or any(ord(c) < 32 for c in name):
            raise ApiError(400, 'INVALID_INPUT', '文件名不能为空、含控制字符或超过240个字符')
        suffix = Path(name).suffix.lower()
        if not re.fullmatch(r'\.[a-z0-9]{1,12}', suffix):
            suffix = '.bin'
        fid = 'file_' + secrets.token_hex(16)
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / (fid + suffix)
        size, sha = 0, hashlib.sha256()
        try:
            with target.open('xb') as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise ApiError(413, 'FILE_TOO_LARGE', f'文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB 上限')
                    sha.update(chunk)
                    output.write(chunk)
            mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
            stamp = now()
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT * FROM conversation_files WHERE conversation_id=? AND request_key=?', (cid, request_key)).fetchone()
                if old:
                    if (old['name'], old['sha256'], old['kind']) != (name, sha.hexdigest(), kind):
                        raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '同一文件请求键不能用于不同内容')
                    target.unlink()
                    return self.public(old)
                db.execute('INSERT INTO conversation_files VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (fid, cid, kind, name, size, sha.hexdigest(), mime, target.name, request_key, stamp, source_path))
            return {'id': fid, 'conversation_id': cid, 'kind': kind, 'name': name, 'size': size,
                    'sha256': sha.hexdigest(), 'mime_type': mime, 'created_at': stamp}
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def publish(self, cid: str, source: Path, key: str) -> dict:
        with self.store.connection() as db:
            old = db.execute('SELECT * FROM conversation_files WHERE conversation_id=? AND request_key=?', (cid, key)).fetchone()
        if old:
            if old['source_path'] != str(source):
                raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '同一交付调用不能用于不同文件')
            return self.get(cid, old['id'])[0]
        if not source.is_file():
            raise ApiError(404, 'NOT_FOUND', '需要交付的文件不存在或不是普通文件')
        try:
            with source.open('rb') as stream:
                return self.save(cid, stream, source.name, 'output', key, str(source))
        except OSError:
            raise ApiError(400, 'FILE_UNREADABLE', '无法读取需要交付的文件') from None

    def references(self, turn: dict) -> list[dict]:
        references = []
        for fid in turn.get('attachment_ids', []):
            item, path = self.get(turn['conversation_id'], fid)
            references.append(item | {'path': str(path.resolve())})
        return references

    def preview(self, cid: str, fid: str) -> dict:
        item, path = self.get(cid, fid)
        suffix = Path(item['name']).suffix.lower()
        if suffix == '.pdf':
            return {'kind': 'pdf', 'truncated': False}
        if suffix in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}:
            return {'kind': 'image', 'truncated': False}
        if suffix in {'.txt', '.md', '.csv', '.json', '.yaml', '.yml', '.py', '.js', '.ts', '.html', '.xml', '.log', '.rst'}:
            with path.open('rb') as stream:
                raw = stream.read(512 * 1024 + 1)
            return {'kind': 'text', 'text': raw[:512 * 1024].decode('utf-8-sig', errors='replace'), 'truncated': len(raw) > 512 * 1024}
        return {'kind': 'download', 'truncated': False}
